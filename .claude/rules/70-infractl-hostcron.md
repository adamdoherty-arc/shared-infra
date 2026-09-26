# 70 — infractl + hostcron (always-loaded)

## infractl — the single-writer control plane

`infractl` (`infractl/`, container `shared-infra-control`, port 8095
localhost-only) is the intended single writer for gateway config changes,
provider park/unpark, and restart authority — replacing ad-hoc host
scripts and manual edits one surface at a time (WS6 in the current plan;
not fully landed as of 2026-09-25, so some of the mechanics below are
still manual). Its CLI (`infractl/cli.py`) is a thin `httpx` client over
the running API, authenticated with `INFRACTL_TOKEN` — **never talk to
`bifrost/config.db` or ledger files directly from a script that bypasses
infractl once a surface has moved under it**, or the single-writer
guarantee is void and you're back to the two-writers-diverge bug class
`50-bifrost.md` describes.

Key commands (see `infractl/cli.py` for the full surface —
`mcp codegraph call codegraph_search --arg query=infractl` beats grepping
the file if the index is warm):
```bash
infractl status                 # GET /api/status
infractl health                 # GET /api/health — runs all probes live
infractl consumers               # GET /api/consumers
infractl probes run              # POST /api/probes/run
infractl actions list            # queued/applied actions (ledger)
```
Run from inside the container: `docker exec shared-infra-control infractl
<command>` when the CLI isn't installed on the host.

## Single writer of the Bifrost config (2026-09-25)

infractl is the only writer of `bifrost/config.json` and
`bifrost/disabled-providers.json`:

| Caller | Action | Route |
|---|---|---|
| ADA `scripts/bifrost_model_sync.py --apply` | `bifrost_models_apply` | `POST /api/bifrost/models/apply` `{changes: {provider: {add, remove}}, reason, requested_by, dry_run}` |
| `bifrost-autoheal` (auth failures) | `bifrost_provider_park` | `POST /api/config/providers/{p}/park` (`requested_by=bifrost-autoheal`) |
| humans | same | `infractl models apply --changes JSON --reason R [--apply]`, `infractl park/unpark` |

Ladder (`core/actions.py`): cooldown/cap -> lock -> drift guard -> pure
plan + validation (provider active; resulting config checked with
`sync_vk_allowlists.operator_violations()`; invalid = 400/422 with no
write and no restart) -> snapshot to `/state/snapshots` -> atomic write ->
binding restart (`bifrost/restart.py`, same steps as
`scripts/bifrost_restart.sh`; autoheal is left running only when it is the
requester) -> verify -> on ANY failure restore config.json +
disabled-providers.json and run the ladder again -> ledger row ->
redacted snapshot refresh. Dry-run returns the structured + unified diff
and writes nothing. `vk_resync` is the same restart (T2); heal rules only
ever dry-run a T2 kind. Git truth: `scripts/config_autocommit.py`
(`20-git-workflow.md`).

## Ledger discipline

Every action infractl takes (park a provider, apply a config change, heal
a restart) writes a ledger row before/after the action — this is what
lets a restart-auditor probe say "this container restarted with no ledger
row" as an alert rather than a shrug. If you add a new infractl action,
it MUST write to the ledger; an action with no ledger entry is the same
un-versioned-enforcement problem `00-critical.md` calls out for rules with
no hook.

## Probes

`infractl/probes/` hold the deterministic, LLM-free health checks (lane
liveness, config lint, VK sanity) that back `infractl health` / `infractl
probes run`. The `shared-infra-health` skill (`.claude/skills/
shared-infra-health/`) uses these as its data source rather than
duplicating probe logic — if you're tempted to write a new bash health
check, check whether it belongs in `infractl/probes/` instead so both the
skill and the CLI get it for free.

## hostcron — the single host service replacing 14 scheduled tasks

`scripts/hostcron/schedule.json` is the source of truth for every
recurring host-side job (migrated off Windows Task Scheduler 2026-09-23).
Rules for editing it:

- **Every job MUST carry `timeout_s`.** Nothing on this box runs
  unbounded — a hung job under LocalSystem with no timeout is exactly the
  kind of silent-failure mode `00-critical.md`'s NO DEFERRING rule exists
  to prevent from going unnoticed.
- Schedule fields (`minute`/`hour`/`dow`/`dom`) are `null` (any), an int
  (exact), or a list (any-of). `dow` is `0=Sunday..6=Saturday`. Times are
  host-local (ET).
- The service re-reads this file each tick — edits apply within a minute,
  no service restart needed.
- **LocalSystem PYTHONPATH gotcha**: a job that ran fine under Windows
  Task Scheduler as the interactive user can fail under hostcron's
  LocalSystem service account with `ModuleNotFoundError`, because
  LocalSystem does not inherit the interactive user's `site-packages`
  (`ada-master-pulse`'s `PYTHONPATH` env override in `schedule.json` is
  the worked example — it points explicitly at
  `C:\Users\hadam\AppData\Roaming\Python\Python314\site-packages` because
  the system-wide `C:\Python314` site-packages isn't writable without
  admin). If a hostcron job you add or edit uses a third-party package,
  either install it into the system Python's site-packages or set
  `env.PYTHONPATH` explicitly in the job block — don't assume "it worked
  when I tested it interactively" transfers to the service account.
- `wsl-memory-reclaim`, `claude-usage-forensics-weekly`, `shared-infra-gate`
  and `freellmapi-operator-disabled` are the shared-infra-owned jobs; every other current job belongs to `ada`
  (owner field is informational, used for filtering/ownership, not
  enforced access control).

`shared-infra-gate` (nightly 02:30, runs `scripts/gate.py`) and
`freellmapi-operator-disabled` (daily 07:45, after `ada-bifrost-model-sync`)
are the shared-infra jobs added 2026-09-25. `config-autocommit` (every 15
min, `scripts/config_autocommit.py`, timeout 600 s, same `PYTHONPATH`
override so the pre-commit gate's ruff resolves under LocalSystem) commits
the Bifrost config files infractl writes.
