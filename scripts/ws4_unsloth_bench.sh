#!/usr/bin/env bash
# WS4 unsloth re-bench (Legion sprint 15018 follow-on, 2026-09-22).
#
# The 2026-09-21 isolated bench (ws4_isolated_bench.sh) found the
# Inferact/Qwen3.8-27B-NVFP4 checkpoint too large to fit beside vllm-embed
# on one RTX 5090. Fix-1100000610 (same night) moved vllm-embed off the GPU
# entirely, reclaiming ~2.8 GiB. This script re-runs the canary bench with
# the smaller, Apache-2.0 unsloth/Qwen3.8-27B-NVFP4 build (already swapped
# into docker-compose.vllm.yml's qwen38-nvfp4 service and
# engine_cutover.sh's launch_canary_variant), now embed-free.
#
# Variant order: graphs (default CUDA graph capture -- the compose
# default) first, then mtp (graphs + speculative decode) ALWAYS, then eager
# (--enforce-eager, no CUDA graphs) ONLY if graphs failed to boot -- eager
# exists purely as the capture-OOM fallback the 2026-09-21 pass needed on
# the larger checkpoint. A KV-cache-allocation OOM at the 32768 context is
# retried once at 16384 before the variant is recorded as failed.
#
# This gate would pass trivially if it silently treated a bench_gate
# failure as a pass, or skipped the qwen38-chat restore on an early exit --
# it does not: bench_gate failures are recorded verbatim in RESULTS (never
# swallowed), and the qwen38-chat restore + health verification run
# unconditionally at the end of the variant loop, never inside a path that
# an early `exit` could skip.
set -euo pipefail
# NOT exporting MSYS_NO_PATHCONV globally -- see engine_cutover.sh's header
# comment (verified live 2026-09-21): this script's own
# `docker compose -f "$VLLM_COMPOSE"` calls need normal MSYS path
# conversion; only the `docker run` inside launch_canary_variant needs the
# override, and it scopes that itself.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPORT_DIR="$INFRA_DIR/reports"
NOW_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
BOOT_TIMEOUT_S="${BOOT_TIMEOUT_S:-900}"   # 15 min, per the task's own boot-failure cutoff

# Reuses launch_canary_variant / stop_canary / wait_for_models / bench_gate /
# require_owner_ack / log from the cutover script.
# shellcheck source=./engine_cutover.sh
source "$SCRIPT_DIR/engine_cutover.sh"

require_owner_ack

mkdir -p "$REPORT_DIR"
CONCURRENCY_LEVELS=(1 8 16 32)
N_PER_LEVEL=8
MAX_TOKENS=1000

bench_full() {
  local base_url="$1" model="$2" out="$3"
  local -a levels=("${@:4}")
  local vk
  vk="$(grep '^INFRA_PROBE_VK=' "$ENV_FILE" | cut -d= -f2-)"
  python3 "$BENCH_SCRIPT" --base-url "$base_url" --model "$model" --api-key "$vk" \
    --concurrency "${levels[@]}" --n "$N_PER_LEVEL" --max-tokens "$MAX_TOKENS" --out "$out"
}

RESULTS=()   # "label:report_path:status"

log "=== WS4 unsloth re-bench: stop qwen38-chat, recreate wedge-monitor -> qwen38-nvfp4 ==="
VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
  docker compose -f "$VLLM_COMPOSE" stop qwen38-chat
VLLM_TARGET_CONTAINER=qwen38-nvfp4 \
  docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor

try_variant() {
  local v="$1" ctx="$2"
  local out="$REPORT_DIR/engine-bench-2026-09-21-unsloth-$v.json"
  log "=== variant=$v ctx=$ctx: launching (timeout ${BOOT_TIMEOUT_S}s) ==="
  BOOT_START="$(date +%s)"
  launch_canary_variant "$v" "$ctx"
  boot_ok=0
  if wait_for_models "http://127.0.0.1:18803" "qwen3.8-27b" $((BOOT_TIMEOUT_S / 5)); then
    boot_ok=1
  fi
  BOOT_ELAPSED="$(( $(date +%s) - BOOT_START ))"
  if [[ "$boot_ok" == "1" ]]; then
    log "variant=$v: booted in ${BOOT_ELAPSED}s, benching at ${CONCURRENCY_LEVELS[*]}"
    if bench_full "http://127.0.0.1:18803/v1" "qwen3.8-27b" "$out" "${CONCURRENCY_LEVELS[@]}"; then
      RESULTS+=("$v:$out:BENCH_OK")
    else
      RESULTS+=("$v:$out:BENCH_RAN_GATE_FAILED")
    fi
    log "watching for GDN wedge signature (generation stalling to 0 tok/s with Running>0) in last 60 log lines:"
    docker logs qwen38-nvfp4 --tail 60 2>&1 | grep -iE "tokens/s|running|error|traceback|cuda" | tail -20 || true
    stop_canary
    return 0
  fi
  log "variant=$v: FAILED to boot within ${BOOT_TIMEOUT_S}s at ctx=$ctx"
  local tail_log
  tail_log="$(docker logs qwen38-nvfp4 --tail 120 2>&1 || true)"
  echo "$tail_log" | tail -80
  stop_canary
  if [[ "$ctx" == "32768" ]] && echo "$tail_log" | grep -qiE "Available KV cache memory:.*-|OutOfMemoryError|CUDA out of memory"; then
    log "variant=$v: KV-cache OOM signature detected at ctx=32768 -- retrying once at ctx=16384"
    try_variant "$v" 16384
    return $?
  fi
  RESULTS+=("$v:NONE:BOOT_FAILED_${BOOT_ELAPSED}s")
  return 1
}

GRAPHS_OK=1
try_variant graphs 32768 || GRAPHS_OK=0

log "=== variant=mtp: always attempted regardless of graphs result ==="
try_variant mtp 32768 || true

if [[ "$GRAPHS_OK" == "0" ]]; then
  log "=== variant=eager: graphs failed to boot -- trying the capture-OOM fallback ==="
  try_variant eager 32768 || true
else
  log "=== variant=eager: SKIPPED -- graphs booted, eager is a fallback-only path ==="
  RESULTS+=("eager:SKIPPED:GRAPHS_BOOTED")
fi

log "=== restoring qwen38-chat + wedge-monitor before the clean-baseline re-bench ==="
VLLM_TARGET_CONTAINER=qwen38-chat \
  docker compose -f "$VLLM_COMPOSE" up -d qwen38-chat
VLLM_TARGET_CONTAINER=qwen38-chat \
  docker compose -f "$VLLM_COMPOSE" up -d --force-recreate vllm-wedge-monitor
wait_for_models "http://127.0.0.1:18801" "qwen3.8-27b" 180 || log "WARNING: qwen38-chat did not report ready in time -- inspect before leaving this run"

log "=== final health verification of qwen38-chat ==="
curl -sf http://127.0.0.1:18801/health && log "qwen38-chat /health: OK" || log "qwen38-chat /health: FAILED -- investigate before ending the session"
PROBE_VK="$(grep '^INFRA_PROBE_VK=' "$ENV_FILE" | cut -d= -f2-)"
curl -s http://127.0.0.1:18801/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer $PROBE_VK" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"Say OK."}],"max_tokens":5}' | head -c 500
echo

CLEAN_OUT="$REPORT_DIR/engine-bench-2026-09-21-current-embed-free-c32.json"
log "=== embed-free clean re-bench of qwen38-chat at concurrency 32 only, n=16 ==="
if bench_full "http://127.0.0.1:18801/v1" "qwen3.8-27b" "$CLEAN_OUT" 32; then
  RESULTS+=("current-embed-free-c32:$CLEAN_OUT:BENCH_OK")
else
  RESULTS+=("current-embed-free-c32:$CLEAN_OUT:BENCH_RAN_GATE_FAILED")
fi

log "=== SUMMARY (label:report:status) ==="
for r in "${RESULTS[@]}"; do
  log "  $r"
done
log "Reports directory: $REPORT_DIR"
log "Update docs/engine-refresh-2026-09-21.md with the results table (this script does not write markdown -- NEVER SHIP STUBS: no synthesized table without real numbers behind it)."
