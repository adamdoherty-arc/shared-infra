# Bifrost gateway config (shared-infra)

Bifrost is the **sole** OpenAI-compatible LLM gateway for Zero, ADA, and
Legion. See `c:\code\shared-infra\docker-compose.bifrost.yml` for the
container shape and `config.json` here for the providers.

## Status: migration complete (2026-05-14)

All three projects (Zero, ADA, Legion) route 100% of LLM traffic through
Bifrost. `shared-litellm` at port 4444 was retired the same day —
container removed, service block deleted from `docker-compose.vllm.yml`,
`litellm/config.yaml` archived to `shared-infra/.deleted-2026-05-14/`.

**Active providers** (as of 2026-06-11, ALL-FREE — no paid providers; verify with `curl localhost:4445/api/governance/virtual-keys`):
- `vllm-local`  → `vllm-chat:8000`  (cyankiwi/Qwen3.6-27B-AWQ-INT4 — dense 27B, AWQ-Marlin INT4 on vLLM v0.23.0-cu129, 16K ctx, TOOLS ENABLED via qwen3_xml parser; served canonical `Qwen3.5-35B-A3B`, stable alias `qwen3-chat`, legacy aliases `Qwen3-32B-AWQ` / `Qwen3.6-35B-A3B` — all map to the served model. Both the GPTQ-Int4 (MoE) and NVFP4 variants were tried and RETIRED — unstable on sm_120/Blackwell)
- `embed-local` → `vllm-embed:8001` (Qwen3-Embedding-0.6B)
- `moonshot`    → **COMPAT SHIM over `https://integrate.api.nvidia.com`** (the paid api.moonshot.ai account was retired 2026-06-11 — Kimi K2.6 is free on NVIDIA NIM. The provider name survives so legacy `moonshot/kimi-*` callers/DB rows keep working; every kimi variant aliases to `moonshotai/kimi-k2.6`)
- `nvidia-nim`  → `https://integrate.api.nvidia.com` (44 models incl. z-ai/glm-5.1, moonshotai/kimi-k2.6, deepseek-v4-pro/flash, minimax-m2.7, nemotron-3-ultra-550b, qwen3.5-122b/397b, qwen3-next-80b — 40 RPM free. NOTE 2026-06-11: qwen3-coder-480b was DELISTED from the NIM catalog; deepseek-v4-pro/flash time out >170s — registered but not in any chain)
- `openrouter`  → `https://openrouter.ai/api` (17 `:free` models incl. nemotron-3-ultra-550b (1M ctx), qwen3-coder (1M ctx), qwen3-next-80b, gemma-4-31b/26b-a4b, gpt-oss-120b/20b, poolside laguna, plus `openrouter/free` auto-router. $10 lifetime topup applied 2026-06-11 → 1,000 free req/day. KNOWN ISSUE: `qwen/qwen3-coder:free` returns 404 until the account's privacy setting "allow free endpoints that may train on inputs" is enabled)
- `hf-router`   → `https://router.huggingface.co` (DeepSeek, Kimi, MiniMax, Qwen, GLM, gpt-oss — ~100K free credits/month, thin; use as deep fallback only)
- `groq` / `cerebras` / `mistral` — perpetual free tiers (gpt-oss-120b at 200K TPD on groq; ~1M tok/day on cerebras; ~1B tok/month on mistral — incl `mistral-large-latest` + `magistral-medium-latest` added 2026-06-11 and verified through the gateway; magistral returns content as a parts-array, callers must join text parts)
- `gemini`      → FLASH-ONLY (gemini-3.5-flash, gemini-3-flash-preview, 3.1-flash-lite — 1,500 req/day free. Pro previews are billing-gated since ~May 2026 and were removed)
- `zai`         → `https://api.z.ai` (glm-4.7-flash, 203K ctx, perpetually free. Uses `request_path_overrides` to hit `/api/paas/v4/chat/completions` — Z.ai doesn't serve the standard `/v1` path)

**Per-project affinity (2026-06-11)** — so the three projects don't drain each other's free rate limits: Legion→NVIDIA NIM (GLM-5.1 reasoning+code), ADA→Kimi K2.6 via NIM, Zero→Gemini Flash + Groq. Local vLLM is the shared first rung everywhere; freellm/auto is the shared emergency tail.

Re-enabled 2026-05-27 (nvidia-nim/openrouter/hf-router) — the shared LLM router
(`c:\code\shared-llm-router`, consumed by Legion + ADA) routes per-task fallback
chains across these with Feature-540 contention promotion (local → free cloud).

**Parked** in `disabled-providers.json` as ready-to-restore recipes: anthropic,
minimax, gemini, and (added 2026-05-31, perpetual-free) **groq, cerebras, mistral**.
To add a free lane, drop a key into `shared-infra/.env`, add the env passthrough
to `docker-compose.bifrost.yml`, copy the block into `config.json`, restart, and
widen the per-project virtual-key scopes. Free-tier facts + signup URLs are inline
in each parked block's `_comment` / the `_README`.

**Auth: ENFORCED** (`client.enforce_auth_on_inference: true` since
2026-05-14). Callers must send `Authorization: Bearer sk-bf-...` with a
valid virtual key, OR the equivalent `x-bf-vk: sk-bf-...` header.

## Virtual keys

Three virtual keys exist, one per project. Each has all 3 providers
allowed with `allow_all_keys=true` so they can route to vllm-local,
embed-local, and moonshot. Created via the governance API on
2026-05-14:

| Project | Virtual key name | Where the key lives |
|---|---|---|
| ADA     | `ada-prod`      | `C:\code\ADA\.env` → `BIFROST_GATEWAY_KEY=sk-bf-...` |
| Zero    | `zero-prod`     | `C:\code\zero\.env` → `VLLM_API_KEY` + `ZERO_BIFROST_API_KEY` |
| Legion  | `legion-prod`   | `C:\code\Legion\.env` + `Legion\backend\.env` → `BIFROST_API_KEY` |

Rotate by hitting the governance API:
```
# List
curl http://localhost:4445/api/governance/virtual-keys

# Update (preserve provider_configs, change description, etc.)
curl -X PUT http://localhost:4445/api/governance/virtual-keys/<id> \
  -H 'Content-Type: application/json' \
  -d '{"name":"...","description":"...","provider_configs":[...]}'

# Delete
curl -X DELETE http://localhost:4445/api/governance/virtual-keys/<id>
```

NOTE: the PUT endpoint silently drops the `allow_all_keys` field. To
flip it you must `UPDATE governance_virtual_key_provider_configs SET
allow_all_keys=1 WHERE virtual_key_id=...` directly in `config.db`
(stop bifrost first, then start it after the update).

## Calling convention

Bifrost requires the model field to be `provider/model`, not just `model`.

```
POST http://shared-bifrost:8080/v1/chat/completions
{
  "model": "vllm-local/qwen3-chat",
  "messages": [...]
}

POST http://shared-bifrost:8080/v1/embeddings
{
  "model": "embed-local/Qwen/Qwen3-Embedding-0.6B",
  "input": "hello"
}
```

From the host: substitute `localhost:4445` for `shared-bifrost:8080`.

## Open issue (track in Phase 2)

Qwen3.6 emits its chain-of-thought into a separate `reasoning` field
unless `chat_template_kwargs.enable_thinking=false` is passed on every
call. LiteLLM's config does this via `litellm_params.extra_body`. The
Bifrost equivalent is per-key `extra_body` (or `request_path_overrides`)
— wire it before any caller flips to Bifrost in production. Until then,
legion's `vllm_client.py` injects the kwarg client-side AND falls back
to `reasoning_content` so callers always get a non-empty `content`.

## Cloud provider key health (verified 2026-05-14)

Providers with no active key are NOT in `config.json` — their blocks live in
`disabled-providers.json` as ready-to-restore recipes. This prevents
Bifrost's per-minute model-discovery loop from spamming logs with
key-validation failures.

| Provider   | Status                  | Action |
|------------|-------------------------|--------|
| vllm-local | ACTIVE                  | Local vllm-chat:8000 — Qwen3.5-35B-A3B (root `cyankiwi/Qwen3.6-27B-AWQ-INT4`; alias `qwen3-chat`) |
| embed-local| ACTIVE                  | Local vllm-embed:8001 — Qwen3-Embedding-0.6B |
| moonshot   | ACTIVE (verified 200)   | Kimi at `https://api.moonshot.ai`. Models: kimi-k2.6, kimi-k2.5, kimi-k2.6-thinking, moonshot-v1-32k, moonshot-v1-128k. `list_models` is not supported by Moonshot's API — Bifrost falls back to a static datasheet at startup and logs an error line; that error is one-shot and benign. |
| minimax    | DISABLED (user removed) | Block in `disabled-providers.json`. Key still works upstream — blank `MINIMAX_API_KEY` in shared-infra/.env before re-enabling. |
| openrouter | DISABLED (user removed) | Block in `disabled-providers.json`. Key still in upstream account — blank `OPENROUTER_API_KEY` in shared-infra/.env before re-enabling. |
| anthropic  | DISABLED (empty key)    | Block in `disabled-providers.json`. Add `ANTHROPIC_API_KEY` to shared-infra/.env, restore block, add env passthrough in `docker-compose.bifrost.yml`, restart Bifrost. |
| gemini     | DISABLED (user removed) | Same flow — replace `GEMINI_API_KEY`. |

Re-enabling a disabled provider is a 4-step dance: (1) refresh key in
`shared-infra/.env`, (2) add the env passthrough back to
`docker-compose.bifrost.yml` (the active env list is stripped to
`KIMI_API_KEY` only), (3) restore the provider block from
`disabled-providers.json` into `config.json`, (4) restart bifrost. The
`_README` in `disabled-providers.json` keeps this written down.

Env-var syntax: Bifrost expands `${VAR_NAME}` for native providers and
`env.VAR_NAME` for custom OpenAI-compatible providers (moonshot, etc).
Both forms work for the providers we use.

## Stale state in `config.db` (note 2026-05-14)

Bifrost holds a SQLite mirror of provider/key state in `config.db` next to
`config.json`. Removing a block from `config.json` does NOT delete the
corresponding `config_providers` / `config_keys` rows — Bifrost keeps the
provider registered and continues to poll its `list_models` endpoint
every ~12 seconds, which floods logs with `no valid keys found` warnings.
If you remove a provider from `config.json`, also clear its rows from
the SQLite mirror:

```bash
docker stop shared-bifrost
python3 -c "import sqlite3;db=sqlite3.connect('config.db');\
db.execute(\"DELETE FROM config_keys WHERE provider IN ('NAME',...)\");\
db.execute(\"DELETE FROM config_providers WHERE name IN ('NAME',...)\");\
db.commit()"
docker start shared-bifrost
```

The `governance_model_pricing` / `governance_model_parameters` tables hold
Bifrost's built-in cost+param datasheet for hundreds of known models —
leave them alone, they're reference data, not active registrations.

**Virtual-key model allowlists are ALSO mirrored in config.db** (learned
2026-06-11): `governance_virtual_key_provider_configs.allowed_models` holds a
per-VK copy of each provider's model list. Editing `config.json` updates the
provider catalog but NOT the VK allowlists — new models 403/404 for every
project until the VK rows are refreshed. After any provider/model change run
`bifrost/sync_vk_allowlists.py` (syncs every VK's allowed_models verbatim
from `config_keys.models_json` and inserts rows for new providers; covers
all 4 VKs incl. fortressos-prod), then restart bifrost.

## Known follow-ups (post-2026-05-14 migration)

These don't block the new policy but should be cleaned up next time
each project is in active dev:

1. ~~**ADA's KimiClient passes `temperature=0.1` by default**~~ —
   **FIXED (verified 2026-06-11)**: `backend/infrastructure/ai_client.py`
   clamps kimi-k2.* to `temperature=1.0` (see `effective_temperature`
   around line 1404).
2. ~~**Zero's Reachy `bifrost-local-qwen` probe shows DOWN**~~ —
   **FIXED (verified 2026-06-11)**: `bifrost_provider._message_content`
   falls back to `reasoning`/`reasoning_content` when `content` is empty,
   and the status probe only checks HTTP success.
3. **Prometheus plugin not present** in the vendor `maximhq/bifrost:v1.5.0`
   image — startup warns `prometheus plugin not found`. The internal
   `logs.db` (~2.4 GB so far) captures every request with model, latency,
   cost, error_details, full request/response bodies. For RED metrics in
   the existing legion/ada Grafana stacks, either query `logs.db`
   directly OR build a custom Bifrost image with the prometheus plugin
   compiled in.
4. **`legion-litellm` service block** in `c:\code\Legion\docker-compose.yml`
   was deleted by the migration; `legion/docker/litellm/config.yaml`
   moved to `.deleted-2026-05-14/`.

## Ops scripts (in this directory)

- `sync_vk_allowlists.py` — run after ANY config.json provider/model change
  (stop bifrost → run → start bifrost). See "Stale state in config.db".
- `smoke_all_lanes.py` — 1-shot completion against every cloud provider/model
  lane through the gateway; prints PASS/FAIL + latency per lane.
- `verify_local_model.py` — local-model suite: all served aliases, tool-call
  round-trip, 30K+ context needle, decode throughput, embeddings.

## Calling convention from the projects

Each project's code reads the virtual key from a project-specific env
var (see Virtual keys table above) and sends it as
`Authorization: Bearer <key>`. The OpenAI SDK's `api_key=` constructor
arg handles this automatically. Bifrost accepts both `Authorization:
Bearer` AND `x-bf-vk` headers for inference auth.
