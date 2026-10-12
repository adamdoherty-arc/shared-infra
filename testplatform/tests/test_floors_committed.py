"""A heavy run tests the committed tree, so its service floors are the committed floors.

Origin: ADA ada:fast run 11916 (2026-10-12) ended `error` after 1486s: the live floors file carried a labs_engine
floor naming test_labs_trust_tier_clamp, an untracked test file the clean HEAD snapshot does not contain, so the
floor read as "matched zero tests" and a 48,303-test run was thrown away. Each check has a sabotage case and a control.

This gate would pass trivially if the committed text were never consulted (the dirty file read instead) or if a
tracked-but-unreadable floors file fell back to the live copy.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import runner  # noqa: E402

COMMITTED = {"floors": {"core": {"classname_prefixes": ["test_core"], "min_executed": 1}}}
DIRTY = {"floors": {**COMMITTED["floors"], "labs": {"classname_prefixes": ["test_untracked"], "min_executed": 1}}}
CASES = [{"node_id": "tests/test_core.py::t", "status": "passed"}]


class _P:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.profile = {"runtime": {"snapshot": {"rev": "HEAD"}}}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
                   capture_output=True)


def _repo(tmp_path: Path, *, commit: bool = True) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "floors.json").write_text(json.dumps(COMMITTED), encoding="utf-8")
    if commit:
        _git(root, "add", "floors.json")
        _git(root, "commit", "-q", "-m", "floors")
    return root


def test_control_a_clean_floors_file_passes(tmp_path):
    root = _repo(tmp_path)
    text = runner.committed_floors_text(_P(root), {"floors_file": "floors.json"})
    assert runner.check_floors(root / "floors.json", CASES, text) is None


def test_sabotage_a_dirty_floor_for_an_untracked_test_does_not_breach_the_committed_tree(tmp_path):
    root = _repo(tmp_path)
    (root / "floors.json").write_text(json.dumps(DIRTY), encoding="utf-8")
    assert runner.check_floors(root / "floors.json", CASES) is not None
    text = runner.committed_floors_text(_P(root), {"floors_file": "floors.json"})
    assert runner.check_floors(root / "floors.json", CASES, text) is None


def test_control_a_committed_floor_still_breaches(tmp_path):
    root = _repo(tmp_path)
    text = runner.committed_floors_text(_P(root), {"floors_file": "floors.json"})
    breach = runner.check_floors(root / "floors.json", [{"node_id": "tests/test_other.py::t", "status": "passed"}], text)
    assert breach and "test_core" in breach


def test_an_untracked_floors_file_is_read_live(tmp_path):
    root = _repo(tmp_path, commit=False)
    text = runner.committed_floors_text(_P(root), {"floors_file": "floors.json"})
    assert text is not None and json.loads(text) == COMMITTED
