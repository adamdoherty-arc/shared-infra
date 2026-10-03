"""codegraph_prune_phantoms drops rows for files that are gone and untracked, and nothing else.

This gate would pass trivially if the sabotage cases below did not leave a ghost beside the kept
rows (every ghost here is both untracked and absent, and its node carries an edge to a kept node,
so the cascade is exercised), if the tracked-but-absent control were also untracked, or if the
brake case removed rows anyway. Each sabotage has its control in the same module.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "codegraph_prune_phantoms.py"

KEPT_TRACKED_PRESENT = "src/tracked_present.py"
KEPT_TRACKED_ABSENT = "src/tracked_absent.py"
KEPT_UNTRACKED_PRESENT = "src/untracked_present.py"
GHOST_A = "src/ghost_a.py"
GHOST_B = "src/ghost_b.py"
ALL_ROWS = [KEPT_TRACKED_PRESENT, KEPT_TRACKED_ABSENT, KEPT_UNTRACKED_PRESENT, GHOST_A, GHOST_B]
KEPT = {KEPT_TRACKED_PRESENT, KEPT_TRACKED_ABSENT, KEPT_UNTRACKED_PRESENT}

SCHEMA = """
CREATE TABLE files (path TEXT PRIMARY KEY);
CREATE TABLE nodes (id TEXT PRIMARY KEY, file_path TEXT NOT NULL);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, target TEXT NOT NULL,
    FOREIGN KEY (source) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target) REFERENCES nodes(id) ON DELETE CASCADE
);
"""


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("codegraph_prune_phantoms_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _touch(root: Path, rel: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n", encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    for rel in (KEPT_TRACKED_PRESENT, KEPT_TRACKED_ABSENT):
        _touch(root, rel)
    _git(root, "add", KEPT_TRACKED_PRESENT, KEPT_TRACKED_ABSENT)
    (root / KEPT_TRACKED_ABSENT).unlink()
    _touch(root, KEPT_UNTRACKED_PRESENT)
    (root / ".codegraph").mkdir()
    conn = sqlite3.connect(root / ".codegraph" / "codegraph.db")
    conn.executescript(SCHEMA)
    for rel in ALL_ROWS:
        conn.execute("INSERT INTO files (path) VALUES (?)", (rel,))
        conn.execute("INSERT INTO nodes (id, file_path) VALUES (?, ?)", (rel, rel))
    edges = [
        (GHOST_A, KEPT_TRACKED_PRESENT),
        (KEPT_TRACKED_PRESENT, GHOST_B),
        (KEPT_TRACKED_PRESENT, KEPT_UNTRACKED_PRESENT),
    ]
    conn.executemany("INSERT INTO edges (source, target) VALUES (?, ?)", edges)
    conn.commit()
    conn.close()
    return root


def _rows(root: Path) -> tuple[set[str], set[str], int]:
    conn = sqlite3.connect(root / ".codegraph" / "codegraph.db")
    files = {r[0] for r in conn.execute("SELECT path FROM files")}
    nodes = {r[0] for r in conn.execute("SELECT id FROM nodes")}
    edge_count = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
    conn.close()
    return files, nodes, edge_count


def test_sabotage_ghost_rows_are_removed_with_their_nodes_and_cascaded_edges(tmp_path):
    root = _project(tmp_path)
    report = _module().prune_root(root, dry_run=False, max_fraction=0.5)
    files, nodes, edge_count = _rows(root)
    assert report["status"] == "pruned"
    assert report["deleted"] == 2
    assert files == KEPT
    assert nodes == KEPT
    assert edge_count == 1


def test_control_a_tracked_file_missing_from_disk_keeps_its_rows(tmp_path):
    root = _project(tmp_path)
    _module().prune_root(root, dry_run=False, max_fraction=0.5)
    files, nodes, _ = _rows(root)
    assert KEPT_TRACKED_ABSENT in files
    assert KEPT_TRACKED_ABSENT in nodes


def test_control_an_untracked_file_present_on_disk_keeps_its_rows(tmp_path):
    root = _project(tmp_path)
    _module().prune_root(root, dry_run=False, max_fraction=0.5)
    files, nodes, _ = _rows(root)
    assert KEPT_UNTRACKED_PRESENT in files
    assert KEPT_UNTRACKED_PRESENT in nodes


def test_sabotage_a_root_where_most_files_look_missing_is_refused_and_untouched(tmp_path):
    root = _project(tmp_path)
    report = _module().prune_root(root, dry_run=False, max_fraction=0.3)
    files, nodes, edge_count = _rows(root)
    assert report["status"] == "refused_too_many_missing"
    assert report["deleted"] == 0
    assert files == set(ALL_ROWS)
    assert nodes == set(ALL_ROWS)
    assert edge_count == 3


def test_control_dry_run_reports_the_ghosts_and_deletes_nothing(tmp_path):
    root = _project(tmp_path)
    report = _module().prune_root(root, dry_run=True, max_fraction=0.5)
    files, _, edge_count = _rows(root)
    assert report["status"] == "dry_run"
    assert report["ghosts"] == 2
    assert files == set(ALL_ROWS)
    assert edge_count == 3


def test_sabotage_a_file_recreated_after_the_scan_keeps_its_rows(tmp_path, monkeypatch):
    root = _project(tmp_path)
    module = _module()
    real = module.still_missing
    calls = {"n": 0}

    def recreating(base: Path, paths: list[str]) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 2:
            _touch(root, GHOST_A)
        return real(base, paths)

    monkeypatch.setattr(module, "still_missing", recreating)
    report = module.prune_root(root, dry_run=False, max_fraction=0.5)
    files, _, _ = _rows(root)
    assert report["deleted"] == 1
    assert GHOST_A in files
    assert GHOST_B not in files


def test_control_a_clean_index_deletes_nothing(tmp_path):
    root = _project(tmp_path)
    _touch(root, GHOST_A)
    _touch(root, GHOST_B)
    report = _module().prune_root(root, dry_run=False, max_fraction=0.5)
    assert report["status"] == "clean"
    assert report["deleted"] == 0


def test_a_directory_without_an_index_and_a_non_git_index_are_skipped(tmp_path):
    module = _module()
    bare = tmp_path / "bare"
    bare.mkdir()
    assert module.prune_root(bare, dry_run=False, max_fraction=0.5)["status"] == "no_index"
    plain = tmp_path / "plain"
    (plain / ".codegraph").mkdir(parents=True)
    sqlite3.connect(plain / ".codegraph" / "codegraph.db").close()
    assert module.prune_root(plain, dry_run=False, max_fraction=0.5)["status"] == "not_a_git_repo"


@pytest.mark.parametrize("refused", [True, False])
def test_main_exits_2_only_when_a_root_is_refused(tmp_path, capsys, refused):
    root = _project(tmp_path)
    fraction = "0.3" if refused else "0.5"
    code = _module().main(["--root", str(root), "--dry-run", "--max-missing-fraction", fraction])
    out = capsys.readouterr().out
    assert code == (2 if refused else 0)
    assert ("refused_too_many_missing" in out) is refused
