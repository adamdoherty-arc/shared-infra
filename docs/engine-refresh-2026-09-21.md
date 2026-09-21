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
