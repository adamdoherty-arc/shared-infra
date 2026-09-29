"""`changed --paths` frontend routing and the zero-selection refusal.

Origin: testctl runs #227 and #248 (2026-09-29) printed `PASSED ada:changed ... 0 tests` and exited 0 for a
frontend-only `--paths` set, because selection understood only `.py` files.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import profile, runner  # noqa: E402
from tplib.lock import ProjectLock  # noqa: E402

PROFILE = """
framework: pytest
default_target: fast
path_tier: path
vitest_path_tier: vitest
runtime: {kind: exec, container: c}
vitest: {container: fe, workdir: /app, repo_subdir: frontend, container_root: /app}
tiers:
  fast: {paths: [tests], timeout_s: 60}
  changed: {mode: changed, paths: [tests], timeout_s: 60, testmon_datafile: /tmp/x/.testmondata, test_globs: ["tests/**/test_*.py"]}
  path: {timeout_s: 30, min_executed: 1}
  vitest: {framework: vitest, timeout_s: 60}
"""


def _project(tmp_path: Path, text: str = PROFILE) -> profile.Project:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".testplatform.yml").write_text(text, encoding="utf-8")
    reg = tmp_path / "projects.yml"
    reg.write_text(f"projects:\n  demo:\n    root: {repo.as_posix()}\n    legion_project_id: 9\n", encoding="utf-8")
    return profile.load_project("demo", reg)


def _case(node: str, status: str = "passed") -> dict:
    return {"node_id": node, "file": node.split("::")[0], "status": status, "duration_ms": 1, "attempts": 1,
            "body_hash": "h", "feature_slug": None, "requirement_ids": []}


def _spy(monkeypatch, vitest_cases=None, pytest_cases=None):
    calls: dict[str, list] = {"vitest": [], "pytest": []}

    def fake_vitest(project, tier, argv, art, tag):
        calls["vitest"].append(argv)
        cases = list(vitest_cases or [])
        return runner.Execution("passed", cases, None, 0, 0)

    def fake_pytest(project, tier, paths, trigger, art, legion, target, changed_paths=None):
        calls["pytest"].append(changed_paths)
        return runner.Execution("passed", list(pytest_cases or []), None, 0, 0,
                                reason=None if pytest_cases else "no test names or imports any of them")

    monkeypatch.setattr(runner, "_vitest_call", fake_vitest)
    monkeypatch.setattr(runner, "run_pytest", fake_pytest)
    return calls


def test_split_frontend_paths_maps_repo_paths_and_leaves_python_alone():
    fe, rest = runner.split_frontend_paths(
        ["frontend/src/a/X.test.tsx", r"frontend\src\B.ts", "backend/x.py", "scripts/tool.js", "README.md"], "frontend")
    assert fe == ["frontend/src/a/X.test.tsx", "frontend/src/B.ts"]
    assert rest == ["backend/x.py", "scripts/tool.js", "README.md"]


def test_sabotage_frontend_only_paths_no_longer_yield_a_zero_test_pass(tmp_path, monkeypatch):
    """Would have PASSED with 0 tests before the fix; now vitest runs for the frontend files."""
    project = _project(tmp_path)
    calls = _spy(monkeypatch, vitest_cases=[_case("src/lib/guided.test.ts::routes")])
    exe = runner.run_changed_paths(project, project.tier("changed"), "claude", tmp_path, None, "changed",
                                   ["frontend/src/lib/guided.test.ts", "frontend/src/pages/AdvisorHome.tsx"])
    assert calls["pytest"] == [], "a frontend-only set must not reach pytest selection"
    assert ["run", "src/lib/guided.test.ts"] in calls["vitest"]
    assert ["related", "src/pages/AdvisorHome.tsx", "--run", "--passWithNoTests"] in calls["vitest"]
    assert exe.status == "passed" and len(exe.cases) == 1


def test_control_python_paths_still_select_through_pytest_only(tmp_path, monkeypatch):
    project = _project(tmp_path)
    calls = _spy(monkeypatch, pytest_cases=[_case("tests/test_deep_links.py::t")])
    exe = runner.run_changed_paths(project, project.tier("changed"), "claude", tmp_path, None, "changed",
                                   ["backend/services/deep_links.py"])
    assert calls["vitest"] == [] and calls["pytest"] == [["backend/services/deep_links.py"]]
    assert len(exe.cases) == 1


def test_control_import_graph_selection_is_unchanged_for_python(tmp_path):
    from tplib import selection
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "services").mkdir()
    (tmp_path / "backend" / "services" / "deep_links.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "test_uses.py").write_text(
        "from backend.services.deep_links import x\n", encoding="utf-8")
    ids, info = selection.select_for_paths(tmp_path, {"kind": "run", "container": "none"}, "/nope",
                                           ["backend/services/deep_links.py"], ["backend/tests/**/test_*.py"])
    assert ids == ["backend/tests/test_uses.py"] and info["source"] == "import-graph"


def test_mixed_paths_run_both_and_merge_the_verdict(tmp_path, monkeypatch):
    project = _project(tmp_path)
    calls = _spy(monkeypatch, vitest_cases=[_case("src/a.test.ts::x")], pytest_cases=[_case("tests/test_b.py::y")])
    exe = runner.run_changed_paths(project, project.tier("changed"), "claude", tmp_path, None, "changed",
                                   ["frontend/src/a.test.ts", "backend/b.py"])
    assert calls["pytest"] == [["backend/b.py"]] and calls["vitest"]
    assert {c["node_id"] for c in exe.cases} == {"src/a.test.ts::x", "tests/test_b.py::y"}


def test_merge_takes_the_worst_status():
    merged = runner.merge_executions([runner.Execution("passed", [_case("a::1")], None, 0, 0),
                                      runner.Execution("failed", [_case("b::1", "failed")], None, 0, 1)])
    assert merged.status == "failed" and len(merged.cases) == 2


def _run(project, tmp_path, monkeypatch, exe, target, paths, legion):
    monkeypatch.setattr(runner, "execute_framework", lambda *a, **k: exe)
    lock = ProjectLock("demo", tmp_path / "locks")
    assert lock.acquire()[0]
    try:
        return runner.start_and_run(project, target, "claude", legion, lock, artifacts_root=tmp_path / "art",
                                    changed_paths=paths)
    finally:
        lock.release()


class _Legion:
    def create_run(self, body):
        return 5

    def post_results(self, run_id, payload):
        return {"text": f"{payload['status'].upper()} from legion"}

    def hashes(self, project_id):
        return {}

    def quarantine(self, project_id):
        return []


def test_zero_selection_with_paths_is_refused_never_passed(tmp_path, monkeypatch):
    project = _project(tmp_path)
    exe = runner.Execution("passed", [], None, 0, 0, reason="no test names or imports any of them")
    out = _run(project, tmp_path, monkeypatch, exe, "changed", ["docs/a.md", "docs/b.md"], _Legion())
    assert out.status == "error" and out.exit_code == 2
    assert out.text == "NO TESTS SELECTED for 2 paths (no test names or imports any of them)"
    assert "PASSED" not in out.text


def test_control_a_run_that_executed_tests_still_passes(tmp_path, monkeypatch):
    project = _project(tmp_path)
    exe = runner.Execution("passed", [_case("tests/test_a.py::t")], None, 0, 0)
    out = _run(project, tmp_path, monkeypatch, exe, "changed", ["backend/a.py"], _Legion())
    assert out.status == "passed" and out.exit_code == 0


def test_all_skipped_counts_as_zero_executed(tmp_path, monkeypatch):
    project = _project(tmp_path)
    exe = runner.Execution("passed", [_case("tests/test_a.py::t", "skipped")], None, 0, 0)
    out = _run(project, tmp_path, monkeypatch, exe, "changed", ["backend/a.py"], _Legion())
    assert out.exit_code == 2 and out.text.startswith("NO TESTS SELECTED for 1 paths")


def test_whole_tier_that_runs_zero_tests_is_refused(tmp_path, monkeypatch):
    project = _project(tmp_path)
    exe = runner.Execution("passed", [], None, 0, 0)
    out = _run(project, tmp_path, monkeypatch, exe, "fast", None, _Legion())
    assert out.exit_code == 2 and out.text.startswith("NO TESTS EXECUTED by tier")


def test_control_testmon_changed_without_paths_may_legitimately_select_nothing(tmp_path, monkeypatch):
    project = _project(tmp_path)
    exe = runner.Execution("passed", [], None, 0, 0)
    out = _run(project, tmp_path, monkeypatch, exe, "changed", None, _Legion())
    assert out.exit_code == 0


def test_control_non_test_frameworks_are_not_subject_to_the_refusal():
    tier = {"framework": "commands"}
    exe = runner.Execution("passed", [], None, 0, 0)
    totals = {"total": 0, "skipped": 0}
    assert runner.zero_test_refusal("commands", tier, None, exe, totals) is None
    assert runner.zero_test_refusal("pytest", {}, None, exe, totals) is not None


def test_frontend_paths_without_a_vitest_tier_are_refused_not_silently_passed(tmp_path, monkeypatch):
    text = PROFILE.replace("vitest_path_tier: vitest\n", "")
    project = _project(tmp_path, text)
    calls = _spy(monkeypatch)
    exe = runner.run_changed_paths(project, project.tier("changed"), "claude", tmp_path, None, "changed",
                                   ["frontend/src/A.tsx"])
    assert calls["vitest"] == [] and calls["pytest"] == [["frontend/src/A.tsx"]]
    out = _run(project, tmp_path, monkeypatch, exe, "changed", ["frontend/src/A.tsx"], _Legion())
    assert out.exit_code == 2 and out.text.startswith("NO TESTS SELECTED")
