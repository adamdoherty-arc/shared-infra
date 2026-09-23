# hostcron

Single owner for the host-side recurring jobs on this box. Replaces 14 Windows
scheduled tasks that each rendered a console window on every fire (worst case
every 20 minutes, because they were registered `LogonType=Interactive`).

A Windows *service* runs in session 0 and structurally cannot render a window.
That is why this is a service, not a tidier set of scheduled tasks.

## Files

| File | Purpose |
|---|---|
| `hostcron.py` | The daemon. Stdlib only - runs on the bare host interpreter under NSSM. |
| `schedule.json` | The job table. Single source of truth. Re-read every minute, so edits apply without a restart. |
| `install-hostcron.ps1` | Elevated installer / `-Revert`. |
| `docs/native-review-replacement.md` | Plan for the retired claude.exe review jobs. |
| `state/runs.jsonl` | Run ledger: one row per execution with status, rc, duration. |
| `state/heartbeat.json` | Written every tick. The ada-master watchdog reads this. |
| `logs/<job>.log` | Per-job stdout+stderr, appended. |

## Install

```powershell
# elevated
.\install-hostcron.ps1 -SkipTasks   # service only; run alongside the tasks for one cycle
.\install-hostcron.ps1              # service + disable the tasks it replaces
.\install-hostcron.ps1 -Revert      # undo everything
```

The installer refuses to disable any task until the service is Running *and* has
written a heartbeat.

## Guarantees

- **Nothing runs unbounded.** Every job carries `timeout_s`; an overrun has its
  whole process tree killed via `taskkill /T` and is recorded as `timeout`.
- **No stacking.** A job still running when its next tick is due is skipped and
  logged as `skipped_overlap`.
- **No windows.** Subprocesses spawn with `CREATE_NO_WINDOW`.
- **A bad job never kills the loop.** Launch and scheduling errors are caught
  per-job.
- **Failures page.** Non-ok status posts to the ADA ops Discord channel unless
  the job sets `notify_on_failure: false`.

## Adding a job

Append to `jobs` in `schedule.json`:

```json
{
  "name": "my-job",
  "cmd": ["cmd.exe", "/c", "C:\path\to\thing.cmd"],
  "cwd": "C:\code\ADA",
  "schedule": {"minute": 0, "hour": 3},
  "timeout_s": 1800
}
```

`schedule` fields are `minute` / `hour` / `dow` / `dom`; `null` or absent means
any, an int means exact, a list means any-of. `dow` is 0=Sunday. Times are
host-local. `timeout_s` is required in practice - omit it and you get 1800s.

Bump `-MinHostcronJobs` in the ada-master watchdog when the job count changes,
or it will page about a short schedule.

## Not handled here

`ZeroInfra-DockerGuiReclaim` stays a Task Scheduler logon task. It hands the
Docker Desktop GUI from session 0 back to the interactive desktop, which a
session-0 service cannot do by definition. It fires once per logon, not on an
interval.
