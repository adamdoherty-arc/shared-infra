#!/usr/bin/env bash
# =============================================================================
# bifrost_restart.sh -- the ONLY sanctioned way to restart shared-bifrost.
# =============================================================================
#
# `.claude/hooks/bifrost_restart_gate.py` blocks bare `docker restart
# shared-bifrost` / `qwen38-chat` / `vllm-embed` and any `docker compose up`
# that would recreate them, because a bare restart skips two things this
# script does not skip:
#
#   1. Bifrost's provider/key config lives in BOTH `bifrost/config.json`
#      (declarative seed) and `bifrost/config.db` (SQLite mirror). Editing
#      the JSON and restarting without `sync_vk_allowlists.py` first leaves
#      virtual keys 403'ing against providers whose allowlist never picked
#      up the edit (CLAUDE.md "Provider state lives in TWO places").
#   2. `bifrost-autoheal` polls shared-bifrost continuously and can race a
#      manual restart -- it needs to be stopped before the gateway goes
#      down and only restarted once the gateway is verified healthy again.
#
# Sequence (binding, from CLAUDE.md + the WS7 operating-pattern plan):
#   stop bifrost-autoheal -> stop shared-bifrost -> sync_vk_allowlists.py ->
#   start shared-bifrost -> poll /health up to 120s -> authenticated
#   1-token completion probe against vllm-local -> start bifrost-autoheal.
#
# Exit nonzero on ANY failure. The trap ALWAYS restarts shared-bifrost and
# bifrost-autoheal, even on failure, so a broken step never leaves the
# gateway down or autoheal permanently stopped.
#
# Do NOT run this script from an agent session without explicit operator
# intent -- it restarts a production gateway serving ADA/Legion/A-finance.
# Usage: bash scripts/bifrost_restart.sh
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

HEALTH_URL="http://127.0.0.1:4445/health"
POLL_TIMEOUT_S=120
POLL_INTERVAL_S=3

log() { printf '[bifrost_restart] %s\n' "$1"; }
die() { printf '[bifrost_restart] FATAL: %s\n' "$1" >&2; exit 1; }

# --- resolve the probe VK without ever echoing it ---------------------------
_load_probe_vk() {
    if [ -n "${INFRA_PROBE_VK:-}" ]; then
        return 0
    fi
    if [ -f "$REPO_ROOT/.env" ]; then
        local line
        line="$(grep -E '^INFRA_PROBE_VK=' "$REPO_ROOT/.env" | tail -n1)"
        if [ -n "$line" ]; then
            INFRA_PROBE_VK="${line#INFRA_PROBE_VK=}"
            INFRA_PROBE_VK="$(printf '%s' "$INFRA_PROBE_VK" | tr -d '
"' | tr -d "'")"
            export INFRA_PROBE_VK
        fi
    fi
}

# --- trap: unconditionally bring the gateway + autoheal back --------------
STARTED_BACK=0
_restore() {
    if [ "$STARTED_BACK" = "1" ]; then
        return 0
    fi
    STARTED_BACK=1
    log "trap: ensuring shared-bifrost and bifrost-autoheal are running"
    docker start shared-bifrost >/dev/null 2>&1
    docker start bifrost-autoheal >/dev/null 2>&1
}
trap _restore EXIT

main() {
    log "step 1/6: stop bifrost-autoheal"
    docker stop bifrost-autoheal >/dev/null 2>&1 || log "  (bifrost-autoheal already stopped or missing)"

    log "step 2/6: stop shared-bifrost"
    docker stop shared-bifrost >/dev/null 2>&1 || die "docker stop shared-bifrost failed"

    log "step 3/6: sync VK allowlists (config.json -> config.db)"
    if [ -f "$REPO_ROOT/bifrost/sync_vk_allowlists.py" ]; then
        python "$REPO_ROOT/bifrost/sync_vk_allowlists.py" || die "sync_vk_allowlists.py failed"
    else
        log "  WARNING: bifrost/sync_vk_allowlists.py not found, skipping (config.db may drift)"
    fi

    log "step 4/6: start shared-bifrost"
    docker start shared-bifrost >/dev/null 2>&1 || die "docker start shared-bifrost failed"

    log "step 5/6: poll ${HEALTH_URL} (timeout ${POLL_TIMEOUT_S}s)"
    local waited=0
    local healthy=0
    while [ "$waited" -lt "$POLL_TIMEOUT_S" ]; do
        if curl -sf -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
            healthy=1
            break
        fi
        sleep "$POLL_INTERVAL_S"
        waited=$((waited + POLL_INTERVAL_S))
    done
    [ "$healthy" = "1" ] || die "shared-bifrost never reported healthy within ${POLL_TIMEOUT_S}s"
    log "  healthy after ${waited}s"

    log "step 6/6: smoke test"
    _load_probe_vk
    if [ -z "${INFRA_PROBE_VK:-}" ]; then
        die "INFRA_PROBE_VK not set (env or .env) -- cannot smoke test"
    fi
    if [ -f "$REPO_ROOT/bifrost/smoke_all_lanes.py" ]; then
        # smoke_all_lanes.py has no quick/critical-only mode (verified 2026-09-25:
        # it iterates a fixed cloud-lane MODELS list with no CLI flags), so a full
        # run here would probe every free-tier lane on every restart and burn
        # rate limit for no benefit. Run the fast, targeted check instead: an
        # authenticated 1-token completion against the critical local lane.
        log "  (smoke_all_lanes.py has no quick mode; running a targeted vllm-local probe instead)"
    fi
    probe_body='{"model":"vllm-local/qwen3-chat","messages":[{"role":"user","content":"ping"}],"max_tokens":1}'
    probe_status=$(curl -s -o /tmp/bifrost_restart_probe.$$.json -w '%{http_code}' -m 30 \
        -X POST "http://127.0.0.1:4445/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -H "x-bf-vk: ${INFRA_PROBE_VK}" \
        -d "$probe_body")
    rm -f /tmp/bifrost_restart_probe.$$.json
    unset INFRA_PROBE_VK probe_body
    if [ "$probe_status" != "200" ]; then
        die "vllm-local smoke probe returned HTTP ${probe_status} (expected 200)"
    fi
    log "  vllm-local smoke probe OK (HTTP 200)"

    # Per-consumer VK caps live in config.db; re-assert them after every
    # restart so a restored or rebuilt config.db cannot silently drop them.
    if [ -f "$REPO_ROOT/scripts/apply_vk_rate_limits.py" ]; then
        python "$REPO_ROOT/scripts/apply_vk_rate_limits.py" | sed 's/^/[bifrost_restart]   /'             || log "  WARNING: VK rate limits need attention (see output above)"
    fi

    log "restarting bifrost-autoheal"
    docker start bifrost-autoheal >/dev/null 2>&1 || die "docker start bifrost-autoheal failed"
    STARTED_BACK=1

    log "done: shared-bifrost restarted and verified healthy"
}

main "$@"
