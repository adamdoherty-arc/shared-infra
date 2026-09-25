# Local chat engine history

Extracted 2026-09-25 (WS7) from `docker-compose.vllm.yml` header comments,
`README.md`'s "Model history" section, and `docs/DECISIONS.md`, summarized
chronologically. The compose file itself is NOT edited by this document —
its header comments remain the byte-level authoritative record for the
service block currently in force; this file exists so the history is
readable without parsing 1,700+ lines of comments. If the compose header
and this file disagree, treat the compose header as more current only for
the CURRENTLY RUNNING service block — the header is known to lag reality
between passes (see `docs/DECISIONS.md` docs-drift notes), so cross-check
`README.md`'s stack table for what's actually live before trusting either
blindly.

## Chat engine (`vllm-chat` -> `qwen38-chat`)

| Date | Model | Outcome |
|---|---|---|
| March 2026 | 35B-A3B-FP8 | ~34 GiB, doesn't fit 32 GB card. Retired before going live. |
| 2026-04-27, 2026-05-21 | QuantTrio Qwen3.6-35B-A3B-AWQ (2 attempts) | OOM/CUDA failures. Root-caused 2026-06-11 to vLLM 0.18/0.19 hybrid-architecture bugs (vllm#41153/#41619), not the model itself. Reverted both times. |
| 2026-04-28 → 2026-05-17 | Huihui-Qwen3.6-35B-A3B GGUF on llama.cpp | Retired: llama.cpp structured-JSON bugs + a CUDA regression. |
| 2026-04-27 → 2026-06-11 | Qwen3-32B-AWQ (dense, Int4) | Stable baseline for this window. ~50 tok/s, 12K ctx, tools off. Remains the documented rollback reference (legacy alias `Qwen3-32B-AWQ` still routed). |
| 2026-06-11 | Qwen3.6-35B-A3B-NVFP4 | Retired same day: FlashInfer CUTLASS NVFP4 kernels crash under `torch.compile` and hang (<1 tok/s then frozen) under eager on this WSL2+Blackwell box. |
| 2026-06-24 → 2026-08-11 | `cyankiwi/Qwen3.6-27B-AWQ-INT4` (dense, AWQ-Marlin INT4) on vLLM v0.23.0-cu129 | Served as `Qwen3.5-35B-A3B` + alias `qwen3-chat`, ctx 16384, tool calling via `qwen3_xml` parser. Dense chosen deliberately to avoid the NVFP4-MoE `running=2` wedge class on sm_120 (vllm#35566). Retired at Pass-6. |
| 2026-08-11 (Pass-6, hours only) | `Qwen/Qwen3-8B-AWQ` | Very short-lived swap, superseded same day by Pass-7. |
| 2026-08-11 → 2026-08-31 (Pass-7) | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` | Hybrid Mamba-2 + MoE, 30B total / 3B active, NVFP4 via Marlin kernel, ctx 32768, DFlash spec decode. Retired for two measured defects: prefix caching inert (0 hits over 5.35M tokens) and tool calling dead under `enable_thinking:false`, plus 15.8% success over 97k requests once free-cloud saturation dumped ladder traffic onto its 6 seqs. Kept cold behind the `nemotron-rollback` compose profile. |
| 2026-08-31 → current (Pass-9, Migration-20) | `dbirks/Qwen3.8-27B-W4A16-AutoRound` on syv-ai/qwen38-27b-rtx3090 patched vLLM 0.27.1 | Dense 27B, W4A16 + int4 lm_head/embeddings, batch profile, ctx 65,536, `MAX_SEQS=32`, KV fp8, prefix caching live, tools enabled (`qwen3_coder` parser, works under `enable_thinking:false`). Measured 397 tok/s aggregate at c32 — current canary-gate bar. Serves ONE true name `qwen3.8-27b`; 8 legacy aliases remapped in Bifrost's key config so response `model` fields still echo the true model. |

**2026-09 (WS3 research, not yet applied):** the official-image NVFP4 path
(`unsloth/Qwen3.8-27B-NVFP4` on vLLM 0.29.0) was benched on this exact
card — boots, but delivers 148 tok/s aggregate at c32, well under the 397
tok/s bar; MTP failed to boot. Verdict: keep `qwen38-chat`; re-check when
vLLM 0.30+ ships a ≤18 GB 4-bit build or MTP boots on sm120.

## Why legacy alias names never get cleaned up

`Qwen3.5-35B-A3B`, `qwen3-chat`, `Qwen3.6-27B`, `Qwen3-32B-AWQ`,
`Qwen3.6-35B-A3B`, `gpt-oss-20b`, `nemotron-3.5-lightning`, `local-chat`
are all DEPRECATED COMPATIBILITY NAMES that answer with whatever the
current engine is — none of them describe the current weights.
`qwen3-chat` is the one intentional exception: ADA's/Legion's stable ROLE
alias ("whatever answers local chat today"), deliberately swap-proof by
design. The other names persist because ~350 Legion/Zero/ADA call sites
reference them directly and retiring any one is a cross-repo migration
(dozens of hardcoded literals in Legion's `unified_llm_service.py` /
`legion_config.py` / `enums.py` / Alembic data migrations alone) — not a
shared-infra-only change.

## Embed engine (`vllm-embed`)

| Date | Config | Outcome |
|---|---|---|
| through 2026-09-21 | vLLM GPU, `Qwen/Qwen3-Embedding-0.6B` | Co-tenant with chat on the same 5090; measurably starved chat (2.4 tok/s at 30 running vs 631 tok/s recovered by a co-tenant restart alone). |
| 2026-09-22 (Fix-1100000610) → current | `ghcr.io/ggml-org/llama.cpp:server` (CPU), Qwen3-Embedding-0.6B-GGUF f16, `--parallel 1 -b 4096 -ub 4096` | Measured p50 49ms single / 0.5-0.7s batch-16. Moved off GPU specifically to stop starving chat. |

See `.claude/memory/topics/engine-tuning.md` for the full rejected-candidate
table and current measured numbers, and `.claude/rules/55-engines.md` for
the canary/bench gate any future swap must clear.
