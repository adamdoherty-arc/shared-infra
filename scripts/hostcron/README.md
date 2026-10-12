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
| `console_break.py` | Helper hostcron runs to send CTRL_BREAK to a job's console (first rung of the graceful stop). |
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
- **Graceful stop ladder (opt-in, `grace_s`).** A job with `grace_s > 0` that overruns
  `timeout_s` is not killed at once: hostcron runs `console_break.py <pid>`, which frees its own console,
  attaches to the job's, ignores the event itself and sends CTRL_BREAK to the whole console; the job gets
  `grace_s` seconds to clean up and exit, then the same `taskkill /T` tree kill runs. The ledger detail says
  which rung ended it: `graceful stop: exited rc=N after Ns`, `grace Ns expired; hard kill`, or
  `break not delivered (helper rc=N); hard kill`. `.cmd`/`.bat` jobs are refused a grace
  (`grace refused: batch/cmd job continues after a break`) because cmd carries on after a break, so a
  graceful job must be a direct python/exe child. The worst case a job can occupy its slot is
  `timeout_s + grace_s`. A new `hostcron.py` is loaded only when the `ADA-HostCron` service restarts.
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
  "cmd": ["cmd.exe", "/c", "C:\\path\\to\\thing.cmd"],
  "cwd": "C:\\code\\ADA",
  "schedule": {"minute": 0, "hour": 3},
  "timeout_s": 1800
}
```

Optional job fields:

| Field | Meaning |
|---|---|
| `grace_s` | Seconds between the console break and the hard kill (see the graceful stop ladder). Default 0 = hard kill at `timeout_s`. |
| `artifact` | `{"path", "run_id_key" (default `hostcron_run_id`), "reject_status": [...]}`. An exit 0 is ledgered failed unless the JSON file at `path` carries THIS run's `HOSTCRON_RUN_ID` under `run_id_key` and a status not in `reject_status`. |
| `runner_env` | Default true: the job gets `HOSTCRON_JOB`, `HOSTCRON_RUN_ID`, `HOSTCRON_PID`, `HOSTCRON_STARTED_EPOCH`, `HOSTCRON_DEADLINE_EPOCH`, `HOSTCRON_TIMEOUT_S` and `HOSTCRON_GRACE_S` in its environment. `false` leaves the environment untouched. |
| `ok_exit_codes` | Exit codes counted as `ok` (default `[0]`). |
| `enabled` | `false` skips the job. |

`schedule` fields are `minute` / `hour` / `dow` / `dom`; `null` or absent means
any, an int means exact, a list means any-of. `dow` is 0=Sunday. Times are
host-local. `timeout_s` is required in practice - omit it and you get 1800s.

Bump `-MinHostcronJobs` in the ada-master watchdog when the job count changes,
or it will page about a short schedule.

## DR restore drill jobs

Two jobs run the weekly restore drill (`C:\code\ADA\scripts\dr_restore_and_diff.py --target <t>`), one per
target, each a python direct child so the graceful stop reaches it:

| Job | When | `timeout_s` / `grace_s` | Artifact |
|---|---|---|---|
| `legion-dr-restore-drill-weekly` | Sunday 15:00 | 5400 / 1200 | `.claude/state/dr-drill/latest-legion.json` |
| `ada-dr-restore-drill-weekly` | Sunday 17:00 | 13500 / 1200 | `.claude/state/dr-drill/latest.json` |

Windows are checked by `scripts/tests/test_dr_schedule_budget.py` (no overlap with heavy jobs, worst case
`timeout_s + grace_s` ends before the next heavy job). The old `ADA DR Restore Drill - Weekly` task is
unregistered by `install-hostcron.ps1`; `scripts/elevated-dr-finish.ps1` is the one-shot elevated finish.

## Not handled here

`ZeroInfra-DockerGuiReclaim` stays a Task Scheduler logon task. It hands the
Docker Desktop GUI from session 0 back to the interactive desktop, which a
session-0 service cannot do by definition. It fires once per logon, not on an
interval.
