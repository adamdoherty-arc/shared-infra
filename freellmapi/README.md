# shared-freellmapi

Self-hosted OpenAI-compatible proxy that aggregates the **free tiers of ~14
LLM providers** (Google Gemini, Groq, Cerebras, SambaNova, Mistral,
OpenRouter, GitHub Models, Cloudflare Workers AI, Cohere, Z.ai/Zhipu,
NVIDIA, Ollama Cloud) behind a single `/v1/chat/completions` endpoint, with
priority-ordered fallback chain, per-key RPM/RPD/TPM/TPD rate-limit tracking,
and a built-in admin dashboard.

**Upstream:** [tashfeenahmed/freellmapi](https://github.com/tashfeenahmed/freellmapi) — MIT, Node.js/Express + Vite UI.
**Pinned commit:** `18eb04be990bcdaca842574bf6e00a6968308761` (see `Dockerfile`).

Sibling of `shared-bifrost`:
- **bifrost** owns the local vLLM transport gateway (port 4445, Qwen3-32B-AWQ on-prem).
- **freellmapi** owns the cross-provider free-tier fallback chain (port 3001).

They are peers. Consumers (Legion / ADA / Zero) pick one or the other per
call based on their own per-source routing policy.

---

## First boot

```bash
# 1. Set the encryption key (generates a 64-char hex AES-256-GCM master key).
#    Treat this like a DB password — losing it makes stored provider keys
#    unrecoverable.
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
# Paste into c:\code\shared-infra\.env as FREELLMAPI_ENCRYPTION_KEY=...

# 2. Bring up the service.
cd c:\code\shared-infra
docker compose -f docker-compose.freellmapi.yml up -d

# 3. Verify health.
docker logs shared-freellmapi --tail 30
curl http://localhost:3001/
```

## Configure providers (one-time, via dashboard)

1. Open `http://localhost:3001` in a browser.
2. Navigate to the **Keys** page, add at least 3 providers to get a useful
   fallback chain. Recommended starting set: Google AI Studio + Groq +
   Cerebras (high daily caps + fast). See the upstream README's Provider
   table for free-tier limits per platform.
3. Navigate to the **Fallback Chain** page. Reorder so highest-quality /
   highest-quota providers are first. The router will try them in order
   until one accepts the request.
4. Note the **unified bearer token** at the top of the Keys page — copy it
   into each consumer project's `.env` as `FREELLM_BEARER_TOKEN`.

## Consumer integration

Each consumer project (Legion / ADA / Zero) speaks to this service over the
shared docker network at `http://shared-freellmapi:3001/v1`, authed with the
unified bearer token. Inspect response headers on every call:

- `X-Routed-Via: groq/llama-3.3-70b-versatile` — which provider served the call.
- `X-Fallback-Attempts: 1` — how many chain links the router tried before success.

Legion's `FreeLLMAPIClient` (`backend/app/services/llm_clients/freellm_client.py`)
parses these headers and writes them to `llm_call_details` so the LLM Console
can report per-provider attribution.

## Production notes

- **Local-first only.** Per upstream README, do not expose port 3001 to the
  public internet. Free-tier providers' ToS in some cases forbids redistributing
  inference; this service is for personal experimentation across our own apps.
- **Capacity:** ~1B+ tokens/month aggregate across all stacked free tiers
  (varies by provider).
- **Intelligence degrades through the day** — the router falls to lower-quality
  links once daily caps on the top models exhaust; caps reset at UTC midnight.
- **SQLite persistence** lives in the named volume `shared-freellmapi-data`.
  Backups: `docker run --rm -v shared-freellmapi-data:/d -v $PWD:/b alpine
  cp /d/freellmapi.db /b/freellmapi.db.bak`.

## Rebuild on upstream bump

```bash
# Bump the commit SHA in docker-compose.freellmapi.yml or pass via env:
export FREELLMAPI_COMMIT=<new-sha>
docker compose -f docker-compose.freellmapi.yml build --no-cache
docker compose -f docker-compose.freellmapi.yml up -d
```
