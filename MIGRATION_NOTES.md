# Central LiteLLM Migration — 2026-04-21

The shared-infra stack now owns a single LiteLLM proxy at `host.docker.internal:4444`. All projects route through it with one env var.

## Why

Before: Zero hardcoded `:18800`, Legion ran its own `legion-litellm:4000`, ADA bypassed LiteLLM and hit Ollama direct. A port change broke one project silently.

After: one `config.yaml` in `shared-infra/litellm/`, one master key, one URL, one router. Backends move freely.

## Zero — done

- `docker-compose.sprint.yml` now sets `ZERO_VLLM_CHAT_URL=http://host.docker.internal:4444/v1` (chat) and `ZERO_VLLM_EMBED_URL=http://host.docker.internal:4444/v1` (embed). Auth via `ZERO_VLLM_API_KEY=${LITELLM_MASTER_KEY}`.
- `ZERO_VLLM_EMBED_MODEL` changed from the HF slug to the alias `qwen3-embed` (LiteLLM resolves both aliases).
- `.env` picked up `LITELLM_MASTER_KEY`.
- Container recreated.

## Legion — done

- `docker-compose.yml` `LITELLM_URL` flipped from `http://legion-litellm:4000` to `http://host.docker.internal:4444` (override-safe via env).
- `.env` picked up `LITELLM_URL` + `LITELLM_MASTER_KEY`.
- **`legion-litellm` service is still running** — don't rip it out until the shared router is proven in steady state. Disable it when you're ready:
  ```bash
  docker compose -f c:\code\Legion\docker-compose.yml stop legion-litellm
  # Then delete the service block from docker-compose.yml and the
  # docker/litellm/config.yaml file.
  ```
- Restart Legion backend to pick up the new env:
  ```bash
  docker compose -f c:\code\Legion\docker-compose.yml up -d legion-backend
  ```

## ADA — migration diff (not applied)

ADA currently has LiteLLM disabled (commented in `docker-compose.yml` lines 182-183). It calls Ollama direct via `OLLAMA_HOST: http://host.docker.internal:11434`. The migration is additive — ADA can keep calling Ollama for fallback:

```diff
  services:
    ada-backend:
      environment:
        OLLAMA_HOST: http://host.docker.internal:11434
+       LITELLM_URL: http://host.docker.internal:4444
+       LITELLM_API_KEY: ${LITELLM_MASTER_KEY}
-       # LITELLM_URL: http://litellm-proxy:4000  # No litellm-proxy container
-       # LITELLM_API_KEY: sk-litellm-master-key
```

Then in ADA's `.env`, set `LITELLM_MASTER_KEY` to the same value used by Zero/Legion/shared-infra (already in `c:\code\shared-infra\.env`).

ADA's backend must also be updated to prefer LiteLLM over Ollama for models that LiteLLM handles (Claude, Kimi, Gemini, vLLM). That's an ADA code change outside this migration and should happen next time ADA is in active dev.

## Verification

- `curl http://localhost:4444/health/liveliness` → `"I'm alive!"`
- `curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" http://localhost:4444/v1/models` → 17 models (qwen3-chat, qwen3-embed, kimi-k2.5, claude-*, gemini-*, etc.)
- Zero's autonomous research loop dispatched a report end-to-end after the swap.

## What lives where now

```
Zero      (zero-api)       ─┐
ADA       (ada-backend)    ─┼──► host.docker.internal:4444 ──► shared-litellm ──┬─► vllm-chat:8000
Legion    (legion-backend) ─┘                                                    ├─► vllm-embed:8001
                                                                                 ├─► Moonshot (Kimi)
                                                                                 ├─► Anthropic (Claude)
                                                                                 ├─► Google (Gemini)
                                                                                 └─► Ollama on host (:11434)
```

## Future cleanup

- [ ] Remove `legion-litellm` service after 7 days of clean operation on shared router
- [ ] Apply ADA diff above and restart ada-backend
- [ ] Rename Zero's `ZERO_VLLM_CHAT_URL` / `ZERO_VLLM_EMBED_URL` to `ZERO_LLM_ROUTER_URL` (single variable) — code rename across zero codebase
- [ ] Pin LiteLLM image to `main-v1.81.14-stable` or later (compose currently uses `:main-stable` floating tag). Per SecondBrain.md §9, 1.82.7/1.82.8 were compromised.
