# 20 — Git workflow (always-loaded)

## Trunk-based, master only

All work commits directly to `master`. No `sprint/*`, `feature/*`, or
`fix/*` branches for routine work — this repo has one long-lived branch,
same convention as ADA (`c:/code/ADA/.claude/rules/20-git-workflow.md`).
The exception is a short-lived agent worktree (`isolation: "worktree"`)
that self-cleans.

## config.json / disabled-providers.json are auto-commit surfaces

`bifrost/config.json` and `bifrost/disabled-providers.json` are rewritten
by THREE writers: ADA's `bifrost_model_sync.py` (hostcron, daily),
`bifrost-autoheal` (parks providers with repeated auth failures), and
humans doing manual edits. **This means the working tree on this repo is
expected to show these two files as modified between sessions** — that is
not drift to "clean up", it is live routing state. Read the diff before
committing (a park/unpark you didn't expect is worth a line in
`docs/DECISIONS.md`), then commit it with a `config:` prefix message
describing what changed and why (e.g. "config: absorb zai auto-park +
model-sync additions 2026-09-2x"). Once WS6 (infractl Wave 2) lands,
`infractl` auto-commits these on every write it makes; until then, commit
them by hand at the start of a session so `git status` reflects the
current live routing before you start editing anything else.

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
