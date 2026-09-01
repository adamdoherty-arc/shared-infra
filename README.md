# Shared vLLM Infrastructure

One set of vLLM containers serving Zero, Ada, and Legion. Single model in VRAM per role — no per-project duplication. All LLM traffic routes through the **Bifrost gateway** (host port **4445**; `shared-litellm`:4444 was retired 2026-05-14).

## Stack (2026-08-31, Pass-9 / Migration-20)

| Endpoint | Model | Host port | Container port |
|---|---|---|---|
| Chat / code (`qwen38-chat`) | **Qwen3.8-27B** (`dbirks/Qwen3.8-27B-W4A16-AutoRound`) on the syv-ai/qwen38-27b-rtx3090 patched-vLLM 0.27.1 stack — dense 27B VLM (AA Index 52), W4A16 + int4 lm_head/embeddings, batch profile, 65,536 ctx, 32 seqs, 253,952-token KV pool, tools enabled (`qwen3_coder` parser, works under `enable_thinking:false`), prefix caching live. Serves ONE name: `qwen3.8-27b`; all 8 legacy aliases (`Qwen3.5-35B-A3B` / `qwen3-chat` / `Qwen3.6-27B` / `Qwen3-32B-AWQ` / `Qwen3.6-35B-A3B` / `gpt-oss-20b` / `nemotron-3.5-lightning` / `local-chat`) are remapped in Bifrost's `vllm-local` key. Previous engine (`vllm-chat`, Nemotron-3.5-Lightning NVFP4) kept behind the `nemotron-rollback` compose profile | **18801** | 18020 |
| Embedding (`vllm-embed`) | `Qwen/Qwen3-Embedding-0.6B` | 8001 | 8001 |
| LLM gateway (`shared-bifrost`) | Bifrost v1.5.0 — 11 ALL-FREE providers (see `bifrost/README.md`) | **4445** | 8080 |
| Free-tier aggregator (`shared-freellmapi`) | ~14 free providers, priority fallback chain | **3015** | 3001 |

**Why 18801 not 8000?** The Reachy Mini desktop daemon hardcodes `:8000` on the Windows host; 18800 was the retired llama-cpp-chat slot; the stack's native 18020 sits inside a Windows WinNAT excluded port range (17938-18337) and cannot be bound. Inside the Docker network Bifrost hits `qwen38-chat:18020` directly. ~15 Legion/Zero host-port consumers target `host.docker.internal:18801`, which is why the port survived the Pass-9 engine swap.

**Model history** (full flag-level history in `docker-compose.vllm.yml` header):
- *35B-A3B-FP8 (March 2026)* — ~34 GiB, doesn't fit 32 GB. Retired.
- *QuantTrio Qwen3.6-35B-A3B-AWQ (2026-04-27 + 2026-05-21 attempts)* — OOM/CUDA failures. Root cause identified 2026-06-11: vLLM 0.18/0.19 hybrid-architecture bugs (vllm#41153/#41619) + community quant, NOT the model. Reverted both times.
- *Huihui-Qwen3.6-35B-A3B GGUF on llama.cpp (2026-04-28→2026-05-17)* — retired for llama.cpp structured-JSON bugs + CUDA regression.
- *Qwen3-32B-AWQ (2026-04-27→2026-06-11)* — dense 32B Int4, the stable baseline. ~50 tok/s, 12K ctx, tools off. Remains the documented rollback (see compose header).
- *Qwen3.6-35B-A3B-NVFP4 (2026-06-11, retired same day)* — FlashInfer CUTLASS NVFP4 kernels crash under torch.compile and hang (<1 tok/s then frozen) under eager on this WSL2+Blackwell box. Full attempt log in the compose file.
- *Qwen/Qwen3-8B-AWQ (Pass-6, 2026-08-11, hours only)* → *nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 (Pass-7, 2026-08-11→2026-08-31)* — Mamba-2/MoE hybrid, 3B active, DFlash spec decode. Retired for two measured defects: prefix caching inert (0 hits over 5.35M tokens) and tool calling dead under `enable_thinking:false`, plus 15.8% success over 97k requests once free-cloud saturation dumped ladder traffic on its 6 seqs. Kept as the `nemotron-rollback` compose profile.
- *Qwen3.8-27B via syv-ai/qwen38-27b-rtx3090 (Pass-9, CURRENT, 2026-08-31→)* — see stack table above.
- *cyankiwi/Qwen3.6-27B-AWQ-INT4 (2026-06-24→2026-08-11)* — DENSE 27B, **AWQ-Marlin INT4 kernels** on vLLM v0.23.0-cu129, ctx 16384, tool calling enabled via `qwen3_xml` parser. Served canonically as `Qwen3.5-35B-A3B` (+ alias `qwen3-chat`). Dense was chosen to STRUCTURALLY avoid the NVFP4-MoE running=2 wedge on sm_120 (vllm#35566). Both prior paths are RETIRED as unstable on this Blackwell box: *Qwen3.5-35B-A3B-GPTQ-Int4* (MoE, forced `--enforce-eager` → ~9 tok/s + engine wedges) and *Qwen3.6-35B-A3B-NVFP4* (FlashInfer CUTLASS crash / <1 tok/s hang).

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

**Per-project cloud affinity (2026-06-11, model IDs updated 2026-07-13)** — keeps the three projects off each other's free rate limits:
- **Legion** → NVIDIA NIM: `z-ai/glm-5.2` (reasoning + code — renamed upstream from `glm-5.1` 2026-07-13, alias kept in config.json), `qwen3-next-80b` (fast)
- **ADA** → Kimi K2.6 free via NIM (`nvidia-nim/moonshotai/kimi-k2.6`; the `moonshot/` provider name was a compat shim over NIM, PARKED 2026-07-13 — paid Moonshot retired 2026-06-11). **KNOWN DOWN 2026-07-13**: NVIDIA 404s this model ("Function ... Not found for account") despite listing it in `/v1/models` — an upstream NIM bug, not fixable from our side; ADA falls back to local vLLM / groq in the meantime.
- **Zero** → Gemini Flash (`gemini-3-flash-preview`, 1,500 RPD) + Groq `gpt-oss-120b` (200K TPD). **gemini provider PARKED 2026-07-08** (expired `GEMINI_API_KEY`) — Zero's affinity currently resolves to Groq + local vLLM until the key is rotated.
- Everyone → local vLLM first, `freellm/auto` emergency tail.

## VRAM budget (5090 / 32 GB)

- vllm-chat (cyankiwi/Qwen3.6-27B-AWQ-INT4, dense AWQ-Marlin INT4): ~19 GB weights + KV @ 16K ctx
- vllm-embed (Qwen3-Embedding-0.6B): ~1.5 GB
- cudagraph buffers: ~1-1.5 GB
- Total pinned: ~26 GB (0.92 util cap on chat, 0.12 on embed)

If KV pressure shows up, lower `--max-model-len` on `vllm-chat` (currently 16384) or reduce `--gpu-memory-utilization` (currently 0.92). Do NOT switch KV off fp8.
