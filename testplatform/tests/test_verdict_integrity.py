from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_lanes import _project  # noqa: E402

from tplib import runner  # noqa: E402


def _report(tmp_path: Path, tests: list[dict]) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"tests": tests}), encoding="utf-8")
    return path


def _t(node: str, outcome: str = "passed") -> dict:
    return {"nodeid": node, "outcome": outcome, "call": {"duration": 0.01, "outcome": outcome}}


def _interpret(project, tmp_path, rc, tests, tier=None, paths=None):
    log = tmp_path / "out.log"
    log.write_text("log tail\n", encoding="utf-8")
    tier = tier or {"timeout_s": 60, "min_executed": 1}
    return runner.interpret_pytest(project, tier, paths, "claude", rc, _report(tmp_path, tests), log, 1, 60, 0)


def test_sabotage_an_xdist_internal_error_with_passing_cases_is_an_error_not_a_pass(tmp_path):
    exe = _interpret(_project(tmp_path), tmp_path, 3, [_t("tests/a.py::t1"), _t("tests/a.py::t2")])
    assert exe.status == "error" and "pytest exit 3" in exe.error_summary


def test_sabotage_an_interrupted_collection_with_passing_cases_is_an_error(tmp_path):
    exe = _interpret(_project(tmp_path), tmp_path, 2, [_t("tests/a.py::t1")])
    assert exe.status == "error"


def test_a_crashed_xdist_worker_is_a_failure_the_verdict_counts(tmp_path):
    crashed = _t("tests/a.py::t_crash", "failed")
    exe = _interpret(_project(tmp_path), tmp_path, 1, [_t("tests/a.py::t1"), crashed])
    assert exe.status == "failed"
    assert [c["status"] for c in exe.cases].count("failed") == 1


def test_control_a_clean_run_passes(tmp_path):
    exe = _interpret(_project(tmp_path), tmp_path, 0, [_t("tests/a.py::t1")])
    assert exe.status == "passed"


def test_sabotage_zero_collected_tests_is_never_a_pass(tmp_path):
    exe = _interpret(_project(tmp_path), tmp_path, 5, [])
    assert exe.status == "error"
    refusal = runner.zero_test_refusal("pytest", {"timeout_s": 60}, None, runner.Execution("passed", []),
                                       {"total": 0, "skipped": 0})
    assert refusal and "NO TESTS EXECUTED" in refusal


def test_sabotage_all_skipped_is_zero_executed(tmp_path):
    exe = _interpret(_project(tmp_path), tmp_path, 0, [_t("tests/a.py::t1", "skipped")])
    assert exe.status == "error" and "at least 1" in exe.error_summary


def test_verdict_totals_equal_the_cases_the_report_lists(tmp_path):
    tests = [_t("tests/a.py::t1"), _t("tests/a.py::t2", "failed"), _t("tests/a.py::t3", "skipped"),
             _t("tests/a.py::t4")]
    exe = _interpret(_project(tmp_path), tmp_path, 1, tests)
    totals = runner.parsers.totals_of(exe.cases)
    assert (totals["total"], totals["passed"], totals["failed"], totals["skipped"]) == (4, 2, 1, 1)
