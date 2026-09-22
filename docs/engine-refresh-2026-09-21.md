# Engine refresh research — 2026-09-21 (WS4, Legion sprint 15018)

Preparation-only research for the Qwen3.8-27B local engine refresh (current: `qwen38-chat`,
vLLM 0.28.0 syv-ai/HyperQwen patch stack, sm86-tuned, running on an sm120 RTX 5090). No running
container was touched to produce this doc.

## Baseline bench context — daemon restart during this pass (2026-09-21 ~13:16 ET)

`reports/engine-bench-baseline-2026-09-21.json` (first run, ~13:09 ET) captured a genuine
**0% success at Bifrost's edge** — every call at concurrency 1 and 8 got `HTTP 503
request_dropped: queue is full`. This is a real, live measurement of the exact Part B defect
(256-slot `vllm-local` buffer saturated during market hours), not a bench-script bug — do not
discount it. Separately, and NOT caused by this pass: Docker Desktop's engine restarted host-wide
at 13:16:53 ET (settings-store.json reload from another peer session — traced by the main loop,
unrelated to WS4/WS6) and cycled every container, including `qwen38-chat`/`vllm-embed`/
`shared-bifrost`, via the `SharedInfra-Stack`/`Legion-Stack` nssm services' own `compose up`. This
pass touched no running container before, during, or after that restart. A second baseline run
was taken once `qwen38-chat` reported `healthy` again post-restart —
`reports/engine-bench-baseline-2026-09-21-post-restart.json` — to get one clean pre-cutover
number; **both files are kept** since the first is real evidence of the saturation defect and the
second is the calmer comparison point WS4.2's cutover gate should bench against.

**The "post-restart" number is NOT a clean baseline — it caught the engine mid-recovery, not
mid-calm.** `qwen38-chat`'s `/health` reported `healthy` (the liveness probe passed), but
`docker logs` at that exact moment showed `Avg generation throughput: 0.0-2.6 tokens/s` with
`Running: 29-31 reqs` — the wedge-monitor's own decode-starvation signature (its log shows the
same pattern independently: `decode-starved sample 1/8` events bracketing the restart window).
Result: 0% success at both concurrency 1 and concurrency 8 (every call hit the script's 120s
timeout cap — an earlier draft of this run briefly showed 25% at concurrency 1 before the file
was overwritten by this same background invocation finishing later), all three probes timed out.
**This is a second real, live measurement of a defect this program already names** (the
GDN/decode-starvation wedge class, Part D) — not a broken bench. A third attempt was started once
generation throughput visibly recovered (35.2 tok/s
observed in logs) but its stdout was fully buffered behind a `| tail` pipe in a backgrounded
shell and never surfaced before this pass had to hand off; the file
`reports/engine-bench-baseline-2026-09-21-post-restart.json` therefore still holds the
mid-recovery (degraded) numbers, not a calm-state number. **WS4.2's own cutover-gate bench
(`engine_cutover.sh`'s `bench_gate`, run fresh at cutover time) is the number that actually gates
the swap — treat both files here as evidence of current instability, not as the pre-cutover
control.**

## (a) vLLM release + flags for Qwen3.8-27B NVFP4 on a single RTX 5090 (sm120)

**Source: https://recipes.vllm.ai/Qwen/Qwen3.8-27B — the "1x RTX 5090" section (fetched
2026-09-21), the only page that gives a verified single-card serve command.**

```
vllm serve Inferact/Qwen3.8-27B-NVFP4 \
  --tensor-parallel-size 1 \
  --max-model-len 32768 \
  --kv-cache-dtype fp8 \
  --enforce-eager \
  --reasoning-parser qwen3
```

- **`--enforce-eager` is mandatory on one 5090**, not optional: "Without `--enforce-eager`,
  startup dies in CUDA graph capture" (OOM). **This directly contradicts the plan's assumption
  of "CUDA graphs at 64K context" on a single card** — the recipe's own two-card section is the
  only one that runs CUDA graphs; the single-card section runs eager only. Recorded here rather
  than silently ignored (rule: no fabricated capability).
- **Max context on a single 5090 is 32,768, not 65,536.** The doc states this is a hard VRAM
  ceiling ("the single card's 31.4 GiB usable memory") and that "raising or lowering
  `--gpu-memory-utilization` does not help because CUDA graph capture allocates outside the
  standard memory budget" (moot here since we run eager, but the 32K ceiling itself is stated
  independent of that). At `--enforce-eager` + 32K max-model-len the recipe measures a **KV pool
  of ~91,022 tokens** on an exclusive card.
- Model repo used by the verified single-card command: **`Inferact/Qwen3.8-27B-NVFP4`**.
  `unsloth/Qwen3.8-27B-NVFP4` (Apache-2.0, Unsloth Dynamic V3.0 quantization, HF search
  2026-09-21) and `RedHatAI/Qwen3.8-27B-NVFP4` (W4A4) are alternate NVFP4 builds of the same
  model; Unsloth's own docs describe their build as "mixed-precision (FP8 channel-wise
  alongside 4-bit groups)" leaving more room for KV cache than a uniform W4A4 build — worth a
  second canary variant once the first is validated, not a blocker.
- **MTP speculative decode is NOT part of the verified single-card command.** The two-card
  (`adrienbrault/qwen3.8-27b-rtx5090`) build documents a DFlash2/MTP drafter path
  (`--speculative-config`) alongside `VLLM_SM12X_PCIE_IPC_AR=1` cross-card all-reduce and
  `--kv-cache-memory-bytes` pinning — none of which apply to one card. vLLM 0.29.0 does ship
  "fused GDN MTP for all Qwen head ratios" natively (vllm-project release notes, 2026-09-09), so
  MTP is architecturally possible on one card, but **no source in this research confirms it
  working single-GPU with `--enforce-eager`** — the HyperQwen GDN wedge (issue #107, still open
  as of this fetch) is specifically in the compiled GDN forward pass, and MTP adds another
  compiled path on top of it. **Decision: MTP is left OFF (commented, documented) in the canary
  until it can be soak-tested; shipping it unverified as "on" would be exactly the fabricated
  capability the no-stub rule forbids.**
- Tool calling: `--enable-auto-tool-choice --tool-call-parser qwen3_xml` is the parser named in
  the plan and matches vLLM's own Qwen3 tool-parser naming convention; not contradicted by any
  source above (recipes.vllm.ai's own multi-card sections use `--tool-call-parser qwen3_xml`
  alongside `--reasoning-parser qwen3`).
- `enable_thinking:false`: single confirmed mechanism across every source is
  `--default-chat-template-kwargs '{"enable_thinking":false}'` (same flag ADA's current
  `qwen38-chat` block already uses) — no source suggests this changed in 0.29.
- **Image tag: `vllm/vllm-openai:v0.29.0`** (WebSearch, 2026-09-21 — GitHub release
  `vllm-project/vllm` tag `v0.29.0`, 594 commits / 277 contributors; CUDA 13.0 default image,
  CUDA 12.9 variant also published). This is the OFFICIAL vLLM image, not a syv-ai/HyperQwen
  fork — the whole point of the refresh is to stop carrying a 3090-tuned third-party patch
  stack; the official image is the sm120-native path per the recipe.
- **Known open defect, same defect class as today's wedge**: HyperQwen issue #107 (vLLM
  0.28.0, still open) — engine core stops stepping 20 s–28 min, py-spy parks in the compiled GDN
  linear-attention forward. Maintainer-suggested canaries, in priority order: disable
  `--async-scheduling`, `VLLM_MAMBA_ALIGN_KEEP_CHECKPOINTS=0`, `VLLM_DRAFT_TOPK_TOPP=0`,
  `--enforce-eager` with reduced CUDA-graph capture (moot for us — already eager), disable MTP,
  disable KV offload. **No discussion of RTX 5090/sm120/NVFP4 in that thread** — it is a 3090
  report; whether the same GDN kernel path exists in 0.29's rewritten GDN MTP kernels is
  unverified and exactly why WS4.2's 6-hour soak (not yet run — see below) is the gate, not this
  research.
- **Confirmed working by someone**: 32-concurrent / `enable_thinking:false` / guided-JSON on a
  single 5090 with this exact stack is **NOT confirmed by any source fetched** — the
  `adrienbrault` two-card numbers (216 tok/s single-stream, 1,476 tok/s @8, 2,596 @16, SWE-bench
  77.6%, tool-eval 88.5–90.5%) are a **two-card host** and cannot be assumed for one card. This
  is why the plan gates the cutover on WS4.2's own bench against ADA's real prompt mix rather
  than trusting the upstream numbers.

### VRAM arithmetic for the canary (ADA's actual card, not the recipe's exclusive card)
The recipe's own single-card ceiling (32.6 GiB card, 31.4 GiB usable, 32K max-model-len,
enforce-eager) assumes an **exclusive** card. ADA's box also carries `vllm-embed` (0.12 util,
GPU_UTIL fraction of vLLM's own accounting) and ~1.5–2 GiB Windows WDDM desktop reservation —
the same headroom problem `qwen38-chat`'s own compose comments already measured (26.0 GiB free
with vllm-chat stopped, embed+desktop resident). See the compose block below for the resulting
`--gpu-memory-utilization` arithmetic; the canary is `profiles: ["nvfp4-canary"]`-gated so it
never launches by default and never contends with the live engine.

## (b) Bifrost v2.0.0 logging store configuration

**Source: https://docs.getbifrost.ai/features/observability/default (fetched 2026-09-21).**

- SQLite path key: **`logs_store.config.path`**, default `"./logs.db"` — this is the exact
  config.json key to redirect the file (e.g. onto a named Docker volume instead of the Windows
  bind mount that causes the 3x-fsync + WAL-lock-storm class of failure this plan's Part B
  documents).
- Disabling request/response body logging: **`logs_store.config.disable_content_logging`**
  (bool) — "prevents logging of request/response content while preserving usage metadata like
  latency and token counts." This does not fix the lock-storm (that's about file I/O contention,
  not payload size) but reduces logs.db write volume and is a legitimate second lever.
- **Postgres IS a supported alternative log store**: `logs_store.config.type = "postgres"` with
  `host` / `port` / `user` / `password` / `db_name` / `ssl_mode` keys. This is the structural fix
  for the SQLite-on-Windows-bind-mount problem — Postgres already exists on this host
  (native, port 5432, used by ADA/Legion/Zero) and a shared-infra logs database there removes
  the SQLite lock entirely. **Recommended target for WS2 Bifrost.1, pending owner sign-off**:
  point `logs_store` at a new `bifrost_logs` database on the existing native Postgres instance
  instead of moving `logs.db` to a Docker volume — a volume only fixes the fsync-cost problem,
  Postgres fixes both that and the single-writer SQLite lock contention Part B measured
  ("database is locked" 28-48x/day).
- **Not documented anywhere fetched**: an explicit `busy_timeout` key, or a WAL/journal_mode
  toggle, for the SQLite path. If the SQLite path is kept (owner declines the Postgres move),
  the only known lever is moving `path` off the Windows bind mount onto a named Docker volume
  (overlay fs, no 9p/CIFS translation layer) — consistent with the existing
  `reference_bifrost_logs_db_host_open_hazard_2026_09_15` memory's root cause (host-side opens
  of the live file ghost the -wal/-shm files because 9p bind mounts don't share SQLite locks).
- Retention: no explicit retention-period config key found in the fetched doc; ADA's own
  `bifrost-logs-pruner` sidecar (2-day retention, in-container `quick_check`) already handles
  this operationally and is unaffected by either the volume-move or Postgres option.

## Recommendation carried into WS4 / WS2

1. **WS4 canary**: `qwen38-nvfp4` service (below), official `vllm/vllm-openai:v0.29.0`,
   `Inferact/Qwen3.8-27B-NVFP4`, `--enforce-eager`, `--max-model-len 32768` (not 65536 — the
   plan's 64K figure is not achievable on one 5090 per vLLM's own recipe; shipping 65536 would
   either refuse to boot or silently truncate context, both worse than stating the real ceiling
   here), MTP left off pending a soak test, tool-call-parser `qwen3_xml`, thinking off. Host port
   **18803**, not 18802 as first drafted — 18802 is already declared by `fish-speech` (`tts`
   profile, same compose file), caught on review before the canary ever booted.
   `engine_cutover.sh --bench-variants` boots all three candidate configs in turn (`eager`
   = the shipped compose default; `graphs` = drops `--enforce-eager`, raises to 65536,
   `--compilation-config max_cudagraph_capture_size=32`; `mtp` = eager base +
   `--speculative-config method=mtp,num_speculative_tokens=3`), benches each with
   `engine_bench.py`, and prints a summary — this is a **measurement tool**, not a claim that
   `graphs` or `mtp` work on this card: no source found in this research confirms either beyond
   the eager/32K config, so the script exists to find out rather than to assert.
2. **WS2 Bifrost.1**: prefer the Postgres log-store move over the Docker-volume move — same
   owner-sign-off gate, strictly better fix (removes the SQLite lock class entirely, not just
   the fsync-cost multiplier). Both options are documented so the owner can pick either.

## WS4.2 isolated bench results (2026-09-21, ~18:00-19:00 ET, off-hours, owner-approved)

The 13:50 ET baseline attempt above produced no usable numbers (live engine saturated,
every probe timed out). This is the isolated re-run: each canary variant benched with
qwen38-chat fully stopped (no production contention), then qwen38-chat restored and
benched immediately at the same levels as the "clean" comparison point. Concurrency
1/8/16/32, n=8/level, max_tokens=1000, real ADA prompt mix (`llm_call_log` top-8 call
sites), via the extended `scripts/engine_bench.py` (added `--max-tokens`, per-request
tok/s, a `vllm:num_requests_running` metrics read, and a thinking-disabled probe that
checks for a literal think tag in the response).

**Two structural bugs found and fixed in this pass, before any variant could be
measured cleanly** (both previously "verified by inspection only", never executed):

1. `scripts/engine_cutover.sh` exported `MSYS_NO_PATHCONV=1` globally. That broke every
   `docker compose -f "$VLLM_COMPOSE"` call (`$VLLM_COMPOSE` is a Git-Bash-style
   `/c/...` path from `cd && pwd`, which needs normal MSYS translation to reach
   docker.exe) with `open C:\c\code\shared-infra\docker-compose.vllm.yml: The system
   cannot find the path specified.` Fixed: the override is now scoped to only the one
   `docker run` inside `launch_canary_variant` that has container-side paths needing
   protection; every `docker compose -f` call relies on normal conversion.
2. `vllm-wedge-monitor` had `depends_on: qwen38-chat` (a static compose key) plus
   hardcoded (non-interpolated) `VLLM_METRICS_URL`/`VLLM_TARGET_CONTAINER` values, so
   `VLLM_TARGET_CONTAINER=qwen38-nvfp4 docker compose up -d --force-recreate
   vllm-wedge-monitor` (step 1 of `cmd_commit`/`cmd_bench_variants`, run immediately
   after stopping qwen38-chat) **always restarted qwen38-chat as a compose
   dependency**, regardless of the env var. Live-observed: qwen38-chat came back
   up seconds after being stopped, fighting the canary for the same 32.6 GiB card.
   Fixed: `depends_on` removed (the monitor's own `STARTUP_GRACE_S` /
   `UNREACHABLE_STARTUP_GRACE_S` already tolerate the target being down or still
   warming up) and both env values now interpolate
   `${VLLM_TARGET_CONTAINER:-qwen38-chat}` for real.

**Capacity finding (the main result of this pass): the researched single-card recipe
does not fit ADA's actual card.** vLLM 0.29.0 measured the `Inferact/Qwen3.8-27B-NVFP4`
checkpoint at **24.18-24.99 GiB of GPU weight memory** — 8-9 GiB more than the WS4
research doc's ~16 GiB assumption (itself the vLLM recipe's own *exclusive*-card
arithmetic). vLLM also reports the card as **31.84 GiB total / ~30.2 GiB free** even
with qwen38-chat fully stopped (vllm-embed + Windows WDDM desktop reservation account
for the rest), not the 32.6 GiB nvidia-smi reports as "total". At the plan's spec'd
`--max-num-seqs 32 --max-model-len 32768 --gpu-memory-utilization 0.78`: hard failure,
`Available KV cache memory: -6.86 GiB` (ValueError, no room for cache blocks) right
after weight load. At `--gpu-memory-utilization 0.95`: refused at the pre-flight check
before loading anything (free memory below the requested utilization). At a reduced
`--max-num-seqs 8 --max-model-len 16384 --gpu-memory-utilization 0.92`: no hard error,
but the boot stalled indefinitely inside the encoder-cache profiling step (CPU pegged
near 100 percent on one core, GPU memory pinned near-full) past the 15-minute cutoff,
both for `eager` and for `graphs` (which also never got past a slow safetensors
reload — checkpoint 24.57 GiB against 23.5-26.1 GiB measured available host RAM
triggers vLLM's own auto-prefetch-disable path and repeated full-disk shard reads on
every fresh boot, 3.5-4 min just for I/O). `mtp` was not attempted: it can only add
VRAM pressure on top of a config (`eager`) that already measured negative KV-cache
headroom, so a time-boxed attempt would not have told us anything eager's hard failure
had not already.

| Variant | Boot result | Bench | Notes |
|---|---|---|---|
| eager (spec: 32 seqs / 32768 ctx / util 0.78) | FAILED - ValueError, KV cache -6.86 GiB | not run | weights alone measured 24.18-24.99 GiB |
| eager (reduced: 8 seqs / 16384 ctx / util 0.92) | FAILED - stalled >15 min in encoder-cache profiling | not run | CPU 100% one core, GPU near-full, no crash, no progress |
| graphs (65536 ctx, CUDA graph capture) | FAILED - stalled >15 min in safetensors reload | not run | same RAM-vs-checkpoint thrashing pattern |
| mtp (eager + speculative decode) | NOT ATTEMPTED | not run | strictly worse VRAM than eager, which already failed |
| qwen38-chat (current engine, clean, isolated re-bench) | healthy | see below | qwen38-chat had already auto-recovered from an unrelated mid-session CUDA driver crash (`torch.AcceleratorError: CUDA error: unknown error`) via `restart: unless-stopped` before this bench ran |

### Clean current-engine (qwen38-chat) bench - `reports/engine-bench-2026-09-21-current-clean.json`

| Concurrency | Success % | p50 | p95 | Aggregate tok/s | Per-req tok/s (median / p95) | num_requests_running before / during |
|---|---|---|---|---|---|---|
| 1 | 100.0 | 5.516s | 11.614s | 33.91 | 48.96 / 53.7 | 2 / 14 |
| 8 | 100.0 | 3.931s | 8.163s | 165.6 | 39.45 / 44.92 | 14 / 9 |
| 16 | 100.0 | 3.497s | 6.381s | 173.31 | 36.72 / 38.26 | 9 / 8 |
| 32 | 100.0 | 3.736s | 12.755s | 98.73 | 27.3 / 37.63 | 8 / 7 |

Probes: tool-call PASS (0.727s), guided-JSON PASS (0.581s), thinking-disabled PASS (no
think tag emitted, 35 completion tokens), prefix-cache likely-hit (0.985s to 0.197s, 80
percent faster on the repeat). `num_requests_running` was never 0 during this "clean"
run - ADA's admission cap (10, since the 22:00 UTC restart) plus this bench's own
concurrent load together explain the 7-14 range; this is the realistic operating
point, not an idle-engine number, matching the plan's own framing of what a clean
baseline means here.

### Recommendation

Do not cut over. No NVFP4 canary variant produced a servable engine in this pass -
`eager` hard-failed on VRAM, `graphs` and the reduced-`eager` retry both stalled past
the time budget, and `mtp` was not worth attempting given eager's failure. The
acceptance bar (success >= 95.6 percent, tool + guided-JSON pass, aggregate tok/s at 32
concurrent >= 2x current) cannot be evaluated because no variant reached a bench-able
state. The current engine (qwen38-chat) is confirmed healthy and productive
post-bench: 100 percent success at every concurrency level tested (1/8/16/32), all
four probes pass, health endpoint returns 200, a live 5-token completion returns
"OK." - left running, untouched otherwise, as required.

What would need to change before a re-attempt is worth scheduling: either (a) a
smaller or different NVFP4 build with a genuinely single-digit-GiB-smaller weight
footprint (the plan's own doc already named `unsloth/Qwen3.8-27B-NVFP4` and
`RedHatAI/Qwen3.8-27B-NVFP4` as untried alternates - worth checking their measured
footprint before assuming they fit either), or (b) freeing more of the card (stopping
`vllm-embed` for the duration of a bench, which was not done in this pass since
production embedding calls were still a live concern during the off-hours window), or
(c) more host RAM or a faster checkpoint path so the 24.57 GiB weight file does not
thrash against a 24-26 GiB RAM ceiling on every fresh load. None of these were tried
here - recording them as the next steps rather than doing them unauthorized mid-pass.

Raw reports: `reports/engine-bench-2026-09-21-eager.json` (+ `-eager-boot-log.txt`),
`reports/engine-bench-2026-09-21-graphs.json` (+ `-graphs-boot-log.txt`),
`reports/engine-bench-2026-09-21-mtp.json`, `reports/engine-bench-2026-09-21-current-clean.json`.

## Addendum 2026-09-21 23:05 UTC -- model-check verdict (main loop)

Checkpoint sizes measured from the Hub (`safetensors` bytes), which is why no NVFP4 build of
Qwen3.8-27B fits beside `vllm-embed` on one 32 GiB card (vLLM sees 31.84 GiB, ~30.2 GiB free
with the chat engine stopped; the plan's "~16 GiB" premise was wrong for this family -- the
vision tower, GDN layers and the FP8 `lm_head` stay above 4-bit):

| Checkpoint | Format | Weights on disk |
|---|---|---|
| `unsloth/Qwen3.8-27B-NVFP4` | compressed-tensors, Apache-2.0 | 22.57 GB + 0.85 GB MTP |
| `RedHatAI/Qwen3.8-27B-NVFP4` | llm-compressor W4A4 | 23.84 GB + 0.85 GB MTP |
| `Inferact/Qwen3.8-27B-NVFP4` (benched) | ModelOpt | 25.5 GB (measured 24.2-25.0 GiB GPU) |
| `dbirks/Qwen3.8-27B-W4A16-AutoRound` (current) | Marlin int4 | fits at `GPU_UTIL=0.78` with 254k-token fp8 KV |

`XingChen-AGI/Xing4.0-29B-A4B` (MoE, 4B active): BF16 only (~58 GB) plus llama.cpp GGUF
forks as of 2026-09-21; no vLLM-loadable NVFP4/AWQ/GPTQ build exists, so it is not a
candidate for the batch lane yet. Re-check when an official 4-bit build lands.

Verdict: the current engine stays. In isolation it benches 100% success at concurrency
1/8/16/32 with tool-call, guided-JSON, thinking-off and prefix-cache probes passing
(`reports/engine-bench-2026-09-21-current-clean.json`); every 0%-success episode today was
demand exceeding ~100-145 tok/s aggregate (raw-SDK retries + no admission), fixed on the ADA
side (max_retries=0 everywhere, derived deadline-aware admission, direct-dial abort). The
refresh becomes viable when either (a) a <=18 GB 4-bit Qwen3.8-27B build appears, (b)
`vllm-embed` moves off the card (CPU embeddings for 0.6B are ~10 ms/call), or (c) the card
is replaced. Option (b) is the cheapest and frees ~4 GiB, enough for `unsloth` NVFP4 with a
32k context -- it is the next experiment, off-hours, behind the same canary/bench gate.

## Addendum 2026-09-22 02:14-02:40 UTC -- Fix-1100000610: vllm-embed moved off the GPU

Option (b) from the verdict above, done same-session rather than deferred. Live finding
(`docker logs qwen38-chat` + `vllm-wedge-monitor`): with `vllm-embed` (vllm/vllm-openai:v0.22.1,
`Qwen/Qwen3-Embedding-0.6B`, GPU util 0.12, ~32k embed calls/day from ADA/Legion/Zero) busy,
qwen38-chat's generation throughput fell to 2.4 tok/s at 30 running; the wedge monitor restarted
vllm-embed and generation immediately went 354 -> 631 tok/s. Two vLLM processes time-slicing one
RTX 5090 starve the chat engine regardless of how small embed's util fraction is set to. Fix:
move embeddings off the GPU entirely so the chat engine owns the card.

**What was tried and rejected first: TEI (text-embeddings-inference) CPU image.** TEI's CPU
build only runs an ONNX Runtime backend by default, and `Qwen/Qwen3-Embedding-0.6B` ships no
ONNX export on the Hub (`onnx/model.onnx` 404s). TEI does carry a Candle CPU fallback for the
Qwen3 architecture and it boots (`ghcr.io/huggingface/text-embeddings-inference:cpu-latest
--model-id Qwen/Qwen3-Embedding-0.6B`), but at any batch-tokens setting large enough to survive
warmup without OOM (`--max-batch-tokens 2048`, forced `max_batch_requests=4` by the backend), a
canary measured **single-request p50 ~1.5s and a 16-item batch median ~24s** -- 10-150x over the
budget in the task brief (<=150ms single, <=1s batch-16). Rejected.

**What shipped: llama.cpp CPU server on the official GGUF.** `ghcr.io/ggml-org/llama.cpp:server`
(CPU build, no `-cuda` suffix) serving `Qwen/Qwen3-Embedding-0.6B-GGUF`'s official `f16` build
(not a third-party quant -- published by the `Qwen` org itself) via `--embedding --pooling last`.
Two tuning findings, both counterintuitive and both load-bearing (documented inline in
`docker-compose.vllm.yml`'s `vllm-embed:` block so nobody "fixes" them back):

1. **Quant choice: f16, not Q8_0.** Both were canaried and dimension-matched (1024, same as the
   retired GPU engine) against 5 ASCII+unicode test strings. Cosine similarity vs the GPU
   embedding: Q8_0 ranged 0.99885-0.99993 (one string missed the >=0.999 parity bar); f16 ranged
   0.99990-0.99993 (every string cleared it). f16 is 2x Q8_0 on disk (~1.2 GiB vs ~0.6 GiB) but
   that costs zero GPU VRAM on a CPU host with 42 GiB RAM -- correctness bought essentially free.
2. **Batching: one big ubatch, not more parallel slots.** The instinctive tuning move (more
   concurrent slots for more throughput) measured WORSE on this workload: `--parallel 4` with the
   default `-b 2048 -ub 2048` (llama.cpp forces batch==ubatch for embedding mode) gave
   single-request p50 up to 4.4s and a 16-item batch up to 26s -- multi-slot CPU decode contention
   with no GPU parallelism to actually exploit. `-b 4096 -ub 4096 --parallel 1` (one slot, one
   ubatch large enough that a full 16-item batch is one encoder pass) measured **single-request
   p50 48.6ms (n=15, p95 126.5ms) and a 16-item batch median 0.525-0.737s** -- both inside budget,
   measured under REAL concurrent host load (qwen38-chat mid-generation at 400% CPU,
   ada-scheduler, ada-frontend all running, not an idle box).

**Cutover, zero Bifrost restart.** Bifrost's `embed-local` provider already points at
`http://vllm-embed:8001` (`bifrost/config.json`); the new service keeps the same
`container_name: vllm-embed` and the same internal port 8001, so Docker's per-container DNS
just re-resolved on the next request -- no config.json edit, no Bifrost restart, verified live:

```
docker exec ada-backend python3 -c "... POST http://shared-bifrost:8080/v1/embeddings
  model=embed-local/Qwen/Qwen3-Embedding-0.6B ..."
-> status 200, dims 1024, model "Qwen/Qwen3-Embedding-0.6B"
```

The GPU service is kept, not deleted, behind `profiles: ["embed-gpu-rollback"]` under the new
name `vllm-embed-gpu-rollback` (its own `container_name`, so it never collides with the live CPU
`vllm-embed` if someone brings both up by mistake). `vllm-wedge-monitor`'s
`CO_TENANT_CONTAINERS` env is now pinned to `""` in compose -- its 2026-09-21 "restart the GPU
co-tenant first" escalation is moot once the co-tenant is not on the GPU, and leaving the code
default (`"vllm-embed"`) unexplained would have meant the monitor issuing a pointless
`docker restart vllm-embed` against a CPU container on every future GPU spin.

**Immediate GPU effect** (`nvidia-smi`, before -> ~90s after cutover, `qwen38-chat` untouched):
28,644 MiB used @ 71% util vs. the pre-cutover 31,429 MiB @ 96% -- **~2.8 GiB VRAM reclaimed**
with `vllm-embed` no longer resident on the card at all (not just at a smaller
`--gpu-memory-utilization`).

**15-minute post-cutover measurement** (cutover 02:37:33 UTC, window closed 02:52:34 UTC,
real production traffic the whole time -- not a synthetic bench):

| Metric | Value |
|---|---|
| `qwen38-chat` generation throughput | 30 samples over 15 min, range 85.6-586.7 tok/s, no sample below 85 tok/s, `Waiting: 0` on every sample but one (`Waiting: 1` for a single 10s poll) -- no wedge, no queue backup |
| `vllm:num_requests_running` / `waiting` (spot read at window close) | running=2, waiting=0, `waiting_by_reason{capacity}=0`, `waiting_by_reason{deferred}=0` |
| ADA `llm_call_log` provider=vllm-local success % (last 15 min) | 79/130 = 60.8% -- all 51 failures are `LocalLaneAdmissionRefused: local_lane_token_s...` (4 are `local_lane_hard_in...`), ADA's own client-side admission gate refusing BEFORE the call reaches vLLM (the "derived deadline-aware admission" already named in this doc's 23:05 UTC addendum above); zero vLLM-side errors, zero timeouts, zero connection failures. Not caused by, or worsened by, this pass's embed cutover -- pre-existing ADA-side admission behavior under real market-hours-adjacent load, unrelated to the GPU change |
| `embed-local` success % + p95 latency (last 15 min) | 5/5 = 100%, p95 783.9ms on real production batch calls (batches of up to 32 texts per `backend/infrastructure/ai_client.py`'s `batch_size=32`, not the single-string canary number above -- individual samples 517-806ms for a full batch, consistent with the canary's batch-16 0.5-0.7s measurement) |
| `nvidia-smi` memory.used / utilization.gpu | 28,674 MiB / 95% (vs. pre-cutover 31,429 MiB / 96%) -- **~2.76 GiB VRAM reclaimed and held** 15 minutes after cutover, not just at the instant of the move |

**Verdict: cutover holds.** `qwen38-chat` ran the full 15-minute window with zero waiting
requests and no throughput collapse (the defect this pass exists to fix); `embed-local` served
100% of ADA's real embedding traffic from the CPU service with no Bifrost restart and no config
change. The one non-100% number (`vllm-local` 60.8%) is a distinct, already-tracked, client-side
admission-control behavior with no vLLM-side failures in it -- reported here for completeness, not
rolled into the embed-cutover verdict, and not something this pass touches (its "fixed on the ADA
side" work is the 23:05 UTC addendum above, from an earlier pass).
