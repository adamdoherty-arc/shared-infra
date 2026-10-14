"""A targeted run never writes the real database by default (ADA review note 14, 2026-10-06).

ADA's `path` tier used to deselect only `slow`, `stress` and `memory_heavy`, so `testctl run ada:<file>` on a file with
live_db tests ran them against the real `adam` database (run 5956 rebuilt cc_outcome_expectancy). The path tier's marker
now starts with `not live_db`; a node id still applies no marker (the explicit route to one test) and
`testctl run ada:live_db --paths <files>` is the explicit route to a whole live_db file.

This gate would pass trivially if it only compared the profile text with the string "not live_db", if it evaluated the
marker only for a test that carries no other marker (`not live_db or slow` contains the text, deselects a bare live_db
test and selects one also marked slow, so the one sabotage case below shows that both weaker checks accept it), or if
the `--paths` route were checked on a profile the shipped one does not match (the controls read the shipped profile
and fail on the old one).
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from _pytest.mark.expression import Expression

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shipped_profiles import shipped_profile_file  # noqa: E402

from tplib import profile, runner  # noqa: E402

OLD_ADA_PATH_MARKER = "not slow and not stress and not memory_heavy"
OTHER_MARKERS = ("slow", "stress", "memory_heavy", "integration")


def _selects(expression: str, markers: set[str]) -> bool:
    return Expression.compile(expression).evaluate(lambda name, /, **_kwargs: name in markers)


def _live_db_variants() -> list[set[str]]:
    return [{"live_db", *extra} for count in range(len(OTHER_MARKERS) + 1)
            for extra in itertools.combinations(OTHER_MARKERS, count)]


def _selects_some_live_db_test(expression: str) -> bool:
    return any(_selects(expression, markers) for markers in _live_db_variants())


def _shipped_ada() -> dict:
    return yaml.safe_load(shipped_profile_file("ada").read_text(encoding="utf-8"))


def test_sabotage_the_old_path_marker_selects_a_live_db_test():
    assert _selects_some_live_db_test(OLD_ADA_PATH_MARKER)


def test_sabotage_a_text_search_or_a_bare_live_db_check_would_bless_an_expression_that_selects_live_db():
    expression = "not live_db or slow"
    assert "not live_db" in expression
    assert not _selects(expression, {"live_db"})
    assert _selects_some_live_db_test(expression)


def test_control_the_shipped_ada_path_tier_deselects_every_live_db_test_and_keeps_ordinary_ones():
    tier = _shipped_ada()["tiers"]["path"]
    assert not _selects_some_live_db_test(tier["marker"])
    assert _selects(tier["marker"], set())
    assert tier["marker_unless_node_id"] is True


def test_control_the_shipped_ada_live_db_tier_selects_only_live_db_and_accepts_paths():
    tier = _shipped_ada()["tiers"]["live_db"]
    assert tier["accepts_paths"] is True
    assert _selects(tier["marker"], {"live_db"}) and not _selects(tier["marker"], set())


def test_a_plain_file_run_carries_the_marker_and_a_node_id_run_is_the_explicit_route():
    path_tier = _shipped_ada()["tiers"]["path"]
    project = SimpleNamespace(profile={"pytest": {"command": ["python", "-m", "pytest"]}})
    whole_file = runner._pytest_args(project, path_tier, ["backend/tests/test_x.py"], "claude", "/r.json", [], [])
    one_test = runner._pytest_args(project, path_tier, ["backend/tests/test_x.py::TestA::test_b"], "claude", "/r.json", [], [])
    marker = whole_file[3:][whole_file[3:].index("-m") + 1]
    assert not _selects_some_live_db_test(marker)
    assert "-m" not in one_test[3:]


def test_a_live_db_paths_run_keeps_the_live_db_marker_over_exactly_the_named_files():
    tier = _shipped_ada()["tiers"]["live_db"]
    project = SimpleNamespace(profile={"pytest": {"command": ["python", "-m", "pytest"]}})
    args = runner._pytest_args(project, tier, ["backend/tests/test_cc_outcome_expectancy.py"], "claude", "/r.json", [], [])
    assert args[3:][args[3:].index("-m") + 1] == "live_db"
    assert "backend/tests/test_cc_outcome_expectancy.py" in args
    assert "backend/tests" not in args


PROFILE = """
project: demo
framework: pytest
default_target: fast
path_tier: path
runtime: {kind: exec, container: c}
tiers:
  fast: {paths: [tests], timeout_s: 60}
  path: {timeout_s: 30, marker: "not live_db", marker_unless_node_id: true}
  live_db: {paths: [tests], timeout_s: 60, marker: live_db, accepts_paths: true}
"""


def _project(tmp_path: Path, text: str = PROFILE) -> profile.Project:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (repo / ".testplatform.yml").write_text(text, encoding="utf-8")
    registry = tmp_path / "projects.yml"
    registry.write_text(f"projects:\n  demo:\n    root: {repo.as_posix()}\n    legion_project_id: 9\n", encoding="utf-8")
    return profile.load_project("demo", registry)


def test_paths_are_refused_on_a_tier_that_does_not_accept_them(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(runner.RunError, match="accepts_paths"):
        runner.preflight(project, "fast", ["tests/test_a.py"])


def test_paths_on_a_tier_that_accepts_them_must_name_an_existing_file(tmp_path):
    project = _project(tmp_path)
    runner.preflight(project, "live_db", ["tests/test_a.py"])
    with pytest.raises(runner.RunError, match="names no existing file"):
        runner.preflight(project, "live_db", ["tests/test_gone.py"])


def test_a_live_db_paths_request_takes_the_heavy_lane_so_two_never_write_the_database_at_once(tmp_path):
    project = _project(tmp_path)
    assert runner.lane_of(project, "live_db", ["tests/test_a.py"]) == runner.HEAVY
    assert runner.lane_of(project, "tests/test_a.py") == runner.LIGHT


def test_a_paths_request_on_an_accepting_tier_runs_pytest_over_those_files_only(tmp_path, monkeypatch):
    project = _project(tmp_path)
    seen: dict = {}

    def fake_run_pytest(project_, tier, paths, trigger, art, legion, target, changed_paths=None, **kw):
        seen.update(paths=paths, changed_paths=changed_paths, tier=tier)
        return runner.Execution("passed", [])

    monkeypatch.setattr(runner, "run_pytest", fake_run_pytest)
    runner._execute_framework(project, project.tier("live_db"), None, "claude", tmp_path, None, "live_db",
                              ["tests/test_a.py"], None)
    assert seen["paths"] == ["tests/test_a.py"] and seen["changed_paths"] is None
    assert seen["tier"]["marker"] == "live_db"


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_accepts_paths_must_be_a_boolean(tmp_path, bad):
    text = PROFILE.replace("accepts_paths: true", f"accepts_paths: {yaml.safe_dump(bad).strip().removesuffix('...').strip()}")
    with pytest.raises(profile.ProfileError, match="accepts_paths"):
        _project(tmp_path, text)


def test_accepts_paths_is_refused_on_a_changed_tier(tmp_path):
    text = PROFILE + "  changer: {mode: changed, paths: [tests], timeout_s: 60, accepts_paths: true}\n"
    with pytest.raises(profile.ProfileError, match="accepts_paths"):
        _project(tmp_path, text)


def test_the_zero_executed_hint_names_the_live_db_route_only_when_the_marker_deselects_live_db():
    deselecting = {"marker": "not live_db and not slow", "marker_unless_node_id": True}
    assert "testctl run <project>:live_db --paths <file>" in runner.zero_executed_hint(deselecting)
    assert "name a node id" in runner.zero_executed_hint(deselecting)
    legacy = {"marker": "not slow", "marker_unless_node_id": True}
    assert "live_db" not in runner.zero_executed_hint(legacy) and "name a node id" in runner.zero_executed_hint(legacy)
    assert runner.zero_executed_hint({"marker": "not slow"}) == ""
