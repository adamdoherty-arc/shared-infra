# Engine tuning — dated facts

Last verified: 2026-09-25. Full chronological history:
`docs/engine-history.md`. Operating rules (canary gate, VRAM arithmetic):
`.claude/rules/55-engines.md`. This file is measured numbers only.

## Current chat engine — `qwen38-chat` (Pass-9, live since 2026-08-31)

- Model: `dbirks/Qwen3.8-27B-W4A16-AutoRound` on syv-ai/qwen38-27b-rtx3090
  patched vLLM 0.27.1.
- Config: `GPU_UTIL=0.78`, `MAX_SEQS=32`, ctx 65,536, KV fp8, prefix
  caching live, tools enabled (`qwen3_coder` parser, works under
  `enable_thinking:false`).
- Measured baseline: **397 tok/s aggregate at c32** — this is the
  canary-gate bar for any candidate replacement.
- Host port 18801 (kept across the engine swap for ~15 Legion/Zero
  consumers) -> container port 18020 (the stack's native port sits inside
  a Windows WinNAT excluded range and cannot be host-bound).

## Rejected/retired candidates (don't re-attempt without new evidence)

| Candidate | Result | Why retired |
|---|---|---|
| `unsloth/Qwen3.8-27B-NVFP4` on vLLM 0.29.0 | 148 tok/s aggregate at c32 | Under the 397 tok/s bar; MTP failed to boot |
| `Qwen3.6-35B-A3B-NVFP4` | <1 tok/s then frozen | FlashInfer CUTLASS kernels crash under `torch.compile`, hang under eager on this WSL2+Blackwell box |
| `Qwen3.5-35B-A3B-GPTQ-Int4` (MoE) | ~9 tok/s | Forced `--enforce-eager` |
| `QuantTrio Qwen3.6-35B-A3B-AWQ` (x2 attempts) | OOM/CUDA failures | vLLM 0.18/0.19 hybrid-architecture bugs (vllm#41153/#41619), not the model |
| `Huihui-Qwen3.6-35B-A3B` GGUF on llama.cpp | retired | structured-JSON bugs + CUDA regression |
| Nemotron-3.5-Lightning-30B-A3B-NVFP4 (Pass-7) | retired after weeks live | prefix caching inert (0 hits/5.35M tokens), tool calling dead under `enable_thinking:false`, 15.8% success at 97k req under saturation |

Pattern: dense quants have been structurally more stable on this card
than MoE quants (avoids the running=2 wedge class on sm_120, vllm#35566).
Treat any new MoE candidate with extra skepticism.

## Embed engine — `vllm-embed`, CPU since 2026-09-22 (Fix-1100000610)

- `ghcr.io/ggml-org/llama.cpp:server` (CPU), `Qwen/Qwen3-Embedding-0.6B-GGUF`
  f16, flags `--parallel 1 -b 4096 -ub 4096 --cache-ram 2048`.
- Measured: p50 49ms single, 0.5-0.7s batch-16.
- `--parallel 4` was tried and regressed to p50 up to 4.4s — do not raise
  parallelism on this service without a fresh measurement (each slot gets
  its own KV allocation for embeddings; more slots = more CPU contention,
  not more throughput, for this workload).
- TEI CPU was benched and rejected (1.5s p50 measured — worse than
  llama.cpp CPU).
- Untested next step: llama.cpp CUDA build, ~1.5 GiB VRAM for this model
  size, short kernel bursts (may not starve chat the way GPU vLLM embed
  did) — must go behind the same canary gate before adoption.

## Reranker — does not exist yet (as of 2026-09-25)

Planned recipe: `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` (official
conversion, includes `cls.output.weight` — community GGUFs of this model
are broken, scores ~1e-23) via `llama-server --reranking --pooling rank`,
`/v1/rerank`. No Bifrost passthrough for rerank exists; consumers would
call the service directly.
