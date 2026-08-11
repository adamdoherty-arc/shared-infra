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
- `moonshot`    → **PARKED 2026-07-13 (Perf-503b-W3b)** — was a compat shim over `https://integrate.api.nvidia.com` for legacy `moonshot/kimi-*` callers. Parked because its only upstream (NVIDIA NIM's `moonshotai/kimi-k2.6`) now 404s "Function ... Not found for account" — see `nvidia-nim` note below. Block preserved verbatim in `disabled-providers.json`; restore once NVIDIA redeploys the function.
- `nvidia-nim`  → `https://integrate.api.nvidia.com` (43 models incl. z-ai/glm-5.2, moonshotai/kimi-k2.6, deepseek-v4-flash, minimax-m2.7, nemotron-3-ultra-550b, qwen3.5-122b/397b, qwen3-next-80b — 40 RPM free. NOTE 2026-07-13: `z-ai/glm-5.1` was RENAMED upstream to `z-ai/glm-5.2` (config.json keeps a `glm-5.1` alias for old callers). NOTE 2026-06-11: qwen3-coder-480b was DELISTED from the NIM catalog; deepseek-v4-pro still times out >170s (not in any chain) but deepseek-v4-flash was verified working 2026-07-08 and added — codestral-22b-instruct/starcoder2-15b/deepseek-coder-6.7b/**moonshotai/kimi-k2.6 (added 2026-07-13)** are LISTED in the catalog but 404 "not found for account", not actually deployed — an NVIDIA-side bug, confirmed on both NV_API_KEY and NV_API_KEY_2, not fixable from our side)
- `openrouter`  → `https://openrouter.ai/api` (19 `:free` models incl. nemotron-3-ultra-550b (1M ctx), qwen3-coder (1M ctx), qwen3-next-80b, gemma-4-31b/26b-a4b, gpt-oss-120b/20b, poolside laguna, cohere/north-mini-code, tencent/hy3, nousresearch/hermes-3-405b, plus `openrouter/free` auto-router. $10 lifetime topup applied 2026-06-11 → 1,000 free req/day. UPDATE 2026-07-08: the old "404 needs privacy toggle" note for `qwen/qwen3-coder:free` no longer reproduces — current behavior is transient 429 upstream rate-limiting via the "Venice" backing provider (retry_after 3-30s), which Bifrost's existing retry/fallback already absorbs. `nex-agi/nex-n2-pro:free` was removed — OpenRouter delisted it from free tier (paid-only now); `poolside/laguna-xs.2:free` renamed upstream to `laguna-xs-2.1:free`, updated)
- `hf-router`   → `https://router.huggingface.co` (13 models: DeepSeek, Kimi (incl. dedicated Kimi-K2.7-Code), MiniMax, Qwen (incl. Qwen3-Coder-480B), GLM, gpt-oss — ~100K free credits/month, thin; use as deep fallback only)
- `groq` / `cerebras` / `mistral` — perpetual free tiers (gpt-oss-120b at 200K TPD on groq; ~1M tok/day on cerebras; ~1B tok/month on mistral — incl `mistral-large-latest` + `magistral-medium-latest` added 2026-06-11 and verified through the gateway; magistral returns content as a parts-array, callers must join text parts). Added 2026-07-08: groq gained `qwen/qwen3.6-27b` + `meta-llama/llama-4-scout-17b-16e-instruct`; cerebras gained `gemma-4-31b`; mistral's `codestral-latest` is the dedicated coder.
- `gemini`      → **PARKED 2026-07-08** — was FLASH-ONLY (gemini-3.5-flash, gemini-3-flash-preview, 3.1-flash-lite — 1,500 req/day free; Pro previews billing-gated since ~May 2026, already removed before parking). `GEMINI_API_KEY` in `shared-infra/.env` is invalid/expired; block preserved verbatim in `disabled-providers.json`. Re-enable: refresh the key at https://aistudio.google.com/apikey, copy the block back into `config.json`, run `sync_vk_allowlists.py`, restart.
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

Six virtual keys exist (as of 2026-07-09), created via the governance API.
Each has all providers allowed with `allow_all_keys=true` so it can route to
vllm-local, embed-local, moonshot, nvidia-nim, and the other free lanes. After
any VK add or provider/model change, run `sync_vk_allowlists.py` (stop -> sync
-> start) so the per-VK allowlists mirror `config_keys` verbatim.

| Project     | Virtual key name    | Where the key lives |
|---|---|---|
| ADA         | `ada-prod`          | `C:\code\ADA\.env` -> `BIFROST_GATEWAY_KEY=sk-bf-...` |
| Zero        | `zero-prod`         | `C:\code\zero\.env` -> `VLLM_API_KEY` + `ZERO_BIFROST_API_KEY` |
| Legion      | `legion-prod`       | `C:\code\Legion\.env` + `Legion\backend\.env` -> `BIFROST_API_KEY` |
| FortressOS  | `fortressos-prod`   | FortressOS project config |
| Claude Code | `claude-code-local` | Local Claude Code / MCP sessions |
| Hermes      | `hermes-prod`       | `~/.hermes/config.yaml` -> `model.api_key` (sk-bf-...); added 2026-07-09 when Hermes moved onto Bifrost |

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

## `vllm-local` gateway concurrency: 1 -> 4 (2026-08-06)

`vllm-local` was `concurrency=1, buffer_size=6`, mirroring vLLM's
`--max-num-seqs 1`. That double-gates the local model: Bifrost admitted one
request at a time, so **any** call to that provider — including
`list_models`, which vLLM answers from memory without touching the
scheduler — blocked until the in-flight generation finished.

Measured, model-list through Bifrost with one generation in flight:

| | `?provider=vllm-local` |
|---|---|
| before (`concurrency=1`) | **16.3s** |
| after (`concurrency=4`) | **0.017s** |

Idle it was 3ms both ways — the cost only appeared under load, which is why
it hid. Compounded over the sequential 15-provider fan-out this was a large
part of `/v1/models` taking 30-90 minutes (see the odysseus session
2026-08-06; ADA polls it every ~30s with a 2.5s client timeout, and Bifrost
does not cancel on client disconnect, so abandoned calls kept grinding).

**Why this does NOT re-open the `running>=2` wedge** (docker-compose.vllm.yml,
falsified promotions 2026-07-13/14): the wedge condition is `running>=2`
*inside vLLM*, and `--max-num-seqs 1` enforces that regardless of how many
requests a client sends. Verified 2026-08-06 with 3 concurrent direct
requests: `num_requests_running` held at 1.0 while `num_requests_waiting`
rose to 2.0. Raising the **gateway** gate moves queueing from Bifrost's
buffer into vLLM's own scheduler; it does not raise vLLM's running count.
`VLLM_MAX_NUM_SEQS` is untouched and must stay 1.

Post-change soak: `running=1.0`, `waiting=3.0`, `generation_tokens_total`
climbing ~62 tok/s (the wedge signature is GPU 100% with that counter
*flat*), all vllm containers healthy, wedge-monitor quiet.

> **SUPERSEDED 2026-08-11 (Pass-6).** Everything above is accurate history for
> the 08-06 change, but two of its statements are no longer current fact:
> gateway `concurrency` is now **6** (`buffer_size` 12), and
> `VLLM_MAX_NUM_SEQS` is now **6**, not 1 — see the "Pass-6" section below and
> `docker-compose.vllm.yml`. The reasoning in this section still holds and is
> worth keeping: raising the *gateway* gate never raised vLLM's running count,
> which is why 08-06 was safe on its own. Pass-6 is a different change — it
> raised the *engine* limit, and it was only defensible because swapping the
> 27B for an 8B first freed ~8 GB of weight VRAM into the KV pool, lifting the
> measured concurrency ceiling to 9.62x at 32K ctx.

Persisted in `config.db` (`config_providers.concurrency_buffer_json`), so it
survives a container restart. Prior value backed up to
`vllm-local.provider.bak-20260806T220527Z.json`. **Revert:**

```bash
curl -s http://localhost:4445/api/providers/vllm-local \
  | python -c "import json,sys,urllib.request; c=json.load(sys.stdin); \
c['concurrency_and_buffer_size']={'concurrency':1,'buffer_size':6}; \
urllib.request.urlopen(urllib.request.Request( \
'http://localhost:4445/api/providers/vllm-local', data=json.dumps(c).encode(), \
headers={'Content-Type':'application/json'}, method='PUT'))"
```

## `vllm-local` Pass-6: engine + gateway to 6 (2026-08-11)

The 08-06 change above raised only the *gateway* gate. Pass-6 raised the
**engine** limit, which is a materially different risk, and is why it needed
the model swap to go with it.

| | before | after |
|---|---|---|
| `--model` | `cyankiwi/Qwen3.6-27B-AWQ-INT4` (~13.5 GB) | `Qwen/Qwen3-8B-AWQ` (~5.5 GB) |
| `VLLM_MAX_NUM_SEQS` | 1 | **6** |
| Bifrost `concurrency` / `buffer_size` | 4 / 6 | **6 / 12** |
| `--max-num-batched-tokens` | 2304 | 8192 |
| `--gpu-memory-utilization` | 0.92 | 0.90 |
| request timeout | 120s | 90s |
| `drop_excess_requests` | false | **true** |

**Why raising the engine limit was defensible here when 07-02 and 07-14 were
not:** both earlier attempts raised concurrency *without freeing weight VRAM*,
so the KV pool could not cover the extra sequences. Freeing ~8 GB first gave a
measured ceiling of **9.62x at 32K ctx** (`GPU KV cache size: 315,296 tokens`),
so seqs=6 sits well under it. Also relevant: seqs=1 was never actually
wedge-free — the wedge-monitor logged stalls at `running=1` on 07-28, 07-29,
08-02, 08-10 and 08-11.

First-day measurement (engine metrics, ~6h after the 08:50Z boot): **6,749
requests, 0 errors, 0 aborts, 0 preemptions**, mean TTFT 0.56s, and **6.5
seconds of total queue time across all requests** — down from ~17s *per*
request. ADA's local-lane success rate went 0-50% -> 89-100% at the hour of the
swap.

**This is a watchful experiment, not a settled config.** Tripwires:
`vllm-wedge-monitor`, host `nvidia-smi`, and `vllm:num_preemptions_total`.
**Rollback:** `.env` `VLLM_MAX_NUM_SEQS=1`, Bifrost `vllm-local` concurrency 1 /
buffer 2, ADA `.env` `VLLM_MAX_CONCURRENT=1`.

**Caveat worth knowing:** all six `--served-model-name` aliases
(`Qwen3.5-35B-A3B`, `Qwen3.6-35B-A3B`, `Qwen3.6-27B`, `Qwen3-32B-AWQ`,
`gpt-oss-20b`, `qwen3-chat`) now answer with an 8B. The names are legacy, not
descriptive. `gpt-oss-20b` and `Qwen3.6-27B` were missing from this provider's
`models` list until 2026-08-11 and 403'd through the gateway while resolving
fine on a direct `:18801` call.

## Nemotron 3.5 Lightning (added 2026-08-11) — thinking + tool-calling gotchas

Added day-0 on both free lanes:

| Bifrost model | measured via gateway |
|---|---|
| `nvidia-nim/nvidia/nemotron-3.5-lightning-30b-a3b` | 891ms |
| `openrouter/nvidia/nemotron-3.5-lightning:free` | 696ms |
| `nvidia-nim/nvidia/nemotron-3.5-content-safety` | 385ms |

30B total / 3B active hybrid Mamba MoE, 1M ctx, OpenMDW-1.1. Artificial Analysis
Intelligence Index **24** — level with gpt-oss-120b, *below* Qwen3.6-35B-A3B (32).
It is a **speed** play: excellent at scoped single-step work (PinchBench 83.4,
MMLU-Pro 81.6), weak at multi-turn agentic tool loops (Terminal-Bench 2.1 = 23.5,
τ³-bench Banking = 9.5). Route high-volume scoped calls to it; do NOT make it a
planner or a primary coding agent.

**Thinking is ON by default and the two providers suppress it differently.**
Measured 2026-08-11 against a "what is 2+2" prompt:

| | `chat_template_kwargs.enable_thinking=false` | `reasoning.enabled=false` |
|---|---|---|
| nvidia-nim | **works** (reasoning 446 -> 0) | n/a |
| openrouter | **IGNORED** (reasoning stayed 481) | **works** (-> 0) |

ADA injects `chat_template_kwargs` globally (`llm_router.py:1681,1714,1769`), so the
NIM lane suppresses correctly and the **OpenRouter lane does not**. This matters
beyond tidiness: with a small `max_tokens` the response truncates mid-reasoning and
the partial chain-of-thought lands in `content` (reproduced at `max_tokens=16`) —
the same reasoning-starvation class documented in `docker-compose.vllm.yml` for
gpt-oss. At `max_tokens>=200` content was clean in every combination.

**On NIM, `enable_thinking=false` silently disables tool calling.** Same prompt and
tool schema, `tool_choice=auto`:

```
nim  auto + enable_thinking:false  -> tool_calls: NULL      (answers in prose)
nim  auto + thinking ON            -> tool_calls: [get_weather]  finish=tool_calls
nim  tool_choice:"required" + off  -> tool_calls: [get_weather]  (forced works)
openrouter  auto, either mode      -> tool_calls: [get_weather]  (unaffected)
```

So: **do not put the NIM Lightning lane in a tool-calling chain** while ADA's global
`enable_thinking=false` injection is in place — tools will silently stop firing with
no error. Use it for non-tool lanes (overflow / general / sentiment / structured
generation), where `response_format: json_schema` was verified working with a strict
schema. The OpenRouter lane is the tool-safe variant but needs `reasoning.enabled=false`
to keep reasoning out of the payload.

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
