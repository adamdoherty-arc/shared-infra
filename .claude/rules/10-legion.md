# 10 — Legion linkage (always-loaded)

shared-infra work is tracked as Legion tasks/sprints in the same Legion
instance ADA and Zero use (e.g. Task 15016 "shared-infra observability",
Task 15018 "Bifrost config hygiene" — check `curl -s
"http://localhost:8005/api/sprints/?source_system=shared-infra"` for the
live list before filing a duplicate).

## Commit trailer

Commits doing shared-infra work SHOULD carry a trailer naming the Legion
task/sprint they belong to:

```
Legion-Task: 15016
```

or, if the work isn't tracked yet, file one first (`curl -X POST
http://localhost:8005/api/sprints/ ...`, project matching shared-infra) —
filing a Legion task is bookkeeping, not the work itself; see
`00-critical.md` NO DEFERRING: the fix still happens in this turn, the
trailer just says which line item it closes.

The `.githooks/commit-msg` hook **warns** (does not block) when the
trailer is missing — shared-infra is infra config as much as code, and
plenty of legitimate commits (a `.bak` prune, a hostcron `.gitignore`
entry) have no sprint of their own. A pattern of NO commits over a week
carrying a trailer while Legion tasks pile up unclosed is the smell to
watch for, not any single untrailered commit.

## Closing the loop

When a WS/task closes, leave a note on the Legion task (or `docs/
DECISIONS.md` for anything with an architectural consequence) — not a new
`*_MIGRATION.md`/`*_PUNCHLIST.md` file. `docs/DECISIONS.md` is the MADR
architecture log; `LESSONS_LEARNED.md` is the incident log. Both are
append-only.
