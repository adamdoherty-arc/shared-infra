from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tplib import parsers, runner  # noqa: E402
from tplib.profile import Project  # noqa: E402


def _line(kind: str, body: dict[str, Any]) -> str:
    return json.dumps({kind: body})


def _started(sid: str, phase: str = "fuzzing") -> str:
    return _line("ScenarioStarted", {"id": sid, "timestamp": 1.0, "suite_id": "s", "phase": phase})


def _finished(sid: str, label: str, status: str, phase: str = "fuzzing", elapsed: float = 0.5,
              checks: dict[str, Any] | None = None, interactions: dict[str, Any] | None = None) -> str:
    recorder: dict[str, Any] = {"label": label}
    if checks:
        recorder["checks"] = checks
    if interactions:
        recorder["interactions"] = interactions
    return _line("ScenarioFinished", {"id": sid, "timestamp": 2.0, "suite_id": "s", "phase": phase,
                                      "status": status, "elapsed_time": elapsed, "is_final": False,
                                      "recorder": recorder})


def _server_error(case_id: str, label: str, uri: str, code: int = 500, body: bytes = b'{"error":"boom"}') -> str:
    checks = {case_id: [{"name": "no_undeclared_server_error", "status": "failure",
                         "failure_info": {"failure": {"type": "ServerError", "operation": label,
                                                      "title": "Server error", "message": "", "case_id": case_id,
                                                      "severity": "critical"}}}]}
    interactions = {case_id: {"request": {"method": "GET", "uri": uri, "headers": {}},
                              "response": {"status_code": code, "headers": {},
                                           "content": {"$base64": base64.b64encode(body).decode()},
                                           "elapsed": 0.01}}}
    return _finished(f"run-{case_id}", label, "failure", checks=checks, interactions=interactions)


def _engine_finished(reason: str = "completed") -> str:
    return _line("EngineFinished", {"id": "e", "timestamp": 3.0, "running_time": 9.0, "stop_reason": reason})


def _stream() -> list[str]:
    return [
        _line("Initialize", {"command": "st run", "seed": 1}),
        _started("a0", "examples"), _finished("a0", "GET /api/a", "skip", phase="examples"),
        _started("a1"), _finished("a1", "GET /api/a", "success"),
        _started("run-c1"), _server_error("c1", "GET /api/b/{id}", "http://h:8003/api/b/x%20y?limit=-1"),
        _started("c2"), _finished("c2", "GET /api/c", "error"),
        _line("NonFatalError", {"id": "n1", "timestamp": 2.5, "value": {
            "type": "ReadTimeout", "message": "HTTPConnectionPool(host='h', port=8003): Read timed out. "
                                             "(read timeout=60.0)"},
            "phase": "fuzzing", "label": "GET /api/c", "related_to_operation": True}),
        _started("d0", "examples"), _finished("d0", "GET /api/d", "skip", phase="examples"),
        _line("NonFatalError", {"id": "n2", "timestamp": 2.6, "value": {
            "type": "ServerUnavailable", "message": "http://h:8003 stopped accepting connections."},
            "phase": "fuzzing", "label": "Server", "related_to_operation": False}),
        _engine_finished(),
    ]


def test_ndjson_folds_every_phase_into_one_case_per_operation():
    report = parsers.parse_schemathesis_ndjson(_stream())
    by_id = {c["node_id"]: c for c in report["cases"]}
    assert by_id["fuzz::GET /api/a"]["status"] == "passed"
    assert by_id["fuzz::GET /api/d"]["status"] == "skipped"
    failed = by_id["fuzz::GET /api/b/{id}"]
    assert failed["status"] == "failed"
    assert failed["failure"]["type"] == "ServerError"
    assert failed["failure"]["message"] == "Server error: 500 on GET /api/b/{id}"
    assert "Reproduce with: curl -X GET 'http://h:8003/api/b/x%20y?limit=-1'" in failed["failure"]["trace_tail"]
    assert 'Response: {"error":"boom"}' in failed["failure"]["trace_tail"]
    errored = by_id["fuzz::GET /api/c"]
    assert errored["status"] == "error" and errored["failure"]["type"] == "ReadTimeout"
    assert by_id["fuzz::Server"]["status"] == "error"
    assert by_id["fuzz::GET /api/a"]["duration_ms"] == 1000
    assert report["engine_finished"] and report["stop_reason"] == "completed" and report["unfinished"] == 0


def test_one_defect_keeps_one_signature_whatever_values_the_fuzzer_drew():
    night1 = parsers.parse_schemathesis_ndjson([_started("run-x1"), _server_error(
        "x1", "GET /api/b/{id}", "http://h:8003/api/b/%C3%A9F?limit=-12244009689829242568704")])
    night2 = parsers.parse_schemathesis_ndjson([_started("run-q9"), _server_error(
        "q9", "GET /api/b/{id}", "http://h:8003/api/b/0?limit=-1", body=b"other body")])
    one, two = night1["cases"][0]["failure"], night2["cases"][0]["failure"]
    assert one["signature"] == two["signature"]
    assert one["message"] == two["message"]
    assert one["trace_tail"] != two["trace_tail"]


def test_a_different_status_is_a_different_failure():
    a = parsers.parse_schemathesis_ndjson([_server_error("a", "GET /api/b", "http://h/api/b", code=500)])
    b = parsers.parse_schemathesis_ndjson([_server_error("b", "GET /api/b", "http://h/api/b", code=502)])
    assert a["cases"][0]["failure"]["message"] != b["cases"][0]["failure"]["message"]


def test_a_killed_stream_reports_what_finished_and_counts_what_was_running():
    lines = [_started("a1"), _finished("a1", "GET /api/a", "success"),
             _started("b1"), _finished("b1", "GET /api/b", "success"),
             _started("hung"),
             _finished("c1", "GET /api/c", "success")[:40]]
    report = parsers.parse_schemathesis_ndjson(lines)
    assert [c["node_id"] for c in report["cases"]] == ["fuzz::GET /api/a", "fuzz::GET /api/b"]
    assert report["unfinished"] == 1
    assert not report["engine_finished"]


def test_timeout_keeps_the_partial_cases_and_says_what_was_still_running():
    report = parsers.parse_schemathesis_ndjson(_stream()[:5] + [_started("hung")])
    exe = runner.interpret_schemathesis(124, report, "tail", elapsed=3600.4, timeout_s=3600)
    assert exe.status == "timeout"
    assert [c["node_id"] for c in exe.cases] == ["fuzz::GET /api/a"]
    assert "1 operations finished and are reported, 1 scenarios still running at the kill" in exe.error_summary


def test_control_a_timeout_without_a_stream_still_reports_no_cases():
    exe = runner.interpret_schemathesis(124, None, "tail", elapsed=3600.4, timeout_s=3600)
    assert exe.status == "timeout" and exe.cases == []
    assert "left no event stream" in exe.error_summary


def test_complete_runs_pass_fail_and_flag_an_early_stop():
    passed = parsers.parse_schemathesis_ndjson([_started("a1"), _finished("a1", "GET /api/a", "success"),
                                               _engine_finished()])
    assert runner.interpret_schemathesis(0, passed, "", 10.0, 3600).status == "passed"
    assert runner.interpret_schemathesis(1, parsers.parse_schemathesis_ndjson(_stream()), "", 10.0,
                                         3600).status == "failed"
    stopped = parsers.parse_schemathesis_ndjson(_stream()[:-1] + [_engine_finished("server_unavailable")])
    exe = runner.interpret_schemathesis(1, stopped, "", 10.0, 3600)
    assert exe.status == "failed" and "stopped early (server_unavailable)" in exe.error_summary


def test_a_run_that_dies_before_the_engine_finishes_is_an_error_with_its_cases():
    report = parsers.parse_schemathesis_ndjson([_started("a1"), _finished("a1", "GET /api/a", "success")])
    exe = runner.interpret_schemathesis(1, report, "Traceback", elapsed=12.0, timeout_s=3600)
    assert exe.status == "error" and len(exe.cases) == 1
    assert "before the run finished" in exe.error_summary


def test_exclusions_flatten_reason_groups_in_order_without_duplicates():
    tier = {"exclude_paths": {"event_stream": ["/a/stream", "/b/events"], "llm_generating": ["/c", "/a/stream"]}}
    assert runner.fuzz_exclusions(tier) == ["/a/stream", "/b/events", "/c"]
    assert runner.fuzz_exclusions({"exclude_paths": ["/x"]}) == ["/x"]
    assert runner.fuzz_exclusions({}) == []


def test_exclusions_merge_a_reason_grouped_file_from_the_project_root(tmp_path):
    (tmp_path / "fuzz").mkdir()
    (tmp_path / "fuzz" / "x.yml").write_text("event_stream: [/s/stream, /a]\nllm_generating: [/l]\n", encoding="utf-8")
    tier = {"exclude_paths": ["/a"], "exclude_paths_file": "fuzz/x.yml"}
    assert runner.fuzz_exclusions(tier, tmp_path) == ["/a", "/s/stream", "/l"]


def test_a_named_exclusion_file_that_is_missing_or_malformed_is_an_error(tmp_path):
    with pytest.raises(runner.RunError, match="exclude_paths_file"):
        runner.fuzz_exclusions({"exclude_paths_file": "nope.yml"}, tmp_path)
    (tmp_path / "bad.yml").write_text("just a string\n", encoding="utf-8")
    with pytest.raises(runner.RunError, match="list or a reason"):
        runner.fuzz_exclusions({"exclude_paths_file": "bad.yml"}, tmp_path)


def _fake_project(tmp_path):
    return Project(name="p", root=tmp_path, legion_project_id=1,
                   profile={"runtime": {"kind": "exec", "container": "c", "workdir": "/app"}})


def test_run_schemathesis_refuses_to_start_without_its_exclusion_file(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(runner, "_run", lambda cmd, *a, **k: (calls.append(list(cmd)), (0, ""))[1])
    monkeypatch.setattr(runner, "ensure_container", lambda runtime, root: None)
    tier = {"framework": "schemathesis", "schema_url": "http://b:8003/openapi.json", "timeout_s": 1,
            "exclude_paths_file": "scripts/fuzz/missing.yml"}
    exe = runner.run_schemathesis(_fake_project(tmp_path), tier, "schedule", tmp_path)
    assert exe.status == "error" and "missing.yml" in exe.error_summary
    assert not any("schemathesis" in c for c in calls)


def test_run_schemathesis_streams_ndjson_and_reports_a_killed_run(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    stream = "\n".join(_stream()[:5] + [_started("hung")]) + "\n"

    def fake_run(cmd, timeout, out_file=None, cwd=None, env=None):
        calls.append(list(cmd))
        if "schemathesis" in cmd:
            return 124, ""
        return 0, ""

    def fake_pull(container, remote, dest):
        dest.write_text(stream, encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr(runner, "pull_file", fake_pull)
    monkeypatch.setattr(runner, "ensure_container", lambda runtime, root: None)
    (tmp_path / "excl.yml").write_text("llm_generating: [/api/l]\n", encoding="utf-8")
    tier = {"framework": "schemathesis", "schema_url": "http://b:8003/openapi.json", "timeout_s": 1,
            "checks": "no_undeclared_server_error", "env": {"SCHEMATHESIS_HOOKS": "/app/hooks.py"},
            "exclude_paths": {"event_stream": ["/api/s/stream"]}, "exclude_paths_file": "excl.yml",
            "args": ["--workers", 4]}
    exe = runner.run_schemathesis(_fake_project(tmp_path), tier, "schedule", tmp_path)
    assert exe.status == "timeout" and [c["node_id"] for c in exe.cases] == ["fuzz::GET /api/a"]
    cmd = next(c for c in calls if "schemathesis" in c)
    assert cmd[cmd.index("-e") + 1] == "SCHEMATHESIS_HOOKS=/app/hooks.py"
    assert cmd[cmd.index("--report") + 1] == "ndjson"
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--exclude-path"] == ["/api/s/stream", "/api/l"]
    assert cmd[cmd.index("--checks") + 1] == "no_undeclared_server_error"
    assert cmd[-2:] == ["--workers", "4"]
    assert (tmp_path / "report.ndjson.gz").exists() and not (tmp_path / "report.ndjson").exists()
