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
 "artifact_path":"C:/code/shared-infra/testplatform/artifacts/ada/2026-09-29/<uuid>"}
```
Response: `201 {"run_id": 123}`.

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
| `GET /cases/hashes?project_id=` | `{node_id: body_hash}`. A test whose last non-skipped status is failed or error maps to `""`, so it never matches a real hash and the runner always resends it. That is how a fix that leaves the test body unchanged is still reported as fixed. |
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
- The runner answers `202 {"accepted":true,"run_id":123}`, or `409 {"attached_run_id":...}` when a run for that project is already in flight.
- A fire that fails is recorded on the schedule (`last_error`) and appears in Legion's scheduler catch-up ledger.

## Runner (testctl)
- **Profiles:** read from `<repo>/.testplatform.yml`. The repo roots are registered in `C:\code\shared-infra\testplatform\projects.yml`.
- **Host service:** `testctl serve` listens on 127.0.0.1:8790 and 0.0.0.0 for the Docker bridge. It keeps one run per project (a lock), and a second request attaches to the run already in flight.
- **Artifacts:** stored under `testplatform/artifacts/<project>/<date>/<uuid>/` (report.json, output.log). Retention is 14 days.
- **CLI:**
  - `testctl run <project>[:<target>] [--wait|--no-wait] [--detail]`. `--wait` is the default and prints `text` verbatim. The exit code is 0 only on pass.
  - `testctl failure <id>`, `testctl run-failures <run_id>`, `testctl history <project> <node_id>`, `testctl flaky <project>`, `testctl status [run_id]`, `testctl schedule list`.
- **Budget:** no CLI command prints more than 15 lines without `--detail`, and `--detail` caps at 60.
