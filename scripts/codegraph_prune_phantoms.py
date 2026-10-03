"""Prune codegraph rows for files that are gone from disk and were never tracked by git.

codegraph's sync asks `git status` which files changed. A file that was untracked, indexed by the
watcher and deleted before any commit never appears in git's output, so its rows stay in the index
for good and answer symbol searches with duplicates. Measured 2026-10-03 on ADA: 6,500 of 18,401
indexed files were such ghosts (one deleted snapshot directory, `.claude/state/ss_arch`, plus
probe and sabotage copies), including three definitions of live_stake_ladder.apply_ladder.

Tracked files are never touched: git reports their deletion itself, and a tracked row dropped
while the file is mid-rewrite would not be re-added until the next change. An untracked file that
reappears is re-added by the next sync, because git lists every untracked file as added.

This gate would pass trivially if it pruned tracked files, if it did not re-check the disk at
delete time (a file recreated after the scan would lose its rows), or if it ran on a root whose
files all look missing because a drive or mount is down; the missing-fraction brake refuses that
case. scripts/tests/test_codegraph_prune_phantoms.py carries a sabotage for each beside its control.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

DEFAULT_PARENT = Path("C:/code")
BATCH_SIZE = 50
MAX_MISSING_FRACTION = 0.5
BUSY_TIMEOUT_MS = 120_000
GIT_TIMEOUT_S = 120


def tracked_paths(root: Path) -> set[str] | None:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=GIT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        return None
    return {p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p}


def still_missing(root: Path, paths: list[str]) -> list[str]:
    return [p for p in paths if not (root / p).exists()]


def prune_root(root: Path, *, dry_run: bool, max_fraction: float) -> dict[str, object]:
    report: dict[str, object] = {"root": str(root).replace("\\", "/")}
    db_path = root / ".codegraph" / "codegraph.db"
    if not db_path.is_file():
        return {**report, "status": "no_index"}
    tracked = tracked_paths(root)
    if tracked is None:
        return {**report, "status": "not_a_git_repo"}
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        indexed = [row[0] for row in conn.execute("SELECT path FROM files")]
        ghosts = still_missing(root, [p for p in indexed if p not in tracked])
        report.update(indexed=len(indexed), ghosts=len(ghosts))
        if not ghosts:
            return {**report, "status": "clean", "deleted": 0}
        if len(ghosts) > max_fraction * len(indexed):
            return {**report, "status": "refused_too_many_missing", "deleted": 0}
        if dry_run:
            return {**report, "status": "dry_run", "deleted": 0}
        deleted = 0
        for start in range(0, len(ghosts), BATCH_SIZE):
            batch = still_missing(root, ghosts[start : start + BATCH_SIZE])
            with conn:
                for path in batch:
                    conn.execute("DELETE FROM nodes WHERE file_path = ?", (path,))
                    conn.execute("DELETE FROM files WHERE path = ?", (path,))
            deleted += len(batch)
        return {**report, "status": "pruned", "deleted": deleted}
    finally:
        conn.close()


def discover_roots(parent: Path) -> list[Path]:
    return sorted(p for p in parent.iterdir() if (p / ".codegraph" / "codegraph.db").is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune codegraph rows for vanished untracked files.")
    parser.add_argument("--root", action="append", type=Path, help="project root, repeatable")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-missing-fraction", type=float, default=MAX_MISSING_FRACTION)
    args = parser.parse_args(argv)
    roots = args.root or discover_roots(DEFAULT_PARENT)
    exit_code = 0
    for root in roots:
        try:
            report = prune_root(root, dry_run=args.dry_run, max_fraction=args.max_missing_fraction)
        except sqlite3.OperationalError as exc:
            report = {"root": str(root).replace("\\", "/"), "status": "db_error", "error": str(exc)}
        print(json.dumps(report))
        if report["status"] == "refused_too_many_missing":
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
