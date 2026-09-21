#!/usr/bin/env bash
# WS4.2 isolated off-hours bench (Legion sprint 15018, 2026-09-21).
#
# The 13:50 ET baseline attempt inside engine-refresh-2026-09-21.md produced
# no usable numbers -- the live engine was saturated by production traffic
# and every probe timed out. This driver benches each of the three canary
# variants (eager|graphs|mtp) in ISOLATION (qwen38-chat stopped, no
# production contention) at concurrency 1/8/16/32 with n=8+ per level and
# max_tokens=1000, then benches the CURRENT engine (qwen38-chat) restored at
# the same levels, immediately, so both numbers are comparable and taken in
# the same off-hours window. It never runs `--commit` -- Bifrost's
# `vllm-local` route is untouched throughout, and qwen38-chat is restored
# and health-verified before this script exits either on success or on any
# failure path.
#
# This gate would pass trivially if it silently swallowed a bench_gate
# failure and reported a variant as viable anyway -- it does not: bench_gate
# failures are recorded verbatim (the JSON report, not a boolean), and the
# variant loop always moves to the next variant rather than either aborting
# the whole run or hiding the failure.
set -euo pipefail
# NOT exporting MSYS_NO_PATHCONV globally here: this script's own
# `docker compose -f "$VLLM_COMPOSE"` calls (VLLM_COMPOSE is set by the
# sourced engine_cutover.sh from `pwd`, a Git-Bash-style absolute path) need
# normal MSYS path conversion to reach docker.exe as a Windows path -- see
# the correction in engine_cutover.sh's header comment, found live during
# this exact run. The one command that needs the container-path protection
# (docker run inside launch_canary_variant) scopes it itself.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPORT_DIR="$INFRA_DIR/reports"
DOC_FILE="$INFRA_DIR/docs/engine-refresh-2026-09-21.md"
NOW_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
BOOT_TIMEOUT_S="${BOOT_TIMEOUT_S:-900}"   # 15 min, per the task's own boot-failure cutoff

# Reuses launch_canary_variant / stop_canary / wait_for_models / bench_gate /
# require_owner_ack / log from the cutover script -- see the sourcing guard
# added there so `exit` inside its argv validation never fires when sourced.
# shellcheck source=./engine_cutover.sh
source "$SCRIPT_DIR/engine_cutover.sh"

require_owner_ack

mkdir -p "$REPORT_DIR"
CONCURRENCY_LEVELS=(1 8 16 32)
N_PER_LEVEL=8
MAX_TOKENS=1000

bench_full() {
  local base_url="$1" model="$2" out="$3"
  local vk
  vk="$(grep '^INFRA_PROBE_VK=' "$ENV_FILE" | cut -d= -f2-)"
  python3 "$BENCH_SCRIPT" --base-url "$base_url" --model "$model" --api-key "$vk" \
    --concurrency "${CONCURRENCY_LEVELS[@]}" --n "$N_PER_LEVEL" --max-tokens "$MAX_TOKENS" --out "$out"
}

RESULTS=()   # "label:report_path:status"

log "=== WS4.2 isolated bench: stop qwen38-chat, recreate wedge-monitor -> qwen38-nvfp4 ==="
VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
  docker compose -f "$VLLM_COMPOSE" stop qwen38-chat
VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
  docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor

for v in eager graphs mtp; do
  log "=== variant=$v: launching (timeout ${BOOT_TIMEOUT_S}s) ==="
  BOOT_START="$(date +%s)"
  launch_canary_variant "$v"
  boot_ok=0
  if wait_for_models "http://127.0.0.1:18803" "qwen3.8-27b" $((BOOT_TIMEOUT_S / 5)); then
    boot_ok=1
  fi
  BOOT_ELAPSED="$(( $(date +%s) - BOOT_START ))"
  if [[ "$boot_ok" == "1" ]]; then
    out="$REPORT_DIR/engine-bench-2026-09-21-$v.json"
    log "variant=$v: booted in ${BOOT_ELAPSED}s, benching at ${CONCURRENCY_LEVELS[*]}"
    if bench_full "http://127.0.0.1:18803/v1" "qwen3.8-27b" "$out"; then
      RESULTS+=("$v:$out:BENCH_OK")
    else
      RESULTS+=("$v:$out:BENCH_RAN_GATE_FAILED")
    fi
    log "watching for GDN wedge signature (generation stalling to 0 tok/s with Running>0) in last 60 log lines:"
    docker logs qwen38-nvfp4 --tail 60 2>&1 | grep -iE "tokens/s|running|error|traceback|cuda" | tail -20 || true
  else
    log "variant=$v: FAILED to boot within ${BOOT_TIMEOUT_S}s -- recording boot failure, moving on"
    log "last 80 log lines from the failed boot:"
    docker logs qwen38-nvfp4 --tail 80 2>&1 | tail -80 || true
    RESULTS+=("$v:NONE:BOOT_FAILED_${BOOT_ELAPSED}s")
  fi
  stop_canary
done

log "=== restoring qwen38-chat + wedge-monitor before the clean-baseline bench ==="
VLLM_TARGET_CONTAINER=qwen38-chat \
  docker compose -f "$VLLM_COMPOSE" up -d qwen38-chat
VLLM_TARGET_CONTAINER=qwen38-chat \
  docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor
wait_for_models "http://127.0.0.1:18801" "qwen3.8-27b" 90 || log "WARNING: qwen38-chat did not report ready within 90 attempts -- inspect before leaving this run"

CLEAN_OUT="$REPORT_DIR/engine-bench-2026-09-21-current-clean.json"
log "=== clean baseline: benching CURRENT engine (qwen38-chat) at the same levels while ADA is capped at ~10 concurrent admissions ==="
RUNNING_BEFORE="$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from engine_bench import fetch_num_requests_running
v = fetch_num_requests_running('http://127.0.0.1:18801/v1')
print(v if v is not None else 'unavailable')
")"
log "vllm:num_requests_running on qwen38-chat immediately before the clean bench: $RUNNING_BEFORE"
if bench_full "http://127.0.0.1:18801/v1" "qwen3.8-27b" "$CLEAN_OUT"; then
  RESULTS+=("current-clean:$CLEAN_OUT:BENCH_OK")
else
  RESULTS+=("current-clean:$CLEAN_OUT:BENCH_RAN_GATE_FAILED")
fi

log "=== final health verification of qwen38-chat ==="
curl -sf http://127.0.0.1:18801/health && log "qwen38-chat /health: OK" || log "qwen38-chat /health: FAILED -- investigate before ending the session"
PROBE_VK="$(grep '^INFRA_PROBE_VK=' "$ENV_FILE" | cut -d= -f2-)"
curl -s http://127.0.0.1:18801/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer $PROBE_VK" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"Say OK."}],"max_tokens":5}' | head -c 500
echo

log "=== SUMMARY (label:report:status) ==="
for r in "${RESULTS[@]}"; do
  log "  $r"
done
log "Reports directory: $REPORT_DIR"
log "Update $DOC_FILE with the results table (this script does not write markdown itself -- the caller reads the JSON and writes the summary, per NEVER SHIP STUBS: no synthesized table without real numbers to back it)."
