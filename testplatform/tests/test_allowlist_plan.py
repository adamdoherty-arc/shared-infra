"""runner.allowlist_plan: a large named-file pytest selection becomes one directory argument plus an allowlist.

This gate would pass trivially if allowlist_plan returned None for every selection (the large-selection case below
asserts a plan), or if it returned a plan for small selections or profiles without allowlist_env (the controls assert
None there).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import runner  # noqa: E402


def _project(**pytest_cfg):
    return SimpleNamespace(profile={"pytest": pytest_cfg})


def _files(n, root="backend/tests"):
    return [f"{root}/test_mod_{i}.py" for i in range(n)]


def test_large_selection_collapses_to_one_directory_and_lists_every_file():
    plan = runner.allowlist_plan(_project(allowlist_env="ADA_COLLECT_ALLOWLIST"), _files(40))
    assert plan is not None
    dirs, allow = plan
    assert dirs == ["backend/tests"]
    assert allow.splitlines() == _files(40)


def test_nested_directories_keep_only_the_outermost_parent_and_node_ids_survive():
    paths = _files(30) + ["backend/tests/audits/test_x.py::TestA::test_b"]
    dirs, allow = runner.allowlist_plan(_project(allowlist_env="E"), paths)
    assert dirs == ["backend/tests"]
    assert "backend/tests/audits/test_x.py::TestA::test_b" in allow.splitlines()


def test_path_prefix_is_stripped_and_duplicates_collapse():
    paths = ["/app/" + p for p in _files(30)] * 2
    dirs, allow = runner.allowlist_plan(_project(allowlist_env="E", path_prefix="/app/"), paths)
    assert dirs == ["backend/tests"]
    assert len(allow.splitlines()) == 30


def test_control_small_selection_keeps_one_argument_per_file():
    assert runner.allowlist_plan(_project(allowlist_env="E"), _files(runner.ALLOWLIST_MIN_FILES - 1)) is None


def test_control_profile_without_allowlist_env_is_unchanged():
    assert runner.allowlist_plan(_project(), _files(100)) is None


def test_control_a_non_test_file_or_directory_argument_disables_the_plan():
    assert runner.allowlist_plan(_project(allowlist_env="E"), _files(30) + ["backend/tests"]) is None
    assert runner.allowlist_plan(_project(allowlist_env="E"), _files(30) + ["backend/tests/conftest.py"]) is None
