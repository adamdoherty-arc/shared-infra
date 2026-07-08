# CLAUDE.md — shared-infra

Rules and operational notes for Claude Code working in
`c:\code\shared-infra\`. This stack hosts the LLM gateway (Bifrost),
local model containers (vllm-chat + vllm-embed), and other
shared services used by Zero, ADA, and Legion.

## NO DEFERRING — FIX IT NOW (MANDATORY)

When the user asks for work, every issue that surfaces during that work
is part of the work. Do not defer, do not flag for follow-up, do not
write a migration plan or punch-list for "future sessions" unless the
user specifically asks for a plan rather than a fix.

Banned phrases / behaviors in this repo:
- "out of scope for today" / "out of scope here"
- "follow-up" / "next session" / "future session" / "deferred"
- "leave as a known issue" / "documented for later"
- "TODO: fix in a follow-up"
- Writing a `*_MIGRATION.md` / `*_PUNCHLIST.md` / `*_FOLLOWUPS.md` and
  handing it back instead of doing the work
- Marking a task "complete" with a "things I didn't finish" section

The rule: if you can see a bug, a regression, a stale config, a wrong
default, a known-broken probe, a 401, a 5xx, a dead service — **fix it
in this turn**. Diagnose, fix, verify, then continue with what the user
originally asked for. Side issues you spot while doing something else
get fixed in-flight.

The only legitimate stops:
- Hard external blocker (vendor API doesn't exist, hardware unplugged,
  paid SaaS with no key the user must provide)
- Genuinely irreversible destructive action (force-push to main,
  dropping a prod table) — confirm before doing those

Everything else: fix it. Don't ask. Don't write a plan. Don't hand back
a checklist with pending items. Finish the job 100%.

## Operational quick-reference

### Bifrost gateway (`shared-bifrost`)

- Host port `4445` → container port `8080`.
- OpenAI-compatible: `POST /v1/chat/completions`, `POST /v1/embeddings`.
- Model field must be `provider/model`, e.g. `moonshot/kimi-k2.6`,
  `vllm-local/qwen3-chat`, `embed-local/Qwen/Qwen3-Embedding-0.6B`.
- Auth ENFORCED (`enforce_auth_on_inference: true`). Callers must
  send `Authorization: Bearer sk-bf-...` OR `x-bf-vk: sk-bf-...`
  with a valid virtual key.
- Active providers (2026-06-11, ALL-FREE): `vllm-local` (Qwen3.5-35B-A3B
  via stable alias `qwen3-chat`; root `cyankiwi/Qwen3.6-27B-AWQ-INT4`, dense
  AWQ-Marlin INT4), `embed-local`, `nvidia-nim`, `openrouter`
  (:free curated list), `hf-router`, `groq`, `cerebras`, `mistral`,
  `gemini` (flash-only), `zai` (glm-4.7-flash), and `moonshot` — which is
  now a COMPAT SHIM over NVIDIA NIM's free Kimi K2.6 (the paid Moonshot
  account was retired; legacy `moonshot/kimi-*` callers keep working).
  Parked recipes (anthropic, minimax, paid-moonshot, gemini-pro,
  openrouter-wildcard) live in `bifrost/disabled-providers.json`.
- After ANY provider/model change: run `bifrost/sync_vk_allowlists.py`
  (VK allowlists are mirrored per-key in config.db and do NOT update from
  config.json) and restart bifrost.
- See `bifrost/README.md` for the full runbook.

### Provider state lives in TWO places

Bifrost holds provider/key config in BOTH `bifrost/config.json` (JSON
seed) AND `bifrost/config.db` (SQLite mirror). Editing the JSON does
NOT automatically deregister a provider from the SQLite mirror — you
must clean both, or the discovery loop will spam logs every ~12s
with `no valid keys found for provider: X`.

Procedure to remove a provider:
```bash
docker stop shared-bifrost
python3 -c "
import sqlite3
db = sqlite3.connect(r'C:/code/shared-infra/bifrost/config.db')
db.execute(\"DELETE FROM config_keys WHERE provider IN ('NAME',...)\")
db.execute(\"DELETE FROM config_providers WHERE name IN ('NAME',...)\")
db.commit()"
docker start shared-bifrost
```

Leave `governance_model_pricing` / `governance_model_parameters` alone
— those are Bifrost's built-in datasheet for ~400 known models, not
active registrations.

### Virtual keys

Three production virtual keys exist, one per project:

| Project | VK name      | Project env var that holds it |
|---------|--------------|-------------------------------|
| ADA     | `ada-prod`   | `BIFROST_GATEWAY_KEY` in `C:\code\ADA\.env` |
| Zero    | `zero-prod`  | `VLLM_API_KEY` + `ZERO_BIFROST_API_KEY` in `C:\code\zero\.env` |
| Legion  | `legion-prod`| `BIFROST_API_KEY` in `C:\code\Legion\.env` (+ `Legion\backend\.env`) |

The PUT endpoint on `/api/governance/virtual-keys/{id}` silently drops
the `allow_all_keys` field. To grant a virtual key access to a
provider's pool of keys, update `config.db` directly:
```
UPDATE governance_virtual_key_provider_configs SET allow_all_keys=1
  WHERE virtual_key_id='<uuid>';
```
(Stop bifrost first, restart after the update.)

### Local chat backend (`vllm-chat`)

- Host port `18801` → container port `8000`.
- Model: `cyankiwi/Qwen3.6-27B-AWQ-INT4` (dense 27B, AWQ-Marlin INT4) on
  vLLM `v0.23.0-cu129`, ctx 16384. (`llama-cpp-chat` at 18800 was retired
  2026-05-17 — its block is `profile: retired` in `docker-compose.vllm.yml`.)
- Served canonical `Qwen3.5-35B-A3B`; alias `qwen3-chat` → `Qwen3.5-35B-A3B`
  (Bifrost rewrites at the gateway).

### Local embed backend (`vllm-embed`)

- Host port `8001` → container port `8001`.
- Model: Qwen3-Embedding-0.6B.

### Bifrost Prometheus metrics (`bifrost-metrics`)

- Host port `9102` → container port `9100`.
- The vendor `maximhq/bifrost:v1.5.0` image doesn't ship the upstream
  prometheus plugin, so this sidecar reads `bifrost/logs.db` (which
  Bifrost writes natively) and re-exports as Prometheus metrics on
  `/metrics`. Source in `bifrost-metrics-exporter/`.
- Both `ada-prometheus` and `legion-prometheus` scrape via
  `host.docker.internal:9102` (their compose blocks have
  `extra_hosts: host.docker.internal:host-gateway`). The bifrost job is
  visible in each Prometheus UI at `/targets`.
- Exposed metrics:
  - `bifrost_requests_total{provider, model, status, request_type}`
  - `bifrost_request_latency_ms_bucket{provider, model, request_type}`
  - `bifrost_prompt_tokens_total` / `bifrost_completion_tokens_total`
  - `bifrost_cost_usd_total` (computed from Bifrost's pricing datasheet)
  - `bifrost_active_providers`, `bifrost_active_virtual_keys` (gauges)
  - `bifrost_logs_db_bytes`, `bifrost_exporter_*` (self-meta)
- The exporter bootstraps its cursor to MAX(ROWID) on first start, so
  historical rows don't pollute Prometheus rate() calculations.
- Sidecar uses ~50 MB RAM, ~0.05 CPU. Re-builds in ~30 s.

### Retired

- `shared-litellm` at port 4444 — retired 2026-05-14. Service block
  deleted from `docker-compose.vllm.yml`, config archived to
  `.deleted-2026-05-14/litellm_config.yaml`. If you ever need an
  emergency rollback, the last known-good block is in git history
  prior to 2026-05-14.
