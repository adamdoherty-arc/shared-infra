<!-- CODEGRAPH-FIRST: for any code-symbol question (who-calls / where-defined /
blast-radius / feature-context) the FIRST tool call MUST be
`mcp codegraph call codegraph_status` then codegraph_search/callers/callees/
context/impact. Grep is the fallback only when codegraph is offline or returns
nothing. Every subagent spawned by this skill inherits this rule and must report
`codegraph_tools_used: N` in its final output. -->
---
name: shared-infra-health
description: Review, optimize, modernize, and (safely) auto-heal the shared LLM infrastructure (Bifrost gateway, local vLLM chat+embed, all free cloud lanes) and confirm the dependent projects (ADA / Legion / Zero) do not break. Project-scoped copy of the user-scope skill, wired to infractl as its data source (WS7, 2026-09-25) so it travels with this repo instead of living only on one workstation.
version: 3
owner_project: shared-infra
category: ops
tags: [infra, bifrost, vllm, llm-ops, optimize, modernize, health, gpu, infractl]
budget:
  tokens: 120000
  wallclock_s: 1200
inputs:
  - name: mode
    type: string
    default: full            # full | diagnose | report-only
  - name: apply
    type: boolean
    default: true            # full live auto-apply of reversible fixes (operator choice 2026-06-21)
output:
  schema: shared_infra_health_v1
self_improves: true
related_skills: [docker-health, supervise]
deprecated: false
---

# shared-infra-health (project-scoped)

<!-- CODEGRAPH-FIRST -->
**CODEGRAPH-FIRST (binding).** Before any code-symbol search or Read+grep
chain in this skill, call `mcp codegraph call codegraph_status` first, then
`codegraph_search` / `codegraph_callers` / `codegraph_callees` /
`codegraph_context` / `codegraph_impact` / `codegraph_node` /
`codegraph_explore`. Grep is the fallback only when codegraph returns
nothing or the index is offline; Glob is for filename patterns. Every
spawned subagent must inherit this rule and report `codegraph_tools_used:
N` in its output. See `.claude/rules/00-critical.md`.
<!-- /CODEGRAPH-FIRST -->

The runnable, on-demand counterpart to Legion's autonomous **LLM
Operations** cycle. This project-scoped copy (v3) differs from the
user-scope original in one load-bearing way: **its data source is
`infractl`, not ad-hoc curl/nvidia-smi one-liners** — `infractl` already
runs the deterministic probes (`infractl/probes/`) and keeps a ledger of
every action taken against the shared stack, so duplicating probe logic
here would create exactly the two-writers-diverge problem
`.claude/rules/50-bifrost.md` warns about for config. Use this skill when
you want to drive a review now, go deeper than the deterministic pass, or
validate the estate by hand after a change.

**Single owner.** You + the operator own the whole estate — shared-infra
(Bifrost, vLLM, FreeLLMAPI, observability, infractl), ADA, Legion, Zero. A
broken shared container or mistuned model is yours to fix this run,
crossing into whatever repo/compose file it lives in. See
`.claude/rules/00-critical.md` "ONE OWNER".

**Free-tier invariant.** The stack is 100% free-tier. Any recommendation
introducing paid spend is flagged, never auto-applied. Zero is
intentionally OFF — confirm it stays off; do not start it.

**Restart discipline.** Any restart this skill performs on `shared-
bifrost`, `qwen38-chat`, or `vllm-embed` MUST go through `bash scripts/
bifrost_restart.sh` (or `INFRA_RESTART_OK=1` for a deliberate manual
override) — `.claude/hooks/bifrost_restart_gate.py` blocks a bare
`docker restart` on these containers regardless of who or what issues it.
See `.claude/rules/30-docker.md`.

## Ground truth pointers (don't hardcode numbers here — they drift)

- Live topology + VK table: `.claude/memory/topics/bifrost-architecture.md`
- Engine tuning + canary-gate bar: `.claude/memory/topics/engine-tuning.md`
- Config dual-state mechanics: `.claude/memory/topics/config-sync.md`
- Accepted failure modes (wedge ~1/day, NIM 404s, free-tier 429s):
  `docs/ACCEPTED_FAILURE_MODES.md`
- Architecture decision log: `docs/DECISIONS.md`

## Phases

Run them in order. NEVER tell the operator to run a command you can run
yourself — execute it and paste the output. Verify, don't assert.

### 1. Learn (infractl is the data source)
```bash
docker exec shared-infra-control infractl status
docker exec shared-infra-control infractl health      # runs all probes live
docker ps --format '{{.Names}}\t{{.Status}}'           # cross-check restarts/unhealthy
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,power.draw --format=csv,noheader
```
If `infractl` isn't reachable (container down, `INFRACTL_TOKEN` unset),
that IS a finding — fix it before proceeding, don't fall back to
duplicating its probes by hand.

### 2. Diagnose
```bash
docker exec shared-infra-control infractl config lint
docker exec shared-infra-control infractl probes run
python bifrost/smoke_all_lanes.py                       # every cloud lane 200?
```
Wedge check (the chronic WSL2+Blackwell failure mode — `healthy`
container, 0 tok/s, GPU pegged): `curl -s http://localhost:18801/metrics |
grep -E 'vllm:(num_requests_running|generation_tokens_total|
gpu_cache_usage_perc)'`. Flat `generation_tokens_total` with
`num_requests_running>0` + GPU pegged = WEDGE — expected at roughly
1/day (`docs/ACCEPTED_FAILURE_MODES.md`), `vllm-wedge-monitor` should
already be recovering it; if it isn't, that's the actual finding.

Dependent apps: ADA `curl -s http://localhost:8003/api/health`, Legion
stack-facts `bifrost_status==reachable`, `curl -s
http://localhost:9187/metrics | grep pg_stat_activity_count` for native
Postgres connection pressure.

### 3. Optimize + 4. Modernize (prescriptive)
Read infractl's ranked findings (`infractl health` / `infractl probes
run` output) as the optimizer/modernizer signal. Go deeper where
warranted — re-test a parked capability, check for a new engine build
against the canary/bench gate in `.claude/rules/55-engines.md` before
recommending any swap.

### 5. Apply (live, with rails) — only if `apply=true`
For each ranked action, classify:
- **Reversible + low blast-radius** (a wedged/unreliable lane restart, a
  compose image-tag bump, re-running `sync_vk_allowlists.py`): apply via
  `infractl` action (ledger + snapshot) where the surface has moved under
  infractl already; otherwise snapshot manually (`cp bifrost/config.json
  bifrost/config.json.bak.<ts>`) before editing. Any Bifrost restart goes
  through `scripts/bifrost_restart.sh` — never a bare `docker restart`.
- **Irreversible / destructive** (re-embedding, dropping vector
  collections, deleting volumes): NEVER auto-apply. File a Legion task
  (`.claude/rules/10-legion.md`) + note in `docs/DECISIONS.md`.
- **Needs a secret / interactive login** (a fresh cloud key): surface the
  specific blocker loudly; do not invent a key. See `~/.claude/CLAUDE.md`
  "THERE IS ONE OWNER" for the narrow legitimate-stop list.

### 6. Verify (blast-radius gate — runs after EVERY applied change)
```bash
python bifrost/smoke_all_lanes.py
docker exec shared-infra-control infractl health
python -m pytest bifrost/tests infractl/tests -q -m "not live"
```
ADA `/health` 200 + a real call; Legion bifrost reachable + a routed call;
Zero confirmed still OFF. Any dependent-app regression -> restore the
snapshot, re-sync, restart via `scripts/bifrost_restart.sh`, and
downgrade that action to a filed Legion task rather than re-attempting
blind.

### 7. Report
Write `.claude/state/health/latest.json` (mode, findings, applied vs
rolled-back vs filed, GPU/lane numbers) so hostcron's daily
`shared-infra-health` job (see `.claude/rules/70-infractl-hostcron.md` for
the job-block proposal — `schedule.json` is owned by another workstream)
has something deterministic to check without re-running the full skill.
Summarize for the operator: health score, top ranked actions, what was
auto-applied vs filed vs rolled back, and the single highest-leverage
next move.

## Self-improvement

Append a dated entry to `.claude/state/health/runs/<date>.md`: what was
found, what was applied/rolled back, and the next-run lead. Read the last
3 before starting so leads don't go stale.
