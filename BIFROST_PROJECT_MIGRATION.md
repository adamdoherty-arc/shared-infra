# Bifrost project migration — COMPLETE (2026-05-14)

**Status: all three projects fully migrated, `shared-litellm` retired,
auth enforced.** See `bifrost/README.md` for the current operational
runbook. This file is preserved as historical record of the punch list
and known follow-ups.

## Summary of what shipped

- All cloud keys (Gemini, MiniMax, OpenRouter, Anthropic, DeepSeek,
  Groq, Grok) removed from `shared-infra/.env` AND from every project
  `.env`. Backups: `*.bak.20260514T061951`.
- `KIMI_API_KEY` centralized in `shared-infra/.env`.
- Bifrost `config.json`: vllm-local + embed-local + moonshot active.
  MiniMax / OpenRouter / Anthropic / Gemini parked in
  `disabled-providers.json`.
- Bifrost SQLite (`config.db`) cleaned of stale minimax/openrouter
  provider registrations (would otherwise spam logs every 12s).
- All three project codebases rewritten so every LLM call routes
  through `http://shared-bifrost:8080` (inside docker) /
  `http://host.docker.internal:4445` (host).
- `shared-litellm` (port 4444) container stopped + removed; service
  block deleted from `docker-compose.vllm.yml`; `litellm/config.yaml`
  archived to `shared-infra/.deleted-2026-05-14/`.
- Bifrost auth ENFORCED (`enforce_auth_on_inference: true`). Three
  virtual keys minted (`ada-prod`, `zero-prod`, `legion-prod`), one
  per project, each granted access to all 3 active providers. Keys
  live only in the respective project `.env`. Backups: `*.bak.20260514T171126-vk`.
- Project containers recreated to pick up new env: `ada-backend`,
  `ada-scheduler`, `ada-arq-worker`, `zero-api`, `legion-backend`.
- Smoke-tested end-to-end: every project's virtual key returns HTTP
  200 against vllm-local + moonshot + embed-local. Unauth requests
  return HTTP 401 as expected.

Below is the original punch list, preserved for reference.

---

## Current infra state (verified 2026-05-14 ~06:25 PT)

- **Bifrost** at `http://host.docker.internal:4445` (inside docker:
  `http://shared-bifrost:8080`).
- Active providers: `vllm-local` (Qwen3.6-35B-A3B, alias `qwen3-chat`),
  `embed-local` (Qwen3-Embedding-0.6B), `moonshot` (Kimi: kimi-k2.6,
  kimi-k2.5, kimi-k2.6-thinking, moonshot-v1-32k, moonshot-v1-128k).
- `KIMI_API_KEY` lives in `shared-infra/.env` (verified 200 OK against
  Moonshot 2026-05-14). All project `.env`s have it blanked.
- `MINIMAX_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
  `ANTHROPIC_API_KEY` blanked in `shared-infra/.env` AND in every project
  `.env` (backups: `*.bak.20260514T061951`).
- `shared-litellm:4444` still running with full cloud surface — to be
  retired after the three projects are migrated.

## Calling convention reminder

Bifrost expects `provider/model` in the `model` field, not just `model`:

| Old (LiteLLM 4444)        | New (Bifrost 4445)                          |
|---------------------------|---------------------------------------------|
| `kimi-k2.6`               | `moonshot/kimi-k2.6`                        |
| `qwen3-chat`              | `vllm-local/qwen3-chat` (or `Qwen3.6-35B-A3B`) |
| `qwen3-embed`             | `embed-local/Qwen/Qwen3-Embedding-0.6B`     |
| (any other cloud)         | **rejected — provider not registered**      |

Auth header: `Authorization` is optional today
(`enforce_auth_on_inference: false`). Keys for cloud providers are
resolved by Bifrost from `shared-infra/.env`, not from the caller's
header.

---

## Project 1 — Zero  (highest urgency)

**Why first**: `zero-api` is currently spamming bifrost with
`openrouter/google/gemini-3.1-flash-lite` requests every ~30s from its
Reachy intent provider-status probe. All three fall back paths
(`MiniMax-M2.7`, `kimi-k2.6` direct) fail because the keys are blanked
and circuit breakers are open. This is benign for Zero's UI but generates
~10 KB/min of error logs.

### Immediate fix (5 min, no code change)

Switch Zero's Reachy active provider away from gemini/openrouter to a
local model. Two options:

**Option A — via the runtime config file (preferred)**:
```bash
# Inside zero-api container, the persisted router config lives at
# /app/workspace/llm/router_config.json. Patch the active provider:
docker exec zero-api python -c "
import json, pathlib
p = pathlib.Path('/app/workspace/llm/router_config.json')
cfg = json.loads(p.read_text())
# Adjust the active_id / default_model fields to point at bifrost/vllm-local
# Inspect cfg first; structure is task-keyed.
print(json.dumps(cfg, indent=2))
"
# Then edit + restart zero-api.
```

**Option B — via Zero's UI** if there's a settings page for Reachy
providers.

### Code-side migration (~2-3 hour session)

| Severity | File / location | Change |
|---|---|---|
| BLOCKER | `docker-compose.sprint.yml:74,88` | Default `ZERO_VLLM_CHAT_URL` and `ZERO_VLLM_EMBED_URL` from `:4444/v1` → `:4445/v1`. |
| BLOCKER | `docker-compose.sprint.yml:75`   | `ZERO_VLLM_API_KEY` default — drop `LITELLM_MASTER_KEY`; Bifrost is keyless. |
| BLOCKER | `docker-compose.sprint.yml:85`   | `ZERO_VLLM_CHAT_MODEL=qwen3-chat` → `vllm-local/qwen3-chat` (or `vllm-local/Qwen3.6-35B-A3B`). |
| BLOCKER | `docker-compose.sprint.yml:89`   | `ZERO_VLLM_EMBED_MODEL=qwen3-embed` → `embed-local/Qwen/Qwen3-Embedding-0.6B`. |
| BLOCKER | `.env:83-84,92`                  | Strip `VLLM_CHAT_BASE_URL=localhost:18800/v1`, `VLLM_EMBED_BASE_URL=localhost:8001/v1`, and `LITELLM_MASTER_KEY` (or repoint to `:4445`). |
| BLOCKER | persisted runtime: `zero-api:/app/workspace/llm/router_config.json` | Rewrite `default_model` and each task's `providers[]` to use bifrost-prefixed names (`bifrost/vllm-local/qwen3-chat`, `bifrost/moonshot/kimi-k2.6`, `bifrost/embed-local/...`). **Editing code alone won't take effect until this file is rewritten.** |
| BLOCKER | `backend/app/infrastructure/llm_router.py:153-157` | `_DEFAULT_FALLBACKS` lists `minimax/MiniMax-M2.7`, `kimi/kimi-k2.6`, `vllm/qwen3-chat` — collapse to `bifrost/moonshot/kimi-k2.6`, `bifrost/vllm-local/qwen3-chat`. |
| BLOCKER | `backend/app/infrastructure/llm_providers/__init__.py:36-46` | Drop direct providers (`gemini`, `openrouter`, `huggingface`, `kimi`, `minimax`, `ollama`) from registration. Keep only `bifrost` + `vllm` (where `vllm` should also point at bifrost). |
| BLOCKER | `backend/app/infrastructure/llm_providers/gemini_provider.py:140-152` | `genai.Client(api_key=...)` has no `base_url` override — cannot redirect through bifrost. Must be **deleted**, not redirected. |
| BLOCKER | `backend/app/infrastructure/llm_providers/kimi_provider.py:70` | Replace direct `https://api.moonshot.ai/v1` with bifrost: `base_url=ZERO_BIFROST_URL`, model `moonshot/kimi-k2.6`. |
| IMPORTANT | `backend/app/infrastructure/ollama_client.py` | Retire (currently hits `:11434/api/chat` direct). Already aliased to vllm provider in `llm_providers/__init__.py` per comment, but the file is still imported. |
| IMPORTANT | `docker-compose.sprint.yml:105-109` | Remove `ZERO_GEMINI_API_KEY`, `ZERO_OPENROUTER_API_KEY`, `ZERO_KIMI_API_KEY`, `ZERO_HUGGINGFACE_API_KEY` env passthroughs. |
| NICE-TO-HAVE | `backend/app/infrastructure/config.py:30-44` | Drop settings fields for `gemini_api_key`, `openrouter_api_key`, etc. |

### Open question for Zero

Reachy realtime explicitly probes `bifrost-gemini-flash-lite`,
`bifrost-qwen`, `bifrost-deepseek` (see
`reachy_chat_provider.py` / `reachy_realtime/local_handler.py`). What's
the intended provider for the companion voice now that gemini-flash is
out? Recommend: `bifrost-qwen` (vllm-local) for full local-only.

---

## Project 2 — ADA  (highest complexity)

**Why complex**: ADA has the correct `llm_router.py` pointing at
bifrost (good), but a **second parallel routing system** initializes
at startup (`src/services/llm_service.py` +
`src/services/intelligent_llm_router.py`) which registers ollama / vllm
/ huggingface / grok as independent providers. Logs confirm:
`"Initialized intelligent router with 4 providers"`. ADA's KimiClient,
OpenRouterClient, MiniMaxClient all live alongside.

### Code-side migration (~3-4 hour session)

| Severity | File / location | Change |
|---|---|---|
| BLOCKER | `.env:319-320` | `VLLM_CHAT_BASE_URL=http://host.docker.internal:18800/v1`, `VLLM_EMBED_BASE_URL=http://host.docker.internal:8001/v1`. Repoint both to `http://host.docker.internal:4445/v1`, change model strings to bifrost-prefixed. |
| BLOCKER | `backend/infrastructure/ai_client.py:1378-1420` | `KimiClient` opens `AsyncOpenAI` direct to Moonshot. Change `base_url` → `BIFROST_GATEWAY_URL`, model name → `moonshot/kimi-k2.6`. |
| BLOCKER | `backend/infrastructure/ai_client.py:1137-1175` | `OpenRouterClient` direct to openrouter.ai — delete the class and any callers (OpenRouter is intentionally out per the new policy). |
| BLOCKER | `backend/infrastructure/llm_router.py:211-216` | Comment + design explicitly says Kimi callers "bypass this router." Reverse: route Kimi through the same router with model prefix `moonshot/kimi-k2.6`. |
| BLOCKER | `backend/services/provider_registry.py:102-141` | Parallel registry registering Kimi/OpenRouter/Groq/Ollama as standalone providers. Collapse to a single bifrost provider. |
| BLOCKER | `backend/services/chart_vision_analyzer.py:545` | `POST https://api.anthropic.com/v1/messages` direct. Anthropic is fully off; rewrite to Kimi vision via bifrost `moonshot/kimi-k2.6` OR delete the analyzer if Anthropic was its only path. |
| BLOCKER | `backend/services/intelligent_qa_agent.py:1117` | Same — direct `https://api.anthropic.com/v1/messages`. |
| BLOCKER | `src/services/llm_service.py` + `src/services/intelligent_llm_router.py` | Competing router. Either delete (preferred) or rewrite its `_initialize_providers` to register only `bifrost`. |
| BLOCKER | `src/services/config.py:82,145,175` | Hardcoded `base_url="http://ollama:11434"`, `https://api.anthropic.com`, `https://api.moonshot.cn/v1` (wrong region — `.cn` instead of `.ai`). |
| BLOCKER | `src/ada/langgraph/providers/{minimax,kimi}.py` | Direct cloud URLs (lines 79/135 minimax, 88/156 kimi). Replace with bifrost. |
| BLOCKER | `src/ada/core/config.py:59,78` + `src/ada/core/llm_engine.py:106,149` + `src/ada/core/structured_output.py:78` | `ollama_host` defaults to `http://ollama:11434` — hardcoded literal docker hostname. Mitigated in current docker by `OLLAMA_HOST=http://shared-bifrost:8080` but fragile. |
| IMPORTANT | `docker-compose.yml:287,294,295` | `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` env passthroughs in `ada-backend` — strip. |
| IMPORTANT | `backend/routers/integration_test.py:353` | Reads `GEMINI_API_KEY` — dead. Clean up. |
| IMPORTANT | Verify `LOCAL_EMBEDDINGS_MODEL` actually flows: startup log says `nomic-embed-text:latest` while env says `embed-local/Qwen/Qwen3-Embedding-0.6B`. Possible stale log string OR a code path using `nomic-embed-text`. Confirm with a request trace. |
| NICE-TO-HAVE | `backend/infrastructure/ai_client.py:60-62` | `AIProvider` enum still includes `OLLAMA`, `OPENROUTER`, `KIMI`. Collapse to `BIFROST` + per-model tags. |
| NICE-TO-HAVE | `backend/infrastructure/llm_router.py:1-20` | Module docstring still references "LiteLLM proxy / MiniMax Cloud API" as fallback. |
| NICE-TO-HAVE | `backend/tests/test_llm_router.py:115,237,244` | Test fixtures use `ollama_host="http://localhost:11434"` — update after migration. |

---

## Project 3 — Legion  (already half-migrated)

**Why easiest**: `LEGION_USE_BIFROST=true` is already set in
`Legion/.env:44` and `LEGION_ALLOW_CLOUD_FALLBACK=false` already
prevents the fallback chain from firing. Only `VLLMClient` honors the
flag today; Gemini/Claude/Kimi/MiniMax clients still bypass.

### Code-side migration (~2 hour session)

| Severity | File / location | Change |
|---|---|---|
| BLOCKER | `backend/app/services/llm_clients/kimi_client.py:33` | `KIMI_BASE_URL = "https://api.moonshot.ai/v1"` hardcoded. Rewrite to take `BIFROST_URL` as `base_url` with model `moonshot/kimi-k2.6`. |
| BLOCKER | `backend/app/services/llm_clients/minimax_client.py:49` | `MINIMAX_BASE_URL = "https://api.minimax.io/v1"` hardcoded. **Delete** — MiniMax is fully disabled per policy. |
| BLOCKER | `backend/app/services/llm_clients/gemini_client.py:104` | Uses `genai.Client(api_key=...)` — no `base_url` override. **Delete** — Gemini fully disabled per policy. |
| BLOCKER | `backend/app/services/llm_clients/claude_client.py:88,105` | Anthropic SDK with hardcoded `api_key`. **Delete** — Anthropic fully disabled per policy. |
| BLOCKER | `backend/app/services/unified_llm_service.py:399-411` | Instantiates `ClaudeClient`, `GeminiClient`, `KimiClient`, `MinimaxClient` eagerly at startup. Delete imports for Claude/Gemini/MiniMax; keep Kimi (now routed through bifrost per `kimi_client.py` fix above). |
| BLOCKER | `backend/app/services/unified_llm_service.py:443-480` | Only Kimi has a disable gate (`KIMI_DISABLED`). Either gate all four with `*_DISABLED` env vars or delete the four cloud client paths entirely. |
| IMPORTANT | `docker-compose.yml:147-152` | Strip `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `KIMI_API_KEY`, `MINIMAX_API_KEY` env injections. Bifrost owns the keys; the container shouldn't see them. |
| IMPORTANT | `.env` vs `backend/.env` model-name drift | Root says `VLLM_CHAT_MODEL=qwen3-chat`; backend says `Qwen3.6-35B-A3B`. Pick one (Bifrost aliases both). Recommended: `qwen3-chat`. |
| IMPORTANT | `.env:33-34,81` | `OLLAMA_BASE_URL`/`OLLAMA_NATIVE_URL` still set, `OLLAMA_DISABLED=false` — flip to disabled. User wants no Ollama path. |
| NICE-TO-HAVE | `docker-compose.yml:82-116` | `legion-litellm` service block (gated `profiles: [disabled]`) — delete the block and the `docker/litellm/config.yaml` file it mounts. |
| NICE-TO-HAVE | `docker-compose.yml:177-180` | `LEGION_USE_LITELLM=false`, `LITELLM_URL`, `LITELLM_MASTER_KEY` — strip; no longer used. |
| NICE-TO-HAVE | `docker-compose.observability.yml:75,98,110` | References nonexistent `legion-db` — rename to `legion-postgres`. |
| NICE-TO-HAVE | `backend/app/services/rag_service.py:236-237` | Reads `VLLM_EMBED_BASE_URL` directly (host:8001/v1), bypasses bifrost. Acceptable for "local embeddings" goal; future cleanup could route through bifrost as `embed-local/...`. |

---

## After all three projects are migrated

Once Zero / ADA / Legion all route through `:4445` and the cloud-client
classes are deleted:

1. **Retire `shared-litellm`**:
   ```bash
   docker stop shared-litellm
   docker rm   shared-litellm
   # then remove the service block from
   #   shared-infra/docker-compose.vllm.yml
   # and delete shared-infra/litellm/config.yaml
   ```
2. **Tighten Bifrost auth** in `bifrost/config.json`:
   ```jsonc
   "client": { "enforce_auth_on_inference": true, ... }
   ```
   Then issue per-project virtual keys via the bifrost governance API,
   wire them into the projects' env, and Bifrost will reject any caller
   without a valid token.
3. **Enable Bifrost file logging**. `bifrost/logs/` is currently an empty
   directory — runtime errors only land in `docker logs shared-bifrost`.
   Worth flipping on persistent file logging before tightening auth, so
   post-cutover failures are debuggable.
4. **Add Prometheus**. `prometheus plugin not found` warns on every
   startup. Wiring this gives Bifrost RED metrics in the existing ada/
   legion grafana stacks.

## What was changed today (2026-05-14) — recap

- `bifrost/config.json` — minimax + openrouter removed; moonshot added.
- `bifrost/disabled-providers.json` — moonshot moved out, minimax +
  openrouter moved in. README rewritten.
- `docker-compose.bifrost.yml` — env passthrough stripped to
  `KIMI_API_KEY` only.
- `shared-infra/.env` — propagated ADA's working Kimi key
  (`sk-Ou1k…`, verified 200 OK); blanked MiniMax/Gemini/OpenRouter/
  Anthropic keys.
- `ADA/.env`, `zero/.env`, `Legion/.env`, `Legion/backend/.env` — all
  cloud-provider keys blanked. Backups left as `*.bak.20260514T061951`.
- `bifrost/config.db` — stale `config_providers` (minimax id=9,
  openrouter id=26) + matching `config_keys` rows deleted via SQLite
  to stop the per-12s polling/log-spam. Backup left as
  `config.db.bak.20260514T0625`.
- `bifrost/README.md` — updated to reflect Kimi-only cloud surface and
  the new SQLite-cleanup procedure for future provider removals.

## Smoke-test results post-fix (2026-05-14 11:25 UTC)

```
vllm-local/Qwen3.6-35B-A3B                    HTTP 200 ~430ms
vllm-local/qwen3-chat (alias)                 HTTP 200 ~180ms  (was 502 connection-drop before restart — fixed)
moonshot/kimi-k2.6                            HTTP 200 ~1100ms (was 400 "no providers found" before)
embed-local/Qwen/Qwen3-Embedding-0.6B         HTTP 200 ~40ms
minimax/MiniMax-M2.7                          HTTP 400 "no keys found that support model"
openrouter/google/gemini-3.1-flash-lite       HTTP 400 (rejected at routing layer)
```
