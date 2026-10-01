"""Committed-tree isolation for runtimes that read the repo from the host (`run` containers, host vitest, host gates).

An official whole-tier run must test what is committed, not whatever another session has half-edited in the live
working tree. `exec` runtimes (ADA) already do this by streaming `git archive` into their container
(`runner.sync_snapshot`); this module does it on the host: `git archive <rev>` into a fresh per-run directory,
which the runner then bind-mounts (pytest in a `run` container) or uses as the cwd (host vitest, host gates).

Placement. `snapshot.location` (a repo subdir, e.g. `console/web`) puts the tree at
`<repo>/<location>/.tp-snapshot/<run-id>/`. That is what lets Node resolve the REAL `<repo>/<location>/node_modules`
by walking up the directory tree, with no junction or symlink into the real tree. Without `location` the tree goes
under the run's artifact dir. Untracked runtime files (node_modules, .venv, .env) are never copied.

Safety. A cleanup that followed a Windows junction once emptied a real node_modules and .venv, so nothing here ever
creates a link, and `safe_rmtree` (a) only deletes a directory strictly inside the expected snapshot root that carries
our marker file, and (b) never recurses into a symlink or junction: it removes the link itself and stops.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SNAP_DIRNAME = ".tp-snapshot"
MARKER = ".tp-snapshot-marker"
STALE_AFTER_S = 3 * 3600
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")


class SnapshotError(Exception):
    pass


def is_link(path: str | os.PathLike[str]) -> bool:
    """True for a symlink, an NTFS junction or any other reparse point; never follows it."""
    p = Path(path)
    try:
        if p.is_symlink():
            return True
        is_junction = getattr(p, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        st = os.lstat(p)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def _strictly_inside(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    p, r = _norm(path), _norm(root)
    return p != r and p.startswith(r.rstrip("\\/") + os.sep)


def _remove_link(path: str) -> None:
    """Remove the link itself, never its target: unlink for a file link, rmdir for a directory link or junction."""
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)


def _rm_entry(path: str) -> None:
    if is_link(path):
        _remove_link(path)
        return
    if os.path.isdir(path):
        with os.scandir(path) as it:
            children = [e.path for e in it]
        for child in children:
            _rm_entry(child)
        try:
            os.rmdir(path)
        except PermissionError:
            os.chmod(path, stat.S_IRWXU)
            os.rmdir(path)
        return
    try:
        os.unlink(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)


def safe_rmtree(path: str | os.PathLike[str], allowed_root: str | os.PathLike[str]) -> None:
    """Delete `path`, refusing anything that is not a real directory strictly inside `allowed_root`.

    Refuses: a path outside the root (or the root itself), a path whose own entry or any ancestor up to the root is a
    link, and a directory without our marker file. Never follows a link while deleting.
    """
    if not _strictly_inside(path, allowed_root):
        raise SnapshotError(f"refusing to delete {path}: not strictly inside {allowed_root}")
    if is_link(allowed_root):
        raise SnapshotError(f"refusing to delete under {allowed_root}: the snapshot root is a link")
    cur = Path(os.path.abspath(path))
    while _norm(cur) != _norm(allowed_root):
        if is_link(cur):
            raise SnapshotError(f"refusing to delete {path}: {cur} is a link")
        cur = cur.parent
    if not os.path.lexists(path):
        return
    if not os.path.isfile(os.path.join(path, MARKER)):
        raise SnapshotError(f"refusing to delete {path}: it has no {MARKER}, so testctl did not create it")
    _rm_entry(os.path.abspath(path))


@dataclass
class Snapshot:
    root: Path        # the extracted committed tree
    base: Path        # directory that contains it: the only place cleanup may delete under
    rev: str          # the resolved commit sha
    seconds: float = 0.0

    def cleanup(self) -> None:
        safe_rmtree(self.root, self.base)
        if self.base.name == SNAP_DIRNAME:
            try:
                self.base.rmdir()  # only succeeds when empty; a concurrent run's dir keeps it
            except OSError:
                pass

    def describe(self) -> str:
        return f"snapshot {self.rev[:12]} -> {self.root.as_posix()} in {self.seconds:.1f}s"


def _git(repo: Path, *args: str) -> list[str]:
    return ["git", "-c", "safe.directory=*", "-C", str(repo), *args]


def resolve_rev(repo: Path, rev: str) -> str:
    proc = subprocess.run(_git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}"), capture_output=True, text=True,
                          timeout=60, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise SnapshotError(f"cannot resolve {rev!r} in {repo}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def extract_archive(stream: Any, dest: Path) -> int:
    """Extract a git-archive tar stream into `dest`: regular files and directories only (no link of any kind is
    created, and a member that would land outside `dest` is refused). Returns the file count."""
    n = 0
    root = _norm(dest)
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for m in tar:
            if m.name == "pax_global_header" or m.issym() or m.islnk():
                continue
            if not (m.isfile() or m.isdir()):
                continue
            target = os.path.join(str(dest), *m.name.split("/"))
            if not _norm(target).startswith(root + os.sep) and _norm(target) != root:
                raise SnapshotError(f"archive member escapes the snapshot: {m.name}")
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                continue
            with open(target, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            n += 1
    return n


def sweep_stale(base: Path, now: float | None = None) -> int:
    """Remove snapshot dirs a crashed run left behind (ours only: marker present, older than STALE_AFTER_S)."""
    if not base.is_dir() or is_link(base):
        return 0
    cutoff = (now or time.time()) - STALE_AFTER_S
    removed = 0
    for child in base.iterdir():
        if not _RUN_ID.match(child.name) or is_link(child) or not (child / MARKER).is_file():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                safe_rmtree(child, base)
                removed += 1
        except (OSError, SnapshotError):
            continue
    return removed


def create(repo: Path, cfg: dict[str, Any], art: Path) -> Snapshot:
    """Build the committed tree for one run. Raises SnapshotError; the caller turns that into an `error` verdict."""
    started = time.time()
    sha = resolve_rev(repo, str(cfg.get("rev", "HEAD")))
    location = str(cfg.get("location") or "").strip("/")
    base = (repo / location / SNAP_DIRNAME) if location else (art / "snapshot")
    if location:
        sweep_stale(base)
    run_dir = base / uuid.uuid4().hex
    include = list(cfg.get("include") or ["."])
    exclude = [f":(exclude){x}" for x in cfg.get("exclude", [])]
    run_dir.mkdir(parents=True)
    (run_dir / MARKER).write_text(json.dumps({"rev": sha, "at": time.time(), "pid": os.getpid()}), encoding="utf-8")
    snap = Snapshot(run_dir, base, sha)
    try:
        producer = subprocess.Popen(_git(repo, "archive", "--format=tar", sha, "--", *include, *exclude),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        try:
            files = extract_archive(producer.stdout, run_dir)
        except tarfile.TarError:
            files = 0  # git wrote no archive; its own exit code and stderr below say why
        finally:
            producer.stdout.close()
        err = producer.stderr.read().decode("utf-8", "replace")
        producer.stderr.close()
        if producer.wait(timeout=60) != 0:
            raise SnapshotError(f"git archive failed rc={producer.returncode}: {err.strip()[:200]}")
        if files == 0:
            raise SnapshotError(f"git archive {sha[:12]} produced no files for include={include}")
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        discard(snap)
        raise SnapshotError(f"snapshot extract failed: {type(exc).__name__}: {exc}") from exc
    except SnapshotError:
        discard(snap)
        raise
    snap.seconds = time.time() - started
    return snap


def discard(snap: Snapshot) -> None:
    try:
        snap.cleanup()
    except (OSError, SnapshotError):
        pass


def wanted(runtime: dict[str, Any], tier: dict[str, Any], framework: str | None, vitest_kind: str | None,
           paths: list[str] | None, changed_paths: list[str] | None) -> bool:
    """Whole tiers of host-read runtimes use a snapshot; a path or `--paths` target stays live so a developer can run
    the file they are editing (untracked included). `exec` runtimes keep their own container snapshot."""
    cfg = runtime.get("snapshot")
    if not cfg or runtime.get("kind") == "exec" or tier.get("snapshot") is False:
        return False
    if paths or changed_paths:
        return False
    if framework == "pytest":
        return runtime.get("kind") == "run"
    if framework == "vitest":
        return vitest_kind == "host"
    return framework == "commands"
