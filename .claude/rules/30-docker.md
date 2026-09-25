# 30 — Docker (always-loaded)

## Compose file ownership

| File | Owns |
|---|---|
| `docker-compose.bifrost.yml` | `shared-bifrost`, `bifrost-metrics`, `bifrost-autoheal`, `bifrost-logs-pruner` (parked, `sqlite-rollback` profile) |
| `docker-compose.vllm.yml` | `qwen38-chat`, `vllm-embed`, `vllm-wedge-monitor`, `vllm-autoheal`, plus every retired/rollback engine profile |
| `docker-compose.observability.yml` | Prometheus, Alertmanager, Grafana, Loki, Tempo, otelcol, cadvisor, dcgm |
| `docker-compose.control.yml` | `shared-infra-control` (infractl) |
| `docker-compose.freellmapi.yml` | `shared-freellmapi` aggregator |
| `docker-compose.searxng.yml` | SearXNG (8091) |
| `docker-compose.otelcol.yml` | OpenTelemetry collector (standalone; also referenced from observability) |

Validate every root compose file (skipping any `.bak*` variant) with
`docker compose -f <file> config -q` — this is stage (a) of `scripts/
gate.py` and catches YAML/interpolation breakage before it reaches a
restart.

## The binding Bifrost restart sequence (MANDATORY)

**Never run a bare `docker restart shared-bifrost` / `qwen38-chat` /
`vllm-embed`, and never `docker compose up` in a way that recreates them.**
`.claude/hooks/bifrost_restart_gate.py` (PreToolUse on Bash) blocks these
mechanically — see that file's docstring for the failure classes it
exists to prevent. Two ways through the gate:

1. **The sanctioned script** (do this by default):
   ```bash
   bash scripts/bifrost_restart.sh
   ```
   Sequence: stop `bifrost-autoheal` -> stop `shared-bifrost` -> `python
   bifrost/sync_vk_allowlists.py` -> start `shared-bifrost` -> poll
   `http://127.0.0.1:4445/health` up to 120s -> authenticated 1-token
   completion probe against `vllm-local` -> start `bifrost-autoheal`.
   Exits nonzero on any step failure; a trap always restores both
   containers even on failure.
2. **Explicit operator override**, for a deliberate manual sequence
   outside the script: prefix the command with `INFRA_RESTART_OK=1`. Use
   this only when you understand why the script's sequence doesn't apply
   (e.g. a debugging session where you intentionally want autoheal to
   stay down) — it is an escape hatch, not a routine path.

`qwen38-chat` / `vllm-embed` restarts (wedge recovery, config bump) should
go through `infractl`'s heal action once WS7 item 6 lands (single restart
authority + ledger row); until then, use the same
`INFRA_RESTART_OK=1` override and log the reason in `docs/DECISIONS.md` or
`LESSONS_LEARNED.md` if it was a recovery from a real incident.

## Healthcheck conventions

- Bifrost: `wget` against `/health` only, never the aggregate `/v1/models`
  route (it can block behind provider discovery while inference stays
  available — see the comment at `docker-compose.bifrost.yml:152-156`).
- Any container health check hitting `localhost` from inside a container
  where the process binds IPv4-only: use `127.0.0.1` explicitly — busybox
  `wget` resolves `localhost` to `::1` first and hangs
  (`docker-compose.bifrost.yml:236-239`).
- Every service needs `restart: unless-stopped` (or `always`) explicitly —
  a service with no `restart:` directive does not survive a host/Docker
  daemon restart, which took down all 10 shared containers simultaneously
  on 2026-08-04 before this was caught.

## Resource limits are required

Every long-running service in `docker-compose.vllm.yml` and
`docker-compose.bifrost.yml` should carry `deploy.resources.limits` (mem +
cpu) — the `vllm-embed` `--parallel 4` regression (WS1) manifested partly
as the container running at 5.07G/6G with no ceiling forcing an earlier,
cleaner failure. If you add a service without a limit, that's a finding to
fix in the same pass (NO DEFERRING), not a note for later.

## Compose profiles table

| Profile | Purpose | File |
|---|---|---|
| `nemotron-rollback` | Previous chat engine (Nemotron-3.5-Lightning NVFP4) kept cold behind Pass-9's swap to `qwen38-chat` | `docker-compose.vllm.yml` |
| `embed-gpu-rollback` | GPU vLLM embed service, kept cold since the 2026-09-22 CPU move (Fix-1100000610) | `docker-compose.vllm.yml` |
| `nvfp4-canary` | Bench-only NVFP4 engine attempts on this card; never the default running engine | `docker-compose.vllm.yml` |
| `qwen38-prepare` | One-shot model-prepare/download step for the qwen38 engine | `docker-compose.vllm.yml` |
| `retired` | Engines retired outright (kept for compose-history reference, never started) | `docker-compose.vllm.yml` |
| `tts` | fish-speech TTS service, optional | `docker-compose.vllm.yml` |
| `sqlite-rollback` | `bifrost-logs-pruner`, cold since the Postgres log-store migration | `docker-compose.bifrost.yml` |

Start a profile explicitly: `docker compose -f docker-compose.vllm.yml
--profile <name> up -d <service>`. Never start a rollback/canary profile
alongside its live replacement without checking the VRAM budget in
`55-engines.md` first — this card is a hard single-GPU ceiling, not a
multi-tenant one.

## `docker compose up` vs `restart` — the env re-read gotcha

`docker restart` does NOT re-read `.env` or an updated compose
`environment:` block; only `docker compose up -d --force-recreate <svc>`
(or `stop` + `start` after a config change) picks up new env values. This
has bitten stale VK rotations twice (`docs/DECISIONS.md` 2026-09-15 "Who
was probing Bifrost"). If you change an env var and then verify with a
bare `docker restart`, you are verifying the OLD value.

## Never open SQLite from the host

`bifrost/config.db` and any other container-owned SQLite file must not be
opened read-write from a host Python process while the container is
running — WAL corruption has happened this way twice (`config.db-shm.*
corrupt-backup` files in `bifrost/`). Read-only (`?mode=ro` URI) is fine
for a probe script; writes go through the container or through a stopped
container + `sqlite3` CLI, never a live host connection.
