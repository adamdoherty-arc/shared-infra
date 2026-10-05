# testplatform (testctl)

Runner and CLI half of the test platform. Legion stores, analyses, schedules and files bugs
(`/api/test-platform/*`); `testctl` runs the tests on this host and prints a verdict of at most
15 lines. Raw pytest/vitest/playwright output goes to `artifacts/<project>/<date>/<uuid>/`
(`output.log`, `report.json`, `payload.json`, `verdict.txt`, `selection.json`) and is pruned after 14 days.
The wire contract is `CONTRACT.md`.

## Commands (`testctl` is on PATH next to `mcp`)

```
testctl run ada:changed --paths <files...>   the tests that depend on those files; <=15 line verdict; exit 0 pass, 1 fail, 2 error/timeout
                                              python files select pytest tests; frontend files (.ts/.tsx/.js/.jsx under the
                                              profile's vitest repo_subdir) run vitest: test files by path, source files via
                                              `vitest related --run`; a mixed set runs both. Zero tests selected/executed is
                                              never PASSED: `NO TESTS SELECTED for <n> paths (<reason>)`, exit 2
testctl run ada:changed          testmon-selected (needs testmon data; refuses with a one-line hint otherwise, never a git-diff guess)
testctl run ada:fast --quiet     prints nothing on pass, the verdict on failure; the exit code gates (hooks)
testctl run ada                  default tier (fast)
testctl run ada:backend/tests/test_x.py[::Class::test]    a file or node id (uses the path tier)
testctl run legion:fast --no-wait                          start and return the run id
testctl run-failures <run_id>    compact failure list
testctl failure <failure_id>     one failure with trace tail
testctl history ada <node_id>    last 20 results + revisions
testctl flaky ada                flake scores and quarantine state
testctl status [run_id]          active runs / a run's verdict
testctl schedule list            Legion schedules
testctl serve [--ensure]         host runner service on :8790 (POST /run, GET /runs/active)
testctl replay                   ingest results saved while Legion was unreachable
```

Every command prints through one helper (`tplib/printer.py`): 15 lines by default, 60 with `--detail`.
`TESTCTL_TRIGGER` (or `--trigger`) sets `claude|schedule|hook|manual`; the default is `claude`.
`claude` and `hook` runs `--deselect` the quarantine list and disable pytest-randomly; `schedule` runs
pass `--reruns 2` and keep random ordering.

## Profiles

`projects.yml` maps a project name to its repo root and Legion project id. Each repo carries a
`.testplatform.yml` (`ada`: `C:/code/ADA/.testplatform.yml`, `legion`: `C:/code/legion/.testplatform.yml`)
with `runtime` (how to reach a test container), `pytest` defaults and named `tiers`
(framework, marker, paths, workers, `timeout_s`, `min_executed`). A target that is not a tier name is a
path or node id and runs under `path_tier`. A tier may carry `floors_file` (per-service executed-test floors keyed on
test module stem; a prefix that matches zero tests, or a shortfall, makes the run an `error`).

A whole pytest tier may carry `serial_marker` (not a `changed` tier): after its xdist workers finish, the same run
executes `-m <serial_marker>` in one process (`-n0`) with the tier's paths and args, on the snapshot the first phase
built, and writes that phase's artifacts under `serial/`. One verdict covers both phases, floors are judged on their
merged cases, the quarantine count is not doubled, `serial_min_executed` (default 0) is the phase's own floor and
`serial_timeout_s` (default the tier's `timeout_s`) its budget. A first phase that timed out or produced nothing ends
the run there. ADA uses it for tests one process grows by gigabytes (`memory_heavy`): the tier's own marker
deselects them, its serial phase runs them, so `ada:bitcoin`, the live-money release preflight, still runs
TestMonteCarloOracle. A heavy run's snapshot is built from the sha the run reports (`pin_snapshot_rev`), not
whatever HEAD is when the snapshot is taken.

ADA tiers: `changed` (two selection paths. `--paths <files>` maps the given source or test files to tests through the
testmon dependency database, falling back to an import-graph scan of the test tree when the database cannot answer;
plain `changed` runs `pytest --testmon`, and with neither paths nor testmon data it refuses with a hint), `testmon`
(builds the testmon data once), `fast`, `live_db`, `full` (also rebuilds the testmon data every night with
`--testmon-noselect`, so `changed` stays current), `path`, `vitest`, `gates` (framework `commands`: each configured
command is one case, exit 0 pass / 1 fail / 2 error; used for the non-test static gates),
`fuzz` (schemathesis, GET only, fixed seed, against `http://ada-backend:8003`; the tier's `checks` names the
schemathesis check, `env` is passed into the container (ADA loads its checks through `SCHEMATHESIS_HOOKS`) and
`exclude_paths` lists the operation templates kept out of the run, as a list or as reason -> list groups, and
`exclude_paths_file` reads the same shape from a file relative to the repo root (ADA: `scripts/fuzz/exclude_paths.yml`,
kept honest by `scripts/fuzz/fuzz_tier_gate.py`); a named file that cannot be read makes the run an `error`. The report is
schemathesis' NDJSON event stream (`report.ndjson.gz` in the artifact dir), flushed one event per line, so a run killed
at `timeout_s` still reports every operation that finished, as a `timeout` run with those cases and the count of
scenarios that were still running),
`e2e` (playwright smoke over a page list; a `pages` entry is a path string or `{path, admin_mode?, assert?, assert_testid?, assert_absent_testid?}`,
turned into `runner.py smoke <url> [--admin-mode] [--assert=TEXT] [--assert-testid=ID] [--assert-absent-testid=ID]` (`assert_absent_testid` is the
negative twin of `assert_testid`: no visible element with that `data-testid` once the page has settled; the same page without `admin_mode`
is the read-only smoke), and `flows: [{name, admin_mode?}]` runs
`runner.py flow <name> <base_url> [--admin-mode]`. Case names say which page or flow and which mode failed
(`/clients?tab=rules [admin]`, `flow:admin_banner_probe [admin]`; two string pages that differ only by query are named by their full
path; a repeat gets ` #2`), an unknown key, a non-boolean `admin_mode`, an empty assertion or one testid both required and forbidden is an `error` and nothing runs, and an item the tier
deadline cut off is a failed case, never silently dropped; every runner process is told its kill timeout (the tier's `timeout_s` divided by
its items, at least 30 s, recorded as `budget_s` in `report.json`) in `TESTCTL_ITEM_TIMEOUT_S`, because testctl discards the output of a
process it had to kill and a runner that knows the budget can print its verdict first). Legion runs in a throwaway container from its own image.

`customer-ops` (`C:/code/customer-ops/.testplatform.yml`, Legion project 29) shows the other runtime shapes: pytest in a throwaway
`run` container from the app's own image with the repo mounted read-only and `runtime.tmpfs` masking live data dirs
(a mask is skipped when the path does not exist on the host); `vitest: {kind: host, repo_subdir: ...}` runs `npx vitest` on the host
when there is no frontend container (node ids stay repo-relative); `gates` and `e2e` are `commands` tiers whose first token is
resolved through PATH (so `npm` finds `npm.cmd` on Windows) and each command may carry its own `env:` map. Plain `changed` needs testmon, which only `exec` runtimes record;
`customer-ops:changed --paths <files>` uses the import-graph scan.

### Committed-tree snapshots (`runtime.snapshot` on `run` runtimes, host vitest, host gates)

An official run must test what is committed, not another session's half-edit (customer-ops failed twice that way on
2026-10-01). `exec` runtimes (ADA) stream `git archive` into their container. For everything that reads the repo from
the host, `runtime.snapshot: {rev: HEAD, include: [.], exclude: [...], location: console/web}` makes every WHOLE tier
(pytest in a `run` container, host `vitest`, `commands` gates and e2e) run against `git archive <rev>` extracted to a
fresh per-run directory (`tplib/snapshot.py`): the container mounts it where the profile mounts `.`, host vitest and host
gates use it as cwd. `location` places it at `<repo>/<location>/.tp-snapshot/<run-id>/`, so Node finds the real
`<location>/node_modules` by walking up the tree; with no `location` it lives under the run's artifact dir. Nothing is
linked into the real tree (no symlink, no junction), untracked runtime files (`node_modules`, `.venv`, `.env`) are never
copied, and tmpfs masks still apply (a mask whose path is not in the committed tree is skipped). A command gate can name
the live root with `{repo}` in its argv or env (for the untracked `.venv` python) and sees `TP_LIVE_ROOT` and
`TP_SNAPSHOT_ROOT`.

A path target and `changed --paths` stay on the LIVE tree, so you can run the test file you are editing, untracked or
not; a tier opts out with `snapshot: false`. If the snapshot cannot be built the run is an `error`, never a silent live
run. Cleanup (`safe_rmtree`) deletes only a marker-bearing dir strictly inside the snapshot root, never follows a link,
and a crashed run's leftovers (older than 3h) are swept by the next run. Add `<location>/.tp-snapshot/` to the repo's
`.gitignore` and exclude it from that tree's lint/test globs.

## Service

`testctl serve` schedules runs per project in two lanes. The **heavy** lane (any whole tier: `fast`, `full`,
`live_db`, `testmon`, `changed` without paths, `vitest`, `fuzz`, `e2e`, `gates`) is one run at a time. The
**light** lane (a path target such as `ada:backend/tests/test_x.py`, a `.tsx` path, or `changed --paths`) runs up
to `light_concurrency` requests at once (`ada`: 3, set in `projects.yml`; a profile's `lanes: {light_concurrency: N}`
overrides it; default 1) and never waits behind a heavy run. A second request for the same spec attaches to the
run in flight (HTTP 409 with `attached_run_id`); a different request queues first come, first served within its
own lane. Light runs always pass `-p no:testmon` so only heavy runs ever write `.testmondata`. `testctl status`
lists active runs with their lane. It is supervised by the hostcron job `testctl-serve-ensure`
(every minute, no-op when the port answers). `install-testctl-service.ps1` is the NSSM alternative
(elevated). Logs: `logs/serve.log`.

## Tests

`python -m pytest testplatform/tests -q --tb=line` (host, no containers).
