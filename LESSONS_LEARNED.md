# Lessons learned (append-only incident log)

Each entry: symptom / root cause / fix / guard. Newest last. Never edit an
old entry to "correct" it — append a new one that supersedes it and say
so, same append-only discipline as `docs/DECISIONS.md`. Seeded 2026-09-25
(WS7) from `docs/DECISIONS.md`, `docs/engine-refresh-2026-09-21.md`, and
compose-file comment history; add new entries here going forward instead
of burying incident knowledge in a compose comment nobody re-reads under
pressure.

## 2026-08-04 — All 10 shared containers died together on a host restart

- **Symptom:** a host/Docker-daemon restart took down all 10 shared
  containers simultaneously.
- **Root cause:** several services (at minimum the then-current chat
  engine) had no `restart:` directive in their compose block, so Docker
  defaulted to a policy that does not survive a daemon restart.
- **Fix:** added explicit `restart: unless-stopped` (or `always`) to every
  long-running service.
- **Guard:** `.claude/rules/30-docker.md` "resource limits are required"
  section now calls out `restart:` as a required field alongside resource
  limits — a new service without one is a finding to fix immediately, not
  a style nit.

## 2026-06-11 — Alias names silently never worked (Bifrost key selection order)

- **Symptom:** requests to a documented alias (e.g. `nemotron-lightning`)
  failed with "no keys found that support model: `<alias>`" even though
  the alias was defined in `config.json`.
- **Root cause:** Bifrost v2 selects a key via
  `key.Models.IsAllowed(<requested model>)` BEFORE resolving
  `key.Aliases` — a key whose `models` list lacks the alias name is never
  selected regardless of how the alias is defined.
- **Fix (2026-09-15):** every key that defines `aliases` now carries those
  alias names in its own `models` list too; `sync_provider_models()`
  writes `models_json` as `models ∪ alias names` so this can't drift back.
- **Guard:** `.claude/rules/50-bifrost.md` "aliases must appear in the
  key's models list" — check this in the same edit whenever adding an
  alias.

## 2026-09-05 — 25 stale model ids accumulate silently across two providers

- **Symptom:** 14 `nvidia-nim` and 11 `openrouter` `:free` model names in
  `config.json` all 404'd upstream despite having been valid when added.
- **Root cause:** free-tier providers retire/rename model ids on their own
  schedule with no notification; nothing in the stack re-verified a
  previously-working id stayed working.
- **Fix:** live 1-token probes through Bifrost found and removed all 25
  dead names, replaced with verified-alive alternatives.
- **Guard:** the WS4 plan item ("eval harness... runs a fixed 30-prompt
  suite... weekly via hostcron") extends this into a recurring check
  instead of a manual sweep — not yet landed as of 2026-09-25; until it
  is, a model-not-answering report is a signal to re-run this sweep, not
  to assume the id is still good because it was three months ago.

## 2026-09-15 — Discord alerts from sidecars silently 403'd since deployment

- **Symptom:** `bifrost-logs-pruner` and `bifrost-autoheal` had been
  "sending" Discord alerts (logs.db size warnings, provider-parked
  notices) since they were written, and not one had ever reached a human.
- **Root cause:** both sidecars POSTed with the default
  `Python-urllib/3.x` User-Agent, which Cloudflare's bot fingerprinting
  blocked with HTTP 403 / `error code: 1010`. The webhook URL itself was
  valid the entire time — nothing about the alert LOGIC was broken, only
  the transport.
- **Fix:** both sidecars now send an explicit descriptive User-Agent
  (`shared-infra-<sidecar>/1.1 (+https://github.com/...)`); verified live
  with test posts.
- **Guard:** any new stdlib-only sidecar copies the explicit User-Agent
  header; `infractl/core/discord.py` sets it centrally so this can't
  regress per-sidecar. **General lesson:** "the code path that sends an
  alert ran with no exception" is not evidence the alert was received —
  verify delivery, not just non-crash, when standing up any new notifier.

## 2026-09-15 — Tempo and cadvisor silently exited for two weeks, no alert fired

- **Symptom:** two observability containers were down for ~2 weeks with
  zero alerting.
- **Root cause:** no meta-alert existed for "an observability container
  itself is down" — the alerting stack could not alert on its own health.
- **Fix:** revived both; gave Tempo a 48-hour trace-consumer watch before
  deciding whether to keep it (no consumer was configured to export
  traces at the time).
- **Guard:** `.claude/rules/60-observability.md` flags "an alert firing
  for multiple days unattended... is itself a finding" — the same logic
  extends to a scrape target or exporter that's been silently down; check
  `up{job=...}` for every job in `.claude/rules/60-observability.md`'s
  table periodically, don't wait for a downstream symptom.

## 2026-09-15 — Two writers on `config.json`/`config.db`, discovered mid-incident

- **Symptom:** while chasing "who is hammering dead lanes", four separate
  probers turned out to be active against Bifrost, and `config.json` had
  been edited by more than the human doing the investigating.
- **Root cause:** ADA's `bifrost_model_sync.py` (daily hostcron) and
  `bifrost-autoheal` (auth-failure park) both write `config.json`/
  `disabled-providers.json` independently of any human session.
- **Fix:** documented explicitly (this repo's `CLAUDE.md`, now also
  `.claude/memory/topics/config-sync.md`); Legion's `VLLM_API_KEY` (found
  stale, pointed at a revoked VK) was fixed and both consuming containers
  force-recreated (a bare `docker restart` does not re-read `.env`).
- **Guard:** `.claude/rules/20-git-workflow.md` "config.json /
  disabled-providers.json are auto-commit surfaces" — treat these two
  files as expected-dirty, review the diff before committing, don't
  assume drift is a mistake to `git checkout --` away.

## 2026-09-15 — `zai` parked: valid key, wrong capacity class

- **Symptom:** `zai/glm-4.5-flash` served ADA's volume with 763 errors at
  11.1s average vs 178 successes at ~61s average over 24h — mostly HTTP
  429 rate-limiting from ZAI's side, not an auth or config problem (manual
  1-token probes succeeded 3/3 at 5-8s, proving the key was valid).
- **Root cause:** the lane's free-tier rate ceiling was far below ADA's
  request rate; nothing was gating ADA's volume down to what the lane
  could actually carry.
- **Fix:** moved `zai` from `config.json` to `disabled-providers.json`
  (a full park, not a silent degrade); documented the re-enable
  requirement (a per-VK rate cap so ADA can't 429 it for every other
  consumer, plus a passing weekly confirm-probe).
- **Guard:** WS2's planned per-VK `rate_limit` (RPM/TPM) config is exactly
  the fix that would have prevented needing to park this lane at all —
  see the workstream plan for the not-yet-landed `network_config` +
  `rate_limit` work. Until it lands, a new free-tier lane addition should
  get a conservative VK rate cap from day one rather than discovering the
  ceiling via a production 429 storm.

## 2026-07-13 — NVIDIA NIM lists a model it cannot actually serve

- **Symptom:** `moonshotai/kimi-k2.6` appears in NIM's `/v1/models`
  catalog on all three API keys, but every completion 404s "Function ...
  Not found for account".
- **Root cause:** an upstream NVIDIA deployment gap between "listed in
  catalog" and "actually deployed" — not fixable from our side, confirmed
  across multiple keys.
- **Fix:** none possible on our end; the `moonshot/kimi-k2.6` compat shim
  over this model was parked, ADA's ladder falls back to local vLLM/groq.
- **Guard:** promoted to `docs/ACCEPTED_FAILURE_MODES.md` item 2 — kept in
  `smoke_all_lanes.py`'s probe list specifically so an upstream fix
  surfaces automatically on the next scheduled run, rather than requiring
  a human to remember to recheck a dead lane periodically.

## 2026-09-21/22 — Two GPU tenants on one card starve each other invisibly

- **Symptom:** `qwen38-chat` throughput measured 2.4 tok/s at 30 running
  requests while `vllm-embed` (also on GPU) was busy — a co-tenant
  restart alone recovered it to 631 tok/s, with no config change to chat
  itself.
- **Root cause:** two vLLM processes time-slicing one RTX 5090 under load
  contend for the same SM/memory-bandwidth resources in a way that
  degrades the higher-value tenant (chat, 92% of tokens) far more than
  the lower-value one (embed) benefits from staying on GPU.
- **Fix:** moved `vllm-embed` to CPU (llama.cpp, `--parallel 1 -b 4096
  -ub 4096`), eliminating the co-tenancy entirely.
- **Guard:** `.claude/rules/55-engines.md` VRAM arithmetic section and
  the canary/bench gate — any future GPU co-tenant (e.g. a reranker
  service) must be benched for its effect on chat throughput specifically,
  not just its own standalone performance, before being adopted.

## 2026-09-22 — `--parallel 4` on embed CPU service looked like a scaling win, was a regression

- **Symptom:** after the embed-to-CPU move, `--parallel 4` was tried to
  increase throughput; instead p50 latency measured up to 4.4s under
  load (vs 49ms at `--parallel 1`).
- **Root cause:** each parallel slot in llama.cpp gets its own KV
  allocation (forced equal for embeddings), so 4 slots means 4-way CPU
  core contention with no batching benefit for this workload shape — more
  parallelism became pure overhead, not scaling.
- **Fix:** reverted to `--parallel 1 -b 4096 -ub 4096`.
- **Guard:** `.claude/memory/topics/engine-tuning.md` records the measured
  numbers explicitly so "just raise parallelism" isn't re-attempted
  without a fresh benchmark; `.claude/rules/55-engines.md` states the rule.

## 2026-09-25 — This repo had no `.claude/` operating infrastructure at all (WS7)

- **Symptom:** every operating rule for this stack (restart sequencing,
  provider dual-state sync, VRAM arithmetic, canary gates) lived only in
  compose-file comments and `docs/DECISIONS.md` prose — no hook enforced
  any of it, no ratchet caught regressions, no gate ran before a commit.
- **Root cause:** the repo grew organically as an ops/config repo rather
  than an application repo, so it never got the `.claude/` tooling pass
  ADA, Legion, and Zero each got early.
- **Fix:** this WS7 pass — `.claude/settings.json`, hooks
  (`agent_model_gate.py`, `session_checkpoint.py`,
  `bifrost_restart_gate.py`), path-scoped rules, memory, a project-scoped
  `shared-infra-health` skill, `scripts/gate.py` with ruff/stub/test/
  compose-config stages and ratchet baselines, `.githooks/` wired via
  `core.hooksPath`.
- **Guard:** `.claude/rules/00-critical.md`'s repo-specific addition — "a
  finding with no hook, no ratchet, and no test is a wish" — exists
  specifically so this doesn't happen again piecemeal; any new operating
  rule added to this repo should come with one of the three from day one.
