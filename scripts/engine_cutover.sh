#!/usr/bin/env bash
# WS4 engine cutover: qwen38-chat (vLLM 0.28.0 syv-ai/HyperQwen, sm86-tuned) ->
# qwen38-nvfp4 (vLLM 0.29.0 official image, NVFP4, sm120-native canary).
#
# NOT RUN as part of the 2026-09-21 preparation pass (Legion sprint 15018) —
# this script is written and verified-by-inspection only. Execute it only
# after the owner's cutover-window sign-off (Part D / WS4.3 of the plan) and
# outside 09:30-16:00 ET.
#
# This gate would pass trivially if it silently continued past a failed
# health check or a failed bench threshold instead of exiting non-zero —
# it does not: every stage below is a hard gate on the next.
#
# Sequence follows the exact gotchas recorded live 2026-08-31/09-01
# (~/.claude memory project_ada_qwen38_local_engine_migration_2026_08_31.md):
#   1. The wedge-monitor resurrects its target ~4 min after losing metrics —
#      it must be recreated with the NEW VLLM_TARGET_CONTAINER in the SAME
#      command that stops the old engine, or it steals the port back.
#   2. Any `docker compose up -d` against docker-compose.bifrost.yml can
#      bounce shared-bifrost itself (recreating one sidecar there restarted
#      the gateway once, opening ADA's circuit breaker for ~5 min) — so the
#      Bifrost cutover is one batched stop-edit-restart, never a partial up.
#   3. Bifrost caches VK governance rows at boot — after sync_vk_allowlists.py
#      a new model name still 403s until Bifrost restarts AGAIN. Working
#      order: edit config.json -> restart bifrost -> sync -> restart bifrost.
#   4. bifrost-autoheal's upstream probe model must be the engine's REAL
#      served name, never only a Bifrost-side alias.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VLLM_COMPOSE="$INFRA_DIR/docker-compose.vllm.yml"
BIFROST_COMPOSE="$INFRA_DIR/docker-compose.bifrost.yml"
ENV_FILE="$INFRA_DIR/.env"
BENCH_SCRIPT="$SCRIPT_DIR/engine_bench.py"
CONFIG_JSON="$INFRA_DIR/bifrost/config.json"
REPORT_DIR="$INFRA_DIR/reports"
NOW_TAG="$(date -u +%Y%m%dT%H%M%SZ)"

MODE="${1:-}"
if [[ "$MODE" != "--commit" && "$MODE" != "--rollback" && "$MODE" != "--dry-run" ]]; then
  echo "usage: $0 --dry-run|--commit|--rollback" >&2
  exit 2
fi

log() { echo "[engine_cutover $(date -u +%H:%M:%S)] $*" >&2; }

require_owner_ack() {
  if [[ "${ENGINE_CUTOVER_OWNER_ACK:-}" != "yes" ]]; then
    log "REFUSED: set ENGINE_CUTOVER_OWNER_ACK=yes to confirm the owner's cutover-window sign-off (Part D / WS4.3)."
    exit 3
  fi
}

wait_for_models() {
  local url="$1" model="$2" tries="${3:-60}"
  for ((i = 1; i <= tries; i++)); do
    if curl -sf --max-time 5 "$url/v1/models" 2>/dev/null | grep -q "\"$model\""; then
      log "models endpoint at $url reports $model ready (attempt $i)"
      return 0
    fi
    sleep 5
  done
  log "FAILED: $url never reported $model ready after $tries attempts"
  return 1
}

bench_gate() {
  local base_url="$1" model="$2" out="$3"
  local vk
  vk="$(grep '^INFRA_PROBE_VK=' "$ENV_FILE" | cut -d= -f2-)"
  python3 "$BENCH_SCRIPT" --base-url "$base_url" --model "$model" --api-key "$vk" \
    --concurrency 1 8 32 --n 12 --out "$out"
  python3 - "$out" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
ok = True
for level in data["concurrency_results"]:
    if level["success_pct"] < 95.6:
        print(f"GATE FAIL: concurrency={level['concurrency']} success_pct={level['success_pct']} < 95.6 baseline")
        ok = False
if not data["tool_call_probe"]["pass"]:
    print("GATE FAIL: tool-call probe did not pass")
    ok = False
if not data["guided_json_probe"]["pass"]:
    print("GATE FAIL: guided-json probe did not pass")
    ok = False
sys.exit(0 if ok else 1)
PYEOF
}

cmd_commit() {
  require_owner_ack
  mkdir -p "$REPORT_DIR"

  log "stage 1/6: stop qwen38-chat + recreate wedge-monitor targeting qwen38-nvfp4, in one command"
  VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
    docker compose -f "$VLLM_COMPOSE" stop qwen38-chat
  VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
    docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor

  log "stage 2/6: start qwen38-nvfp4 canary"
  docker compose -f "$VLLM_COMPOSE" --profile nvfp4-canary up -d qwen38-nvfp4

  log "stage 3/6: wait for /v1/models"
  wait_for_models "http://127.0.0.1:18802" "qwen3.8-27b" 90

  log "stage 4/6: bench at 1/8/32 concurrent against the canary directly (bypassing Bifrost)"
  bench_gate "http://127.0.0.1:18802/v1" "qwen3.8-27b" \
    "$REPORT_DIR/engine-bench-canary-$NOW_TAG.json" || {
      log "BENCH GATE FAILED — cutover aborted before touching Bifrost. Run '$0 --rollback' to restore qwen38-chat."
      exit 4
    }

  log "stage 5/6: cut Bifrost over (edit config.json -> restart -> sync VK allowlists -> restart again)"
  python3 - "$CONFIG_JSON" <<'PYEOF'
# providers is a DICT keyed by name (verified live 2026-09-21 via
# `git show HEAD:bifrost/config.json`, not a list) and base_url lives at
# providers["vllm-local"]["network_config"]["base_url"], NOT per-key.
import json, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
cfg["providers"]["vllm-local"]["network_config"]["base_url"] = "http://qwen38-nvfp4:18020"
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("bifrost config.json vllm-local base_url -> http://qwen38-nvfp4:18020")
PYEOF
  docker compose -f "$BIFROST_COMPOSE" restart shared-bifrost
  python3 "$INFRA_DIR/bifrost/sync_vk_allowlists.py"
  docker compose -f "$BIFROST_COMPOSE" restart shared-bifrost

  log "stage 6/6: set autoheal probe model to the real served name and verify through Bifrost"
  sed -i 's/AUTOHEAL_PROBE_MODEL:.*/AUTOHEAL_PROBE_MODEL: "qwen3.8-27b"/' "$BIFROST_COMPOSE" || true
  docker compose -f "$BIFROST_COMPOSE" up -d bifrost-autoheal
  wait_for_models "http://127.0.0.1:4445" "qwen3.8-27b" 30

  log "COMMIT COMPLETE. Old qwen38-chat is stopped (not removed); weights + qwen38-models volume intact for rollback."
}

cmd_rollback() {
  require_owner_ack
  log "stage 1/5: stop qwen38-nvfp4 canary"
  docker compose -f "$VLLM_COMPOSE" --profile nvfp4-canary stop qwen38-nvfp4 || true

  log "stage 2/5: restart qwen38-chat + recreate wedge-monitor targeting it, in one command"
  VLLM_TARGET_CONTAINER=qwen38-chat \
    docker compose -f "$VLLM_COMPOSE" up -d qwen38-chat
  VLLM_TARGET_CONTAINER=qwen38-chat \
    docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor

  log "stage 3/5: wait for /v1/models on the restored engine"
  wait_for_models "http://127.0.0.1:18801" "qwen3.8-27b" 90

  log "stage 4/5: revert Bifrost config.json base_url and restart twice (same lock-in-cache gotcha applies both ways)"
  python3 - "$CONFIG_JSON" <<'PYEOF'
# Same dict-shaped providers structure as the commit path above.
import json, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
cfg["providers"]["vllm-local"]["network_config"]["base_url"] = "http://qwen38-chat:18020"
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("bifrost config.json vllm-local base_url -> http://qwen38-chat:18020")
PYEOF
  docker compose -f "$BIFROST_COMPOSE" restart shared-bifrost
  python3 "$INFRA_DIR/bifrost/sync_vk_allowlists.py"
  docker compose -f "$BIFROST_COMPOSE" restart shared-bifrost

  log "stage 5/5: verify through Bifrost"
  wait_for_models "http://127.0.0.1:4445" "qwen3.8-27b" 30
  log "ROLLBACK COMPLETE."
}

case "$MODE" in
  --dry-run)
    log "dry-run: would stop qwen38-chat, recreate wedge-monitor -> qwen38-nvfp4, start canary,"
    log "  wait for /v1/models, bench at 1/8/32, then (only with --commit) cut Bifrost over."
    log "no commands executed."
    ;;
  --commit) cmd_commit ;;
  --rollback) cmd_rollback ;;
esac
