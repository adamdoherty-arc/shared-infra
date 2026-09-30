# Test Platform contract (v1, 2026-09-29)

Two halves built in parallel:
- **Legion** (`c:\code\legion`) is the store, analysis, scheduler, bug filer and UI.
- **testctl** (`C:\code\shared-infra\testplatform`) is the host runner and the CLI Claude calls.

Neither side may change this contract without updating this file in the same commit.

Legion projects: Legion=1, ADA=5, Zero=26, A Finance=27, Shared Infra=28.

## Why it exists
Claude never reads raw test output. It calls `testctl` once and gets a verdict of at most 15 lines. The full output lives on disk, and the history lives in Legion.

## Run lifecycle (Legion API, prefix `/api/test-platform`)

### `POST /runs` creates a run in the `running` state
Request body:
```json
{"project_id":5,"target":"changed|fast|full|live_db|e2e|fuzz|<path or node id>",
 "tier":"fast","trigger":"claude|schedule|hook|manual","git_sha":"abc123",
 "framework":"pytest|vitest|playwright|schemathesis","runner_host":"DESKTOP-X",
 "artifact_path":"C:/code/shared-infra/testplatform/artifacts/ada/2026-09-29/<uuid>",
 "schedule_id":7}
```
`schedule_id` is optional and additive (2026-09-29). When present Legion stores it on the run and sets that schedule's `last_run_id`, so a scheduled fire that was queued behind another run still links to the run it eventually starts. Response: `201 {"run_id": 123}`.

### `POST /runs/{run_id}/results` completes the run with per-test rows
The response carries the verdict. Request body:
```json
{"status":"passed|failed|error|timeout|cancelled",
 "completed_at":"ISO8601","duration_s":12.3,
 "totals":{"total":10,"passed":8,"failed":1,"errors":0,"skipped":1,"rerun_passed":0},
 "error_summary":"<=500 chars; set when status=error|timeout (collection error, OOM, container down)",
 "cases":[{"node_id":"backend/tests/test_x.py::TestA::test_b","file":"backend/tests/test_x.py",
           "status":"passed|failed|error|skipped|xfail|xpass|rerun_passed",
           "duration_ms":40,"attempts":1,"body_hash":"sha1 of the test function source (AST segment)",
           "feature_slug":null,"requirement_ids":[],
           "failure":{"type":"AssertionError","message":"<=500 chars",
                      "signature":"normalized: numbers/hex/paths/uuids/timestamps stripped",
                      "trace_tail":"<=40 lines"}}]}
```
Optional additive field `quarantined_deselected` (int): the number of tests the runner deselected. When absent, Legion reports the project's quarantined-case count for `trigger=claude|hook` runs and 0 otherwise.

`POST /runs` also marks any run of the same project still `running` after 6 hours as `error` (the runner died without reporting).
A second `POST /runs/{id}/results` for a completed run answers `409`.

Response: `200`
```json
{"run_id":123,
 "verdict":{"status":"failed","totals":{"...":0},
   "new_failures":[{"failure_id":9,"node_id":"...","one_line":"AssertionError: expected 3 got 2 (test_x.py:41)"}],
   "still_failing":7,"fixed":["node ids"],"flaky":["node ids"],"quarantined_deselected":2,
   "issues_filed":[{"issue_ref":"...","node_id":"..."}]},
 "text":"<=15 line rendered verdict, printed verbatim by testctl"}
```

Delta semantics are computed **per test**, against that test's previous non-skipped result from any run on the same project:
- **New failure:** there is no prior result, or the prior result passed, and the test now fails or errors.
- **Fixed:** the prior result failed and the test now passes.
- **Still failing:** the prior result failed and the test still fails. These are counted only, never listed.

The rendered text lists at most 8 new failures, then `+N more (testctl run-failures <run_id>)`.

### Rows per run and the revision catalog
- **Cases are sparse by default.** For large suites the runner sends every failed, errored or rerun-passed case, plus passed cases only when the run is scheduled or the target is not `changed`. Otherwise it sends `passed` rows capped to cases whose body hash changed since the last ingest (the runner asks for `GET /cases/hashes?project_id=`). This keeps a changed-only Claude run cheap.
- **Revisions:** when a case's `body_hash` differs from `test_cases.current_body_hash`, Legion inserts a `test_case_revisions` row (git_sha, body_hash).

## Reads
| Endpoint | Returns |
|---|---|
| `GET /runs?project_id=&limit=20` | Run list (no cases) |
| `GET /runs/{id}` | Run, totals, verdict text |
| `GET /runs/{id}/failures` | Compact list `[{failure_id,node_id,one_line}]` |
| `GET /failures/{failure_id}` | `{node_id,type,message,trace_tail,first_seen_run,occurrences,issue_ref}` |
| `GET /cases/history?project_id=&node_id=` | Last 20 results plus revisions |
| `GET /cases/hashes?project_id=` | `{node_id: body_hash}`. A test whose last non-skipped status is failed or error maps to `""`, so it never matches a real hash and the runner always resends it. That is how a fix that leaves the test body unchanged is still reported as fixed. Conditional (2026-09-29): the response carries `ETag`; send `If-None-Match` and an unchanged set answers `304` with no body (the runner client does this). Optional `prefix=` narrows to node ids starting with it (never cached). |
| `GET /flaky?project_id=` | `[{node_id,flake_score,runs_30d,flaky_events_30d,quarantined}]` |
| `GET /quarantine?project_id=` | `["node ids"]`; the runner deselects these on `trigger=claude\|hook` runs only |
| `GET/POST/PATCH /schedules` | `{id,project_id,target,tier,cron,timezone,enabled,last_fired_at,last_run_id}` |

## Flakes
A flaky event is either:
- a `rerun_passed` result, or
- a test that both passed and failed on the same `git_sha` within 7 days.

`flake_score = flaky_events_30d / runs_30d`.
- **Quarantine** when `flaky_events_30d >= 3` and `flake_score >= 0.10`.
- **Unquarantine** after 20 consecutive clean scheduled passes.

## Bug filing (Legion, at ingest, `trigger=schedule` only)
- **When:** the same `(node_id, signature)` fails in 2 consecutive scheduled runs of that test, or any scheduled run ends with `status=error`. The error case files a suite-level issue keyed on `error_summary`.
- **How:** idempotent via the feature issues endpoint with `source_ref = "test:<project_id>:<node_id>:<sha1(signature)[:12]>"`.
  - The feature is the `feature_slug` if set, else `product_feature:test-platform`.
  - Use `kind="open_question"`, the node id in the title and `trace_tail` in the body. Also create a project issue if Legion's issue model supports it.
- **Repeats** increment `test_failures.occurrences` and are not refiled.
- **Resolution:** a pass on a later scheduled run marks the issue resolved.
- **Discord:** alert only for newly filed issues.

## Scheduling
- Legion's APScheduler fires each enabled schedule by calling `POST {TESTPLATFORM_RUNNER_URL}/run` with `{"project":"ada","target":"full","trigger":"schedule","schedule_id":7}`.
- The default runner URL is `http://host.docker.internal:8790`.
- The runner answers `202 {"accepted":true,"run_id":123}`, or `409 {"attached_run_id":...}` when the same request (same target and paths) is already in flight. A different request is `202 {"queued":true,"run_id":null}`; the runner persists the queued request under `state/queue/` (re-queued on `testctl serve` restart), starts it when its lane has room, and the run it creates carries the request's `schedule_id`, which is how the schedule row learns the `run_id`.
- A fire that fails is recorded on the schedule (`last_error`) and appears in Legion's scheduler catch-up ledger.

## Runner (testctl)
- **Profiles:** read from `<repo>/.testplatform.yml`. The repo roots are registered in `C:\code\shared-infra\testplatform\projects.yml`.
- **Host service:** `testctl serve` listens on 127.0.0.1:8790 and 0.0.0.0 for the Docker bridge. It schedules two lanes per project:
  - **Heavy lane** (one run at a time): every whole-tier target (`fast`, `full`, `live_db`, `testmon`, `changed` without paths, `vitest`, `fuzz`, `e2e`, `gates`). Lock file `artifacts/.locks/<project>.json`, queue `<project>.queue/`.
  - **Light lane** (bounded concurrency): a request naming files (a path or node-id target, a `.ts/.tsx` path, or `changed --paths`). `light_concurrency` slots (`projects.yml` entry, overridden by the profile's `lanes.light_concurrency`; integer 1..8; default 1; `ada` is 3, sized against the 8 GiB `ada-tests` container where one path run peaks near 0.6 GiB). Slot files `<project>.light<N>.json` are claimed under a short-lived `<project>.light.gate` mutex, queue `<project>.light.queue/`.
  - Lanes do not block each other. Within a lane the queue is first come, first served. A request identical to one in flight (same target and paths, in either lane) attaches to it instead of starting a second run.
  - Every run keeps its own Legion run id, artifact directory, junit/report file (uuid names) and verdict. Light runs disable testmon (`-p no:testmon`); only heavy tiers write `.testmondata`. `testctl status` and `GET /runs/active` report each active run with its `lane`.
- **Artifacts:** stored under `testplatform/artifacts/<project>/<date>/<uuid>/` (report.json, output.log). Retention is 14 days.
- **CLI:**
  - `testctl run <project>[:<target>] [--wait|--no-wait] [--detail] [--paths FILE...] [--quiet]`. `--wait` is the default and prints `text` verbatim. The exit code is 0 only on pass.
  - `--paths` (changed tier only) selects the tests that depend on the given files: testmon dependency data first, an import-graph scan otherwise. The runner service accepts the same list as `"paths"` in `POST /run`.
  - `--paths` also accepts frontend files (`.ts/.tsx/.js/.jsx/.mts/.cts` under the profile's `vitest.repo_subdir`) when the profile names a `vitest_path_tier`: test files (`*.test.*`, `*.spec.*`, `__tests__/`) run by path, other files run through `vitest related <files> --run`, and a mixed set runs pytest and vitest and merges the verdict (worst status wins).
  - **Zero selection is never PASSED.** A run of a pytest or vitest tier that executed no test (skipped tests do not count) is reported `error` (exit 2) with the single-line text `NO TESTS SELECTED for <n> paths (<reason>)` when `--paths` was given, or `NO TESTS EXECUTED by tier (<reason>)` for a whole tier. The one exception is plain `changed` (testmon, no `--paths`), where nothing depending on the edit legitimately selects nothing.
  - `changed` with no `--paths` and no testmon data is refused (exit 2, one line) before any Legion run row exists. It never falls back to a git-diff guess.
  - `--quiet` prints nothing on a pass; failures print the normal verdict. Exit code gates.
  - `testctl failure <id>`, `testctl run-failures <run_id>`, `testctl history <project> <node_id>`, `testctl flaky <project>`, `testctl status [run_id]`, `testctl schedule list`.
- **Budget:** no CLI command prints more than 15 lines without `--detail`, and `--detail` caps at 60.
- **Ingest resilience:** `create_run` and `post_results` retry 3 times (2s then 6s backoff) on a timeout, connection error, 5xx, 408, 425 or 429. A 409 on a retried `post_results` means the first attempt landed, so the client returns the stored verdict from `GET /runs/{id}`. Other 4xx are never retried. A `create_run` the server rejects with 4xx is reported as rejected and is not saved for replay; the target label is cut to 200 characters.
- **Pending results drain by themselves:** a run whose ingest still fails is saved as `pending_ingest.json`. Every `testctl run` drains up to 5 pending items (90s budget) before starting, and `testctl serve` drains every 5 minutes. A drain claims a file by renaming it to `ingesting.<pid>.<thread>.json` (a claim older than 15 minutes is released), stops at the first retryable failure and restores the file, and parks a server-rejected item as `pending_ingest.rejected.json` so it cannot block the queue. `testctl replay` is the manual form of the same drain.
