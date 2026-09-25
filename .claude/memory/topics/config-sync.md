# config-sync — who writes bifrost config, when, and how

Last verified: 2026-09-25.

## The three writers

1. **ADA's `bifrost_model_sync.py`** — hostcron job `ada-bifrost-model-sync`
   (daily 07:30 ET, `scripts/hostcron/schedule.json`). Discovers new
   models on each provider's catalog and writes additions into
   `bifrost/config.json`. This is the "discovery half"; WS6 plans to move
   the "apply half" (actually writing config.json/restarting) into
   `infractl` while keeping ADA's discovery as the data source.
2. **`bifrost-autoheal`** (sidecar, `docker-compose.bifrost.yml`) — watches
   for repeated auth failures against a provider and auto-parks it:
   moves the block from `config.json` to `disabled-providers.json`,
   deregisters from `config.db`, runs `sync_vk_allowlists.py`, restarts.
   Leaves a `.bak.autoheal-<ts>-<reason>` snapshot before each park.
3. **Humans** — manual provider adds/removes, key rotations, alias fixes.

## Why the working tree is expected-dirty

Because writer 1 runs daily and writer 2 runs continuously, `git status`
on this repo will very often show `bifrost/config.json` and
`bifrost/disabled-providers.json` as modified even when no human touched
anything. See `.claude/rules/20-git-workflow.md` for the commit
convention (`config:` prefix, describe what changed and why, commit at
session start before doing anything else so the diff you're reviewing
is the live routing state, not tangled with your own edits).

## The sync script

`bifrost/sync_vk_allowlists.py` reads `config.json`, derives the set of
active/retired providers from `config_keys` rows already in `config.db`
(not from `config.json` alone), and rewrites each VK's per-provider
allowlist rows to match. It has no `--help` flag — any invocation just
runs the sync. Run it with Bifrost **stopped** when restoring a
previously-parked provider block (a live container has caused SQLite
write conflicts in this exact script before); running it with Bifrost
live during routine model-sync additions has been fine.

## WS6 plan (not yet landed as of 2026-09-25)

Move `bifrost_model_sync.py`'s apply-half into `infractl` (`infractl
bifrost sync-models --apply`), route autoheal park/unpark and manual
edits through `infractl` actions (ledger row + snapshot to
`state/snapshots/` + redacted-snapshot re-render + auto-commit of
`config.json`/`disabled-providers.json`/snapshot with a `config:`
message), so git finally reflects live routing without a human doing the
commit by hand. Until this lands, the manual commit discipline in
`20-git-workflow.md` is the only thing keeping git roughly truthful.
