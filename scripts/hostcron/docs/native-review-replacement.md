# Replacing the claude.exe-driven review jobs with native ADA work

Status: plan. Written 2026-09-23, when the five tasks below were disabled.

## What was retired and why

Five Windows scheduled tasks shelled out to headless Claude Code:

| Task | Command | Cadence |
|---|---|---|
| ADA Master - Daily | `claude.exe -p "/ada-master --ring=daily"` | 06:30 daily |
| ADA Master - Weekly | `claude.exe -p "/ada-master --ring=weekly"` | Sun 12:00 |
| ADA Master - Monthly | `claude.exe -p "/ada-master --ring=monthly"` | 1st 08:00 |
| A-finance Daily Review | `claude.exe -p "/daily-review --project=a-finance"` | 06:45 daily |
| ADA Financial Repos - Weekly | `claude.exe -p "/financial-repos weekly" --dangerously-skip-permissions` | Sun 07:00 |

Three reasons they were disabled rather than rehomed:

1. **They were already failing.** At retirement the ada-master watchdog reported
   the daily ring's last success at **170 hours** old and **1061 consecutive RED
   pulses** (~14 days), with 14 probes RED including `tests_green` and
   `ada_containers`. Quota was being spent without a successful ring.
2. **Cost.** These are the top-tier spend the token-discipline rule targets.
3. **`--dangerously-skip-permissions` on an unattended run.** The Financial
   Repos job ran an agent with permission checks disabled, weekly, against
   `C:\code\ADA`, with nobody watching.

Nothing was deleted. `install-hostcron.ps1 -Revert` re-enables all five.

## The three tiers of the work

`/ada-master` is not one thing. Splitting it is what makes it portable:

| Tier | What it is | Native today? |
|---|---|---|
| **Probe** | 36 LLM-free probes, `probes.py` (4970 lines), writes `probe_cache.json` / `heartbeat.json` | **Yes.** Zero tokens. Only its scheduler changed - it now runs under hostcron as `ada-master-pulse`. |
| **Analysis** | 10-pillar scoring, regrade, narrative | **No, but straightforward.** The Bifrost pattern below already does exactly this shape for two other reports. |
| **Fix-in-turn** | Edits ADA source, commits, sweeps backlog (`fix_ratio >= 0.60` is mandatory in the skill) | **No, and not cheaply.** See the gap. |

## The proven native pattern (tier 2)

`scripts/weekly_narrative_bifrost.py` is the shared engine behind
`prompt_weekly_review_bifrost.py` and `llm_weekly_report_bifrost.py` - both
already run with **no Claude quota at all**:

- **LLM call**: `narrate()` POSTs to `{BIFROST_HOST_URL}/v1/chat/completions`,
  trying lanes in order - `vllm-local/qwen3-chat` (free, local) ->
  `nvidia-nim/deepseek-v4-flash` -> `nvidia-nim/nemotron-3.5-lightning`.
  First non-empty 200 wins.
- **Input**: deterministic Python collectors, not the model. `run_collector()`
  subprocesses a `collect_*.py` and parses stdout JSON, then a `reduce_*`
  function shrinks ~200KB to what fits the local 64K context.
- **Output**: `publish()` POSTs markdown + a summary to an ADA ingest endpoint.
  Dashboard numbers are computed deterministically and never taken from the
  model's prose.
- **Failure**: exit 3 (`EXIT_NARRATIVE_UNAVAILABLE`) when every lane is down.

To add a third report, write a `collect_*.py`, a `reduce_*()`, and a prompt.
That is the whole job.

### Immediate change already in effect

`ada-llm-weekly-report` and `ada-prompt-weekly-review` keep running under
hostcron with `ok_exit_codes: [0]`. Their rc-3 branch used to fall back to
`claude.exe --model haiku`; it now pages Discord instead. Same signal, no quota.

## The gap: tier 3 has no native equivalent

`ada-agent` (`backend/agent_core/`) is a **trading/finance decision agent**, not
a coding agent. Its write-tool surface (`WRITE_TOOL_NAMES` in
`backend/agent_core/tools/write_tools.py`) is domain actions only -
`execute_paper_trade`, `watchlist_add`, `file_legion_sprint`,
`restart_container`, and so on. There is no file-edit tool, no shell tool, no
git tool. `restart_container` doesn't even exec - it drops a JSON file for a
sidecar to pick up.

So `/ada-master`'s "auto-apply in-turn" code editing **cannot** be reproduced
in-container today. Two honest options:

**Option A - detect natively, fix deliberately (recommended).**
The probe tier runs free under hostcron. Add a Bifrost analysis job that reads
`probe_cache.json`, scores the pillars, and calls the **already in-container**
`backend/services/legion_sprint_filing.file_sprint(...)` for anything needing
code. You keep the full detection cadence at zero token cost, and code changes
happen when you run `/ada-master` interactively - with a human in the loop,
which is where an agent with commit rights belongs.

**Option B - build a coding-agent surface inside ada-agent.**
Add file-edit / shell / git write tools to `agent_core`. This is real work and a
real security decision: it means an LLM with commit rights to ADA running
unattended. The thing that was just disabled for running with
`--dangerously-skip-permissions` is the same capability. Not recommended without
a sandbox and a review gate.

## Sequencing

1. **Done** - probe tier under hostcron; rc-3 pages instead of spending quota.
2. **Next** - `collect_pillar_metrics.py` + `ada_master_review_bifrost.py`
   following the `weekly_narrative_bifrost` skeleton; publish to the ADA
   dashboard; `file_sprint()` for anything needing code.
3. **Then** - same treatment for `a-finance` daily review and the financial
   repos weekly: deterministic collector + Bifrost narration + sprint filing.
4. **Decide later** - whether tier 3 ever becomes autonomous (Option B), or
   stays an interactive `/ada-master` invocation (Option A).

Until step 2 lands, the pillar analysis simply does not run. That is deliberate:
a heuristic stand-in labeled `_v1` would be a stub, and the probe tier plus
sprint filing already covers detection.
