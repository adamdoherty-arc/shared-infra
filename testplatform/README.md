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

ADA tiers: `changed` (two selection paths. `--paths <files>` maps the given source or test files to tests through the
testmon dependency database, falling back to an import-graph scan of the test tree when the database cannot answer;
plain `changed` runs `pytest --testmon`, and with neither paths nor testmon data it refuses with a hint), `testmon`
(builds the testmon data once), `fast`, `live_db`, `full` (also rebuilds the testmon data every night with
`--testmon-noselect`, so `changed` stays current), `path`, `vitest`, `gates` (framework `commands`: each configured
command is one case, exit 0 pass / 1 fail / 2 error; used for the non-test static gates),
`fuzz` (schemathesis, GET only, fixed seed, `not_a_server_error`, against `http://ada-backend:8003`),
`e2e` (playwright smoke over a page list). Legion runs in a throwaway container from its own image.

`customer-ops` (`C:/code/customer-ops/.testplatform.yml`, Legion project 29) shows the other runtime shapes: pytest in a throwaway
`run` container from the app's own image with the repo mounted read-only and `runtime.tmpfs` masking live data dirs
(a mask is skipped when the path does not exist on the host); `vitest: {kind: host, repo_subdir: ...}` runs `npx vitest` on the host
when there is no frontend container (node ids stay repo-relative); `gates` and `e2e` are `commands` tiers whose first token is
resolved through PATH (so `npm` finds `npm.cmd` on Windows). Plain `changed` needs testmon, which only `exec` runtimes record;
`customer-ops:changed --paths <files>` uses the import-graph scan.

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
