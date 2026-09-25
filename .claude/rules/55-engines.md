---
paths: ["docker-compose.vllm.yml", "vllm_wedge_monitor.py"]
---

# 55 — Local engines (path-scoped: docker-compose.vllm.yml, vllm_wedge_monitor.py)

Full engine-swap history: `docs/engine-history.md` (extracted from this
compose file's header comments — read that for "why is the current model
X and not Y", read this file for the operating rules).

## VRAM arithmetic — single RTX 5090, 32 GB, hard ceiling

This card runs `qwen38-chat` (chat/code) and `vllm-embed`'s GPU rollback
profile as co-tenants when both are on GPU; the CPU move of `vllm-embed`
(2026-09-22, Fix-1100000610) exists specifically because two vLLM
processes time-slicing one 5090 measurably starved chat (2.4 tok/s at 30
running vs 631 tok/s with embed off GPU — see `docs/engine-history.md`).
**`--gpu-memory-utilization` PRE-ALLOCATES.** Freeing weight VRAM does not
free card-level VRAM back to the OS — it is handed to the KV cache pool
instead. Do not "helpfully" raise `--gpu-memory-utilization` to chase a
KV-cache-too-small error without checking what else is resident on the
card first; the fix for that error is almost never more utilization, it's
fewer co-tenants or a smaller `MAX_LEN`/`MAX_SEQS`.

Current `qwen38-chat` budget (measured, not assumed): `GPU_UTIL=0.78`,
`MAX_SEQS=32`, ctx 65,536, KV fp8, prefix caching live — chosen so the
card coexists with `vllm-embed`'s GPU profile if that profile is ever
reactivated; an exclusive card (no embed co-tenant) tested at
`GPU_UTIL=0.972`, `MAX_LEN=150000`, `MAX_SEQS=64`. Changing `MAX_SEQS`
changes throughput non-linearly and in the wrong direction sometimes: a
measured comparison found `MAX_SEQS=32` gave n=855 / 60% ok / p50 40.0s
vs `MAX_SEQS=16` giving n=67 / 34% ok / p50 94.3s — latency 2.4x worse AND
lower success at the smaller value. Never change `MAX_SEQS` without a
before/after measurement recorded in `docs/DECISIONS.md` or
`docs/engine-refresh-*.md`.

## Canary/bench gate (binding for any engine or config swap)

**The bar is 397 tok/s aggregate at c32** on the current tuned syv-ai
vLLM 0.27.1 build (`dbirks/Qwen3.8-27B-W4A16-AutoRound`) — this is the
measured baseline, not an aspiration. Any candidate engine, quant, or
flag change must be benched behind a canary profile (`nvfp4-canary` or a
new one) and must land within 5% of that number before it can replace the
live engine. Measured failures on this exact card, for reference before
re-attempting:
- Official-image NVFP4 (`unsloth/Qwen3.8-27B-NVFP4` on vLLM 0.29.0): boots,
  148 tok/s aggregate at c32 — well under the bar. MTP failed to boot.
- `Qwen3.6-35B-A3B-NVFP4` (2026-06-11): FlashInfer CUTLASS kernels crash
  under `torch.compile` and hang under eager on this WSL2+Blackwell box.
- MoE quant paths generally: multiple retirements (GPTQ-Int4 forced
  `--enforce-eager` -> ~9 tok/s; various AWQ/GGUF MoE attempts OOM'd or
  hit vLLM hybrid-architecture bugs). Dense quants have been structurally
  more stable on this card than MoE quants — treat a new MoE candidate
  with extra skepticism and bench it fully before considering a swap.

Re-check the field when vLLM 0.30+ ships a <=18 GB 4-bit build or MTP
boots on sm120 — until then, keep `qwen38-chat` as-is; the lever is
keeping the card exclusive and removing wasted work, not chasing a swap.

## Wedge class (the chronic WSL2+Blackwell failure mode)

Symptom: container reports `healthy`, GPU pegged near 100%, but
`generation_tokens_total` (Prometheus/`/metrics`) is flat and
`num_requests_running > 0` — requests are queued but nothing is being
generated. This is a known class (GDN/HyperQwen-style engine wedge, not a
container crash) that `vllm-wedge-monitor` detects via the metrics
scrape and `vllm-autoheal` (willfarrell-based) recovers via a container
restart. Current cadence: roughly 1 wedge/day, auto-restarted — this is an
ACCEPTED failure mode (see `docs/ACCEPTED_FAILURE_MODES.md`), not
something to "fix" by disabling the monitor.

**Counter-reset bug (fixed by the other WS1 session, still worth knowing
about here):** after an engine restart, `generation_tokens_total` resets
to 0. A rate calculation that doesn't detect the reset will compute a
huge negative rate (`gen_rate=-2798014/s` was observed) and can
false-positive a stall/starved sample on the very next tick after a
legitimate restart. Any change to `vllm_wedge_monitor.py`'s rate
calculation must include a unit test with a counter-reset replay
(`infractl/tests/test_vllm_wedge_monitor_predicates.py`) — this is exactly
the "no finding closed without a test" rule from `00-critical.md`.

## Embed CPU tuning — `--parallel 1 -b 4096 -ub 4096`, and why not `--parallel 4`

`vllm-embed` runs on CPU (`ghcr.io/ggml-org/llama.cpp:server`, NOT vLLM,
NOT the GPU, since 2026-09-22) serving `Qwen/Qwen3-Embedding-0.6B` f16.
Measured: `--parallel 1 -b 4096 -ub 4096` gives p50 49ms / batch-16
0.5-0.7s. `--parallel 4` was tried and REGRESSED this — each parallel slot
gets its own KV allocation (forced equal for embeddings), so 4 slots
means the model competes with itself for the same CPU cores with no
throughput benefit, measured up to p50 4.4s (90x worse) under load. Do
not raise `--parallel` on this service without a fresh before/after
measurement — the CPU-bound nature of a single small model here means
more parallelism is a regression, not a scaling knob, until proven
otherwise on a specific measured workload.

A llama.cpp **CUDA** build (`ghcr.io/ggml-org/llama.cpp:server-cuda-*`,
already referenced in this compose file) is the untested next step for a
faster embed path — a 0.6B f16 model needs roughly 1.5 GiB VRAM and does
short kernel bursts rather than vLLM's continuous batching, so it may not
starve `qwen38-chat` the way the previous GPU vLLM embed service did. Any
attempt at this MUST go behind the same canary/bench gate above (embed
p95 target < 300ms single / < 1s batch-16) before it replaces the CPU
baseline, and the reranker service (if/when built) is a separate service
entirely, not a replacement for embed.
