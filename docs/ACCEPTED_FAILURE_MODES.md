# Accepted failure modes

A failure mode belongs on this list only if it is: (1) genuinely
recurring at a known cadence, (2) has an automated watcher that catches
and recovers from it, and (3) recovering fully (eliminating it outright)
would require accepting a worse trade-off elsewhere (cost, latency,
complexity) that the operator has knowingly declined. If any of those
three isn't true, the item does NOT belong here — it's a live bug, and
`.claude/rules/00-critical.md`'s NO DEFERRING rule applies: fix it, don't
list it. An entry that stops meeting all three (e.g. its watcher breaks,
or its cadence jumps) graduates back to "live bug" and comes off this
list until re-fixed and re-verified.

Last reviewed: 2026-09-25.

## 1. `qwen38-chat` engine wedge — ~1/day, auto-restarted

**Symptom:** container reports `healthy`, GPU pegged near 100%,
`generation_tokens_total` flat, `num_requests_running > 0` — requests
queued but nothing generating. A known class on WSL2+Blackwell (GDN/
HyperQwen-#107-style engine wedge), not a crash.

**Watcher:** `vllm_wedge_monitor.py` detects via the `/metrics` scrape;
`vllm-autoheal` (willfarrell-based) restarts the container once
Docker's healthcheck reports `unhealthy` after the wedge is detected.
`start_period: 3600s` on the healthcheck protects against a
false-unhealthy during normal cold-start/high-load.

**Threshold:** roughly 1 wedge/day is the accepted cadence. A jump to
multiple wedges/day, or a wedge the monitor fails to detect/recover
within its polling interval, is a regression — investigate immediately,
don't wait for a pattern to "confirm" it.

**Trade-off declined:** switching to a different engine/quant to
eliminate the wedge class entirely has been evaluated and rejected
multiple times (see `docs/engine-history.md`) — every alternative tested
on this card either fails to boot, crashes harder (NVFP4/FlashInfer), or
delivers materially worse throughput. The wedge + auto-restart pair is
the accepted trade against a card-specific vLLM/Blackwell interaction
that isn't ours to fix upstream.

## 2. NVIDIA NIM `moonshotai/kimi-k2.6` — 404 "Function ... Not found for account"

**Symptom:** the model is listed in NIM's `/v1/models` catalog on all
three API keys, but every completion call 404s with "Function ...
Not found for account" — an NVIDIA-side deployment gap, not a config or
auth problem on our end (confirmed on `NV_API_KEY` and `NV_API_KEY_2`).

**Watcher:** `bifrost/smoke_all_lanes.py` includes this model in its
probe list specifically to keep confirming it's still down (not silently
fixed upstream); `bifrost-metrics-exporter`'s lane-health tracking surfaces
it as a persistently-erroring model rather than a flapping one.

**Threshold:** any request to `nvidia-nim/moonshotai/kimi-k2.6` (or the
`moonshot/kimi-k2.6` compat shim over it) returning 404 is expected;
ADA falls back to local vLLM / groq automatically. If NVIDIA ever fixes
the deployment, `smoke_all_lanes.py`'s next scheduled run surfaces the
change (200 instead of 404) — that's the trigger to promote the lane back
to active use, not a manual recheck cadence.

**Trade-off declined:** none — this is a pure external dependency (b)
under the "hard blocker" exception in `~/.claude/CLAUDE.md`: NVIDIA's
deployment gap is not something we can fix from our side. We keep probing
it because a silent upstream fix should surface automatically.

## 3. Free-tier 429s on groq / openrouter / nvidia-nim under burst load

**Symptom:** any of these lanes returns 429 (rate limited) when ADA's
request volume bursts above the free-tier ceiling for that provider in a
short window.

**Watcher:** Bifrost's fallback chain routes past the 429'd lane to the
next one in the chain automatically; `bifrost-metrics-exporter` tracks
per-provider error rates so a persistent (not transient) 429 pattern is
visible in Grafana rather than only in raw logs.

**Thresholds (per-provider free-tier ceilings, verify current numbers
against the provider's docs before assuming these are still accurate):**
- NVIDIA NIM: ~40 RPM per key (3 keys pooled).
- Groq: ~30 RPM / 1k RPD / 8k TPM / 200k TPD per model.
- OpenRouter `:free`: 20 RPM, 1,000 RPD (with the $10 lifetime purchase
  applied to this account).

A 429 rate that stays under roughly 5% of a lane's daily requests and
recovers via fallback is accepted. A lane pinned at 429 for its entire
active window (the `zai` park on 2026-09-15 — 763 errors at 11.1s average
vs 178 successes at ~61s average, mostly HTTP 429) is NOT accepted —
that's a lane that needs a per-VK rate cap or to be parked, which is what
happened.

**Trade-off declined:** paying for a higher tier on any of these
providers would eliminate the 429s, but the entire stack is deliberately
all-free (`~/.claude/CLAUDE.md`/`CLAUDE.md` free-tier invariant) — any
nonzero `bifrost_cost_usd_total` is itself a tripwire, not a fix applied
here.

## Explicitly NOT on this list (live bugs as of 2026-09-25, fixed by the
concurrent WS1 session, not this document)

`vllm-embed` Prometheus target down since 2026-09-22, `vllm-embed`
flapping unhealthy from a `--parallel 4` regression, dead-model routing
(`deepseek-v4-flash-0731`, `nemotron-3.5-lightning-30b-a3b` on nvidia-nim),
`aion` at 60% error with no confirmed consumer, and the infractl probe
noise (`virtual key is required` spam) are all live bugs per the
2026-09-25 ground truth pass, not accepted failure modes — see
`docs/DECISIONS.md` for their fix status. Do not add them here as a way
to close them without fixing them; that is exactly the stub/deferral
pattern `~/.claude/CLAUDE.md` bans.

## Tolerated by design, registered 2026-09-30 (Dependability W2)

**Backups share the one physical disk with the data.** The host has a single NVMe. Dumps are
copied out of the Docker VHDX (`scripts/backup_offvolume.py`, C:\ProgramData\ops-backups,
sha256-verified) which survives VHDX/Docker loss but not loss of the disk. Stops being tolerable
the moment an owner-provided external drive or cloud target exists: set `BACKUP_OFFSITE_DIR`.
Watched by: `OffVolumeBackupStale`, ops self-check.

**UI/API ports on 0.0.0.0 (ada 8006/5420/5421, legion 8005/3005, grafana 3050, erpnext 8080).**
Owner LAN/Tailscale access; enumerated in `scripts/ops_exposure_allowlist.json`. Data services
(Qdrant, Redis, exporters, testctl, Postgres) are loopback-only. Stops being tolerable if any
becomes reachable from outside the LAN. Watched by: `UnexpectedExposedPort`, ops self-check.

**GPU free VRAM 0.7-1.0 GiB (card ~96% full by design).** Alert at 400 MiB for 30 min. Watched by
`GpuVramNearlyFull`. Legion's own container DB password is still the historical default; that
Postgres binds 127.0.0.1 only.
