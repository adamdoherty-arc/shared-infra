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


_RUN_3208_LOG = (
    "....[gw3] node down: Not properly terminated\nF\nreplacing crashed worker gw3\n"
    "....RR..INTERNALERROR> Traceback (most recent call last):\n"
    "INTERNALERROR>     worker_collection = self.registered_collections[node]\n"
    "INTERNALERROR> KeyError: <WorkerController gw4>\n\n"
    "= 1 failed, 3 passed in 9.0s =\n"
)


def _crash_record(node: str, worker: str = "gw3") -> dict:
    return {"nodeid": node, "outcome": "failed",
            "???": {"duration": 0, "outcome": "failed",
                    "longrepr": f"[{worker}] linux -- Python 3.13\nworker '{worker}' crashed while running '{node}'"}}


def _truncated_run(project, tmp_path, rc: int, tier=None, collected: int = 10, deselected: int = 0):
    tests = [_t("tests/a.py::t1"), _t("tests/a.py::t2"), _t("tests/a.py::t3"), _crash_record("tests/b.py::t_big")]
    summary = {"collected": collected, "total": len(tests)}
    if deselected:
        summary["deselected"] = deselected
    (tmp_path / "report.json").write_text(json.dumps({"summary": summary, "tests": tests}), encoding="utf-8")
    log = tmp_path / "out.log"
    log.write_text(_RUN_3208_LOG, encoding="utf-8")
    tier = tier or {"timeout_s": 60, "min_executed": 1}
    return runner.interpret_pytest(project, tier, None, "schedule", rc, tmp_path / "report.json", log, 1, 60, 0)


def test_sabotage_a_run_truncated_by_a_crashed_worker_is_an_error_that_says_so(tmp_path):
    """ADA nightly full run 3208: gw3 OOM-killed, KeyError on its replacement, pytest exit 3 at 93%. The old
    verdict read FAILED here because exit 3 counted as an error only when no case had failed."""
    exe = _truncated_run(_project(tmp_path), tmp_path, 3)
    assert exe.status == "error"
    assert exe.error_summary.startswith("RUN TRUNCATED: pytest exit 3 (internal error)")
    assert "4 of 10 selected tests reported, 6 never ran" in exe.error_summary
    assert "worker gw3 crashed running tests/b.py::t_big" in exe.error_summary
    assert "KeyError: <WorkerController gw4>" in exe.error_summary


def test_sabotage_the_truncation_outranks_the_floor_breach_it_caused(tmp_path):
    project = _project(tmp_path)
    (project.root / "floors.json").write_text(json.dumps(
        {"floors": {"paper_trading": {"classname_prefixes": ["test_never_reached"], "min_executed": 1}}}),
        encoding="utf-8")
    tier = {"timeout_s": 60, "min_executed": 1, "floors_file": "floors.json"}
    exe = _truncated_run(project, tmp_path, 3, tier=tier)
    assert exe.status == "error"
    assert exe.error_summary.startswith("RUN TRUNCATED")
    assert "consequence: service floor breach: paper_trading" in exe.error_summary


def test_sabotage_a_crashed_worker_with_tests_missing_is_truncation_even_without_exit_3(tmp_path):
    exe = _truncated_run(_project(tmp_path), tmp_path, 1)
    assert exe.status == "error" and "6 never ran" in exe.error_summary


def test_control_a_crashed_worker_whose_replacement_finished_the_run_stays_a_failure(tmp_path):
    exe = _truncated_run(_project(tmp_path), tmp_path, 1, collected=4)
    assert exe.status == "failed"
    assert exe.error_summary is None


def test_control_deselected_items_are_not_tests_that_never_ran(tmp_path):
    """An in-process run counts its deselected items into summary.collected (pytest-json-report adds them back)."""
    exe = _truncated_run(_project(tmp_path), tmp_path, 1, collected=10, deselected=6)
    assert exe.status == "failed"
    assert exe.error_summary is None


def test_verdict_totals_equal_the_cases_the_report_lists(tmp_path):
    tests = [_t("tests/a.py::t1"), _t("tests/a.py::t2", "failed"), _t("tests/a.py::t3", "skipped"),
             _t("tests/a.py::t4")]
    exe = _interpret(_project(tmp_path), tmp_path, 1, tests)
    totals = runner.parsers.totals_of(exe.cases)
    assert (totals["total"], totals["passed"], totals["failed"], totals["skipped"]) == (4, 2, 1, 1)
