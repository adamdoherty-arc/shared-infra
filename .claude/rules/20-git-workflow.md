# 20 — Git workflow (always-loaded)

## Trunk-based, master only

All work commits directly to `master`. No `sprint/*`, `feature/*`, or
`fix/*` branches for routine work — this repo has one long-lived branch,
same convention as ADA (`c:/code/ADA/.claude/rules/20-git-workflow.md`).
The exception is a short-lived agent worktree (`isolation: "worktree"`)
that self-cleans.

## config.json / disabled-providers.json: one writer, auto-committed

`bifrost/config.json` and `bifrost/disabled-providers.json` have ONE
writer, infractl (2026-09-25). ADA's `bifrost_model_sync.py --apply` posts
`bifrost_models_apply`, `bifrost-autoheal` posts `bifrost_provider_park`,
humans use `infractl models apply` / `park` / `unpark`. Agent edits are
blocked by `.claude/hooks/config_write_gate.py` (`INFRA_CONFIG_WRITE_OK=1`
is the deliberate-override escape hatch).

`scripts/config_autocommit.py` (hostcron, every 15 min) commits those two
files plus `bifrost/config.snapshot.redacted.json` (infractl refreshes it
after each verified change) when they differ from HEAD, with
`git commit -- <paths>` (nothing else is swept in) and a `config:` message
built from the infractl ledger rows since the last config commit. It skips
while a human has any of them staged, while a write is < 120 s old, or on
invalid JSON, and it runs the pre-commit gate normally (a gate failure
leaves the files for the next run). A commit titled "config: live routing
change written outside infractl" means a write bypassed the ladder: read
the diff and record why in `docs/DECISIONS.md`.

**Never commit `bifrost/config.db`** (SQLite mirror, binary, machine
state — see `30-docker.md` "provider state lives in TWO places") or `.env`
(secrets). Both are gitignored; verify `git status` doesn't show them
before any commit.

## `.bak` file policy

`bifrost/*.bak.*` and `config.db.bak.*` are the documented rollback path
for provider changes (see `bifrost/README.md`). Policy: **keep the newest
3 per family** (a "family" = same base filename, e.g. all
`config.json.bak.*`), delete older ones. They are gitignored, so this is
disk hygiene, not a commit concern — but do it when you see a family with
more than 3, don't leave it for later (NO DEFERRING).

## Untracked paths that should be gitignored, not committed

`scripts/hostcron/logs/`, `*.bak-proof`, stray `nssm-*.log` beyond the
current + previous rotation, root `__pycache__/` — if `git status` shows
any of these, add them to `.gitignore` and clean the disk copies in the
same pass rather than committing them or leaving them dangling.

## Commit message trailer

See `10-legion.md` for the `Legion-Task:` trailer convention (warned, not
blocked, by `.githooks/commit-msg`).
