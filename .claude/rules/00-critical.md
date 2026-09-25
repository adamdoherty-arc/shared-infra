# 00 — Critical (always-loaded)

This repo has no local override of the binding rules — they live at user
scope (`~/.claude/CLAUDE.md`) and repo scope (`CLAUDE.md`, root of this
repo). Read both before touching anything here. This file is a pointer +
the one addition specific to shared-infra: **a finding is not closed until
it has a gate, a ratchet, or a test** (see below).

## The five rules that govern every session in this repo

1. **ONE OWNER.** You + the operator jointly own the entire system —
   shared-infra AND every consumer (ADA, Legion, Zero, A-finance). A
   crash-looping shared container, a mistuned model, a broken hostcron job:
   all of it is yours to fix in this turn, regardless of which repo it
   physically lives in. `~/.claude/CLAUDE.md` "THERE IS ONE OWNER".
2. **NO DEFERRING.** Every issue found during the work is part of the work.
   `follow-up` / `out of scope for today` / `documented for later` are
   banned phrases in this repo — see `CLAUDE.md` root for the full list.
3. **NEVER SHIP STUBS.** A heuristic/placeholder shipped in place of the
   defined production path, labeled to look like a design choice, is a
   deferral in disguise. Ship the real implementation or delete the
   surface. `~/.claude/CLAUDE.md` "NEVER SHIP STUBS".
4. **CODEGRAPH-FIRST.** Code-symbol questions (who calls X, where is X
   defined, blast radius of changing Y) go through `mcp codegraph call ...`
   first, always, when the index is live. Grep is the fallback for
   free-text/logs/config or a codegraph miss. `~/.claude/CLAUDE.md`
   "CODEGRAPH-FIRST — TOP-LEVEL RULE".
5. **EXECUTE DIRECTLY.** Run the command and paste the result. Do not tell
   the operator to run something you can run yourself.

## The rule specific to this repo (WS7, 2026-09-25)

**A finding with no hook, no ratchet, and no test is a wish, not a fix.**
This repo had zero `.claude/` infrastructure before WS7 — no settings, no
rules, no gate, no restart-intent enforcement — and its operating knowledge
lived only in 1,700+ lines of compose-file comments nobody re-read under
pressure. Every rule below that matters is backed by one of:

- a **PreToolUse hook** (`.claude/hooks/`) that mechanically blocks the
  bad action (`bifrost_restart_gate.py`, `agent_model_gate.py`),
- a **ratchet baseline** (`.audit-baselines/*.json`) that `scripts/gate.py`
  fails if the count grows (ruff errors, stub phrasings),
- or a **test** (`bifrost/tests/`, `infractl/tests/`,
  `.claude/hooks/tests/`) that runs in `scripts/gate.py`.

If you find yourself writing a rule that is prose only, stop and add the
hook/ratchet/test first, or don't write the rule — it will rot exactly
like the compose-comment history it replaces.

## Path-scoped rules in this repo

- `10-legion.md` — Legion task linkage, commit trailers.
- `20-git-workflow.md` — trunk-based master, `.bak` policy, config
  auto-commit expectation.
- `30-docker.md` — compose ownership, the binding Bifrost restart
  sequence, profiles table.
- `50-bifrost.md` (`paths: ["bifrost/**", "docker-compose.bifrost.yml",
  "bifrost-metrics-exporter/**"]`) — provider state, VK sync, tiering.
- `55-engines.md` (`paths: ["docker-compose.vllm.yml",
  "vllm_wedge_monitor.py"]`) — VRAM arithmetic, canary/bench gate, wedge
  class, embed tuning.
- `60-observability.md` — scrape jobs, alert ownership.
- `70-infractl-hostcron.md` — single-writer control plane, hostcron
  schedule rules.
