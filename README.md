# Shared vLLM Infrastructure

One set of vLLM containers serving Zero, Ada, and Legion. Single model in VRAM per role — no per-project duplication. All LLM traffic routes through the **Bifrost gateway** (host port **4445**; `shared-litellm`:4444 was retired 2026-05-14).

## Stack (2026-06-11)

| Endpoint | Model | Host port | Container port |
|---|---|---|---|
| Chat / code (`vllm-chat`) | `Qwen/Qwen3.5-35B-A3B-GPTQ-Int4` on vLLM v0.22.1 — MoE 35B/3B-active, hybrid GDN, 64K ctx, tools enabled, Marlin INT4 kernels. Served names: `Qwen3.5-35B-A3B`, `Qwen3.6-35B-A3B`, `qwen3-chat`, `Qwen3-32B-AWQ` (legacy) | **18801** | 8000 |
| Embedding (`vllm-embed`) | `Qwen/Qwen3-Embedding-0.6B` | 8001 | 8001 |
| LLM gateway (`shared-bifrost`) | Bifrost v1.5.0 — 11 ALL-FREE providers (see `bifrost/README.md`) | **4445** | 8080 |
| Free-tier aggregator (`shared-freellmapi`) | ~14 free providers, priority fallback chain | **3015** | 3001 |

**Why 18801 not 8000?** The Reachy Mini desktop daemon hardcodes `:8000` on the Windows host; 18800 was the retired llama-cpp-chat slot. Inside the Docker network Bifrost hits `vllm-chat:8000` directly.

**Model history** (full flag-level history in `docker-compose.vllm.yml` header):
- *35B-A3B-FP8 (March 2026)* — ~34 GiB, doesn't fit 32 GB. Retired.
- *QuantTrio Qwen3.6-35B-A3B-AWQ (2026-04-27 + 2026-05-21 attempts)* — OOM/CUDA failures. Root cause identified 2026-06-11: vLLM 0.18/0.19 hybrid-architecture bugs (vllm#41153/#41619) + community quant, NOT the model. Reverted both times.
- *Huihui-Qwen3.6-35B-A3B GGUF on llama.cpp (2026-04-28→2026-05-17)* — retired for llama.cpp structured-JSON bugs + CUDA regression.
- *Qwen3-32B-AWQ (2026-04-27→2026-06-11)* — dense 32B Int4, the stable baseline. ~50 tok/s, 12K ctx, tools off. Remains the documented rollback (see compose header).
- *Qwen3.6-35B-A3B-NVFP4 (2026-06-11, retired same day)* — FlashInfer CUTLASS NVFP4 kernels crash under torch.compile and hang (<1 tok/s then frozen) under eager on this WSL2+Blackwell box. Full attempt log in the compose file.
- *Qwen3.5-35B-A3B-GPTQ-Int4 (current, 2026-06-11→)* — official Qwen quant on vLLM v0.22.1, **Marlin INT4 kernels** (the family proven on this card since April). MoE 3B-active + hybrid GDN: 64K context at ~1-2 GB KV, tool calling enabled via `qwen3_xml` parser. Field-proven 5090 recipe (131K ctx / 194 tok/s reported upstream).

## Start / stop

```bash
docker compose -f docker-compose.vllm.yml up -d
docker compose -f docker-compose.vllm.yml logs -f vllm-chat
docker compose -f docker-compose.bifrost.yml up -d
```

First startup downloads weights (~23 GB checkpoint). The `shared-hf-cache` volume persists them across restarts and across all three projects.

## Health

```bash
curl http://localhost:4445/health        # Bifrost gateway (the one projects use)
curl http://localhost:18801/v1/models    # vllm-chat direct (diagnostics only)
curl http://localhost:8001/v1/models     # vllm-embed direct
curl http://localhost:3015/              # freellmapi dashboard
```

## How projects connect

Each project calls Bifrost with a per-project virtual key and `provider/model` strings (see `bifrost/README.md` for the VK table and calling convention). The canonical local model name in ALL project configs is **`vllm-local/qwen3-chat`** — a gateway alias that maps to whatever vllm-chat currently serves, so local model swaps never require project redeploys.

**Per-project cloud affinity (2026-06-11)** — keeps the three projects off each other's free rate limits:
- **Legion** → NVIDIA NIM: `z-ai/glm-5.1` (reasoning + code), `qwen3-next-80b` (fast)
- **ADA** → Kimi K2.6 free via NIM (`nvidia-nim/moonshotai/kimi-k2.6`; the `moonshot/` provider name is a compat shim over NIM — paid Moonshot retired 2026-06-11)
- **Zero** → Gemini Flash (`gemini-3-flash-preview`, 1,500 RPD) + Groq `gpt-oss-120b` (200K TPD)
- Everyone → local vLLM first, `freellm/auto` emergency tail.

## VRAM budget (5090 / 32 GB)

- vllm-chat (Qwen3.6-35B-A3B NVFP4): ~20 GB weights + ~1-2 GB KV @ 64K (hybrid GDN)
- vllm-embed (Qwen3-Embedding-0.6B): ~1.5 GB
- cudagraph buffers: ~1-1.5 GB
- Total pinned: ~25 GB (0.88 util cap on chat, 0.12 on embed)

If KV pressure shows up, lower `--max-model-len` on `vllm-chat` (currently 65536) or reduce `--gpu-memory-utilization` (currently 0.88). Do NOT switch KV off fp8.
