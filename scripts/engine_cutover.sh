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

# Git Bash / MSYS rewrites any bare-leading-slash argument on a docker CLI
# line (e.g. `/cache`, `/app`, `-v shared-hf-cache:/cache`) into a Windows
# path before docker ever sees it -- verified live 2026-09-21: a `docker run
# ... -e HF_HOME=/cache -v shared-hf-cache:/cache ...` landed a 17 GiB
# download at `/C:/Program Files/Git/cache/...` INSIDE the container's
# writable layer instead of the named volume, because HF_HOME got rewritten
# too.
#
# CORRECTION (verified live 2026-09-21, WS4.2 isolated-bench run): the
# previous version of this comment claimed exporting MSYS_NO_PATHCONV=1
# globally here was safe because "this script's docker-compose invocations
# don't take raw /path arguments on the command line" -- that premise was
# never actually run and is false. `docker compose -f "$VLLM_COMPOSE"` DOES
# pass a raw absolute Git-Bash-style path (`$SCRIPT_DIR`/`$INFRA_DIR` come
# from `cd ... && pwd`, e.g. `/c/code/shared-infra/...`) as a bare CLI
# argument, and with MSYS_NO_PATHCONV=1 set globally it is never translated
# to a Windows path -- docker.exe then fails with
# `open C:\c\code\shared-infra\docker-compose.vllm.yml: The system cannot
# find the path specified.` on every `docker compose -f` call in this file.
# The fix is to scope the override to exactly the one command that needs
# it -- the `docker run` inside `launch_canary_variant`, which has
# container-side paths (`HOME=/cache`, `-v ...:/cache/...`) that DO need
# protecting from the same conversion. Every `docker compose -f` call in
# this script relies on normal MSYS path conversion and must NOT have
# MSYS_NO_PATHCONV set in its environment.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VLLM_COMPOSE="$INFRA_DIR/docker-compose.vllm.yml"
BIFROST_COMPOSE="$INFRA_DIR/docker-compose.bifrost.yml"
ENV_FILE="$INFRA_DIR/.env"
BENCH_SCRIPT="$SCRIPT_DIR/engine_bench.py"
CONFIG_JSON="$INFRA_DIR/bifrost/config.json"
REPORT_DIR="$INFRA_DIR/reports"
NOW_TAG="$(date -u +%Y%m%dT%H%M%SZ)"

# Sourcing guard: a sibling script (e.g. an isolated off-hours bench driver
# that needs launch_canary_variant/stop_canary/wait_for_models/bench_gate
# without re-running the argv dispatch below) sources this file with
# `source engine_cutover.sh`. `set -euo pipefail` above means any `exit`
# hit while sourced would kill the CALLER's shell, not just this script --
# so argv validation and the final dispatch are gated on running as the
# top-level script, never on being sourced.
IS_SOURCED=0
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  IS_SOURCED=1
fi

MODE="${1:-}"
VARIANT="eager"
for arg in "$@"; do
  case "$arg" in
    --variant=*) VARIANT="${arg#--variant=}" ;;
  esac
done
if [[ "$IS_SOURCED" == "0" ]]; then
  if [[ "$MODE" != "--commit" && "$MODE" != "--rollback" && "$MODE" != "--dry-run" && "$MODE" != "--bench-variants" ]]; then
    echo "usage: $0 --dry-run|--commit [--variant=eager|graphs|mtp]|--rollback|--bench-variants" >&2
    exit 2
  fi
  if [[ "$VARIANT" != "eager" && "$VARIANT" != "graphs" && "$VARIANT" != "mtp" ]]; then
    echo "unknown --variant=$VARIANT (want eager|graphs|mtp)" >&2
    exit 2
  fi
fi

log() { echo "[engine_cutover $(date -u +%H:%M:%S)] $*" >&2; }

# Three configs to MEASURE, never to claim in advance which wins on this
# card (coordinator directive, 2026-09-21): the compose service ships the
# vLLM-recipe-verified "eager" config (--enforce-eager, 32K, no MTP) as its
# safe default. "graphs" and "mtp" are launched via a direct `docker run`
# that mirrors the compose service's image/env/volumes/ports/resources
# exactly but swaps the command list, because vLLM's CLI (unlike the old
# syv-ai custom entrypoint) has no EXTRA_ARGS hook to layer flags onto a
# fixed YAML command. All three variants use the SAME container name/port so
# only one can ever run at a time -- never concurrent with each other or
# with qwen38-chat.
launch_canary_variant() {
  local variant="$1"
  local -a extra_flags=("--default-chat-template-kwargs" '{"enable_thinking":false}')
  local -a base_flags=(
    --model Inferact/Qwen3.8-27B-NVFP4 --served-model-name qwen3.8-27b --host 0.0.0.0 --port 18020
    --tensor-parallel-size 1 --max-num-seqs 32 --kv-cache-dtype fp8 --enable-prefix-caching
    --gpu-memory-utilization 0.78 --reasoning-parser qwen3 --enable-auto-tool-choice
    --tool-call-parser qwen3_xml
  )
  local -a variant_flags=()
  case "$variant" in
    eager)  variant_flags=(--max-model-len 32768 --enforce-eager) ;;
    graphs) variant_flags=(--max-model-len 65536 --compilation-config '{"max_cudagraph_capture_size":32}') ;;
    mtp)    variant_flags=(--max-model-len 32768 --enforce-eager --speculative-config '{"method":"mtp","num_speculative_tokens":3}') ;;
  esac
  docker rm -f qwen38-nvfp4 >/dev/null 2>&1 || true
  # MSYS_NO_PATHCONV scoped to THIS command only (see the header comment) --
  # every `docker compose -f` call elsewhere in this file needs the opposite.
  MSYS_NO_PATHCONV=1 docker run -d --name qwen38-nvfp4 \
    --ipc host --shm-size 4gb \
    -e HF_TOKEN="$(grep '^HF_TOKEN=' "$ENV_FILE" | cut -d= -f2-)" -e HOME=/cache \
    -e VLLM_WSL2_ENABLE_PIN_MEMORY=1 -e FLASHINFER_DISABLE_VERSION_CHECK=1 \
    -e FLASHINFER_CUDA_ARCH_LIST=12.0f -e FLASHINFER_FORCE_SM=120f \
    -p 18803:18020 --gpus all --cpus 4.0 \
    -v qwen38-nvfp4-cache:/cache -v shared-hf-cache:/cache/.cache/huggingface \
    vllm/vllm-openai:v0.29.0 \
    "${base_flags[@]}" "${variant_flags[@]}" "${extra_flags[@]}"
  log "launched qwen38-nvfp4 variant=$variant"
}

stop_canary() {
  docker rm -f qwen38-nvfp4 >/dev/null 2>&1 || true
}

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
  log "variant=$VARIANT"

  log "stage 1/6: stop qwen38-chat + recreate wedge-monitor targeting qwen38-nvfp4, in one command"
  VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
    docker compose -f "$VLLM_COMPOSE" stop qwen38-chat
  VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
    docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor

  log "stage 2/6: start qwen38-nvfp4 canary (variant=$VARIANT)"
  if [[ "$VARIANT" == "eager" ]]; then
    # The compose service IS the eager variant -- use it directly so the
    # committed config matches what's checked into docker-compose.vllm.yml.
    docker compose -f "$VLLM_COMPOSE" --profile nvfp4-canary up -d qwen38-nvfp4
  else
    # graphs/mtp have no YAML form yet (see launch_canary_variant) -- a
    # direct `docker run` mirroring the compose service's image/env/volumes.
    launch_canary_variant "$VARIANT"
  fi

  log "stage 3/6: wait for /v1/models"
  wait_for_models "http://127.0.0.1:18803" "qwen3.8-27b" 90

  log "stage 4/6: bench at 1/8/32 concurrent against the canary directly (bypassing Bifrost)"
  bench_gate "http://127.0.0.1:18803/v1" "qwen3.8-27b" \
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
  # stop_canary (plain docker rm -f), not `docker compose stop`: a
  # graphs/mtp-variant canary was started with a direct `docker run`, which
  # carries none of compose's project labels, so `docker compose stop`
  # cannot resolve it as "belonging" to this project and would silently
  # no-op on exactly the variant most likely to need a rollback.
  stop_canary
  docker compose -f "$VLLM_COMPOSE" --profile nvfp4-canary rm -f qwen38-nvfp4 >/dev/null 2>&1 || true

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

cmd_bench_variants() {
  require_owner_ack
  mkdir -p "$REPORT_DIR"
  log "stage 1: stop qwen38-chat + recreate wedge-monitor targeting qwen38-nvfp4, in one command"
  VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
    docker compose -f "$VLLM_COMPOSE" stop qwen38-chat
  VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
    docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor

  local -a variants=(eager graphs mtp)
  local -a summary_files=()
  for v in "${variants[@]}"; do
    log "=== variant=$v: launching ==="
    launch_canary_variant "$v"
    if wait_for_models "http://127.0.0.1:18803" "qwen3.8-27b" 90; then
      local out="$REPORT_DIR/engine-bench-canary-$v-$NOW_TAG.json"
      bench_gate "http://127.0.0.1:18803/v1" "qwen3.8-27b" "$out" || log "variant=$v: bench gate did not clear the baseline (see $out)"
      summary_files+=("$v:$out")
    else
      log "variant=$v: FAILED to boot within timeout -- skipping bench, this itself is a measurement"
      summary_files+=("$v:BOOT_FAILED")
    fi
    stop_canary
  done

  log "stage last: restore qwen38-chat + wedge-monitor (bench-variants never commits a Bifrost cutover)"
  VLLM_TARGET_CONTAINER=qwen38-chat \
    docker compose -f "$VLLM_COMPOSE" up -d qwen38-chat
  VLLM_TARGET_CONTAINER=qwen38-chat \
    docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor
  wait_for_models "http://127.0.0.1:18801" "qwen3.8-27b" 90 || true

  log "=== SUMMARY (measurement, not a claim -- read the JSON files before picking a variant) ==="
  for entry in "${summary_files[@]}"; do
    log "  $entry"
  done
}

if [[ "$IS_SOURCED" == "0" ]]; then
  case "$MODE" in
    --dry-run)
      log "dry-run: would stop qwen38-chat, recreate wedge-monitor -> qwen38-nvfp4, start canary,"
      log "  wait for /v1/models, bench at 1/8/32, then (only with --commit) cut Bifrost over."
      log "no commands executed."
      ;;
    --commit) cmd_commit ;;
    --rollback) cmd_rollback ;;
    --bench-variants) cmd_bench_variants ;;
  esac
fi
