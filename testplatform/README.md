# testplatform (testctl)

Runner and CLI half of the test platform. Legion stores, analyses, schedules and files bugs
(`/api/test-platform/*`); `testctl` runs the tests on this host and prints a verdict of at most
15 lines. Raw pytest/vitest/playwright output goes to `artifacts/<project>/<date>/<uuid>/`
(`output.log`, `report.json`, `payload.json`, `verdict.txt`, `selection.json`) and is pruned after 14 days.
The wire contract is `CONTRACT.md`.

## Commands (`testctl` is on PATH next to `mcp`)

```
testctl run ada:changed          one call, <=15 line verdict; exit 0 pass, 1 fail, 2 error/timeout
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
path or node id and runs under `path_tier`.

ADA tiers: `changed` (pytest-testmon; when no testmon data exists, git-changed files mapped to test
files), `testmon` (builds the testmon data), `fast`, `live_db`, `full`, `path`, `vitest`,
`fuzz` (schemathesis, GET only, fixed seed, `not_a_server_error`, against `http://ada-backend:8003`),
`e2e` (playwright smoke over a page list). Legion runs in a throwaway container from its own image.

## Service

`testctl serve` keeps one run per project (lock file per project; a second request attaches to the run
in flight, HTTP 409 with `attached_run_id`). It is supervised by the hostcron job `testctl-serve-ensure`
(every minute, no-op when the port answers). `install-testctl-service.ps1` is the NSSM alternative
(elevated). Logs: `logs/serve.log`.

## Tests

`python -m pytest testplatform/tests -q --tb=line` (host, no containers).
