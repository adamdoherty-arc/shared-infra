"""Committed-tree isolation for `run` containers, host vitest and host gates (tplib/snapshot.py).

Origin: two official customer-ops runs on 2026-10-01 failed because another session had a half-edit in the live
working tree. The cleanup tests exist because a cleanup that followed a Windows junction the same day emptied a
real node_modules and .venv: no deletion here may ever reach through a link.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import profile, runner, snapshot  # noqa: E402

PROFILE = """
framework: pytest
default_target: fast
path_tier: path
runtime:
  kind: run
  image: img:local
  workdir: /opt/app
  mounts:
    - {host: ".", container: /opt/app, mode: ro}
  tmpfs: [/opt/app/data]
  snapshot: {rev: HEAD, location: web, exclude: [docs]}
vitest: {kind: host, repo_subdir: web}
tiers:
  fast: {paths: [tests], timeout_s: 60}
  path: {timeout_s: 30, min_executed: 1}
  vitest: {framework: vitest, timeout_s: 60}
  gates:
    framework: commands
    timeout_s: 60
    commands:
      - {name: g, run: [python, tool.py, "{repo}/x"], env: {PYTHON: "{repo}/.venv/py"}}
  live_gates:
    framework: commands
    snapshot: false
    timeout_s: 60
    commands:
      - {name: g, run: [python, tool.py]}
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo), *args], check=True,
                   capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "web" / "src").mkdir(parents=True)
    (r / "web" / "node_modules").mkdir()
    (r / "web" / "node_modules" / "dep.js").write_text("real", encoding="utf-8")
    (r / "tests").mkdir()
    (r / "docs").mkdir()
    (r / "tests" / "test_a.py").write_text("COMMITTED\n", encoding="utf-8")
    (r / "web" / "src" / "a.ts").write_text("committed\n", encoding="utf-8")
    (r / "docs" / "d.md").write_text("doc\n", encoding="utf-8")
    (r / "tool.py").write_text("t\n", encoding="utf-8")
    (r / ".gitignore").write_text("node_modules/\n.tp-snapshot/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def _project(tmp_path: Path, repo: Path) -> profile.Project:
    (repo / ".testplatform.yml").write_text(PROFILE, encoding="utf-8")
    reg = tmp_path / "projects.yml"
    reg.write_text(f"projects:\n  demo:\n    root: {repo.as_posix()}\n    legion_project_id: 9\n", encoding="utf-8")
    return profile.load_project("demo", reg)


def _make_dir_link(link: Path, target: Path) -> bool:
    """A directory symlink, or an NTFS junction where symlinks need privilege. False if neither can be made."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return proc.returncode == 0
    return False


def _marked(root: Path, name: str = "a" * 32) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / snapshot.MARKER).write_text("{}", encoding="utf-8")
    return d


# ---- cleanup safety ------------------------------------------------------------------------------------------------

def test_cleanup_never_follows_a_link_into_a_real_directory(tmp_path):
    """The 2026-10-01 incident: a link inside the snapshot pointed at a real node_modules/.venv."""
    base = tmp_path / ".tp-snapshot"
    snap = _marked(base)
    sentinel = tmp_path / "real_node_modules"
    (sentinel / "pkg").mkdir(parents=True)
    (sentinel / "pkg" / "index.js").write_text("keep me", encoding="utf-8")
    (sentinel / "top.txt").write_text("keep me too", encoding="utf-8")
    (snap / "src").mkdir()
    (snap / "src" / "x.ts").write_text("x", encoding="utf-8")
    if not _make_dir_link(snap / "node_modules", sentinel):
        pytest.skip("cannot create a symlink or junction on this host")
    if not _make_dir_link(snap / "src" / "nested_link", sentinel):
        pytest.skip("cannot create a second link")
    assert snapshot.is_link(snap / "node_modules")

    snapshot.safe_rmtree(snap, base)

    assert not snap.exists()
    assert (sentinel / "pkg" / "index.js").read_text(encoding="utf-8") == "keep me"
    assert (sentinel / "top.txt").read_text(encoding="utf-8") == "keep me too"


def test_cleanup_removes_a_file_symlink_without_touching_its_target(tmp_path):
    base = tmp_path / ".tp-snapshot"
    snap = _marked(base)
    target = tmp_path / "secret.txt"
    target.write_text("keep", encoding="utf-8")
    try:
        os.symlink(target, snap / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a file symlink on this host")
    snapshot.safe_rmtree(snap, base)
    assert target.read_text(encoding="utf-8") == "keep" and not snap.exists()


def test_safe_rmtree_refuses_paths_outside_the_expected_root(tmp_path):
    base = tmp_path / ".tp-snapshot"
    base.mkdir()
    outside = _marked(tmp_path / "elsewhere")
    with pytest.raises(snapshot.SnapshotError):
        snapshot.safe_rmtree(outside, base)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.safe_rmtree(base, base)  # the root itself
    with pytest.raises(snapshot.SnapshotError):
        snapshot.safe_rmtree(base / ".." / "elsewhere", base)  # traversal
    assert outside.exists() and base.exists()


def test_safe_rmtree_refuses_a_directory_testctl_did_not_create(tmp_path):
    base = tmp_path / ".tp-snapshot"
    foreign = base / ("b" * 32)
    foreign.mkdir(parents=True)
    (foreign / "mine.txt").write_text("a developer's files", encoding="utf-8")
    with pytest.raises(snapshot.SnapshotError, match="no .tp-snapshot-marker"):
        snapshot.safe_rmtree(foreign, base)
    assert (foreign / "mine.txt").exists()


def test_safe_rmtree_refuses_when_the_snapshot_root_or_an_ancestor_is_a_link(tmp_path):
    real = tmp_path / "real_base"
    victim = _marked(real)
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    base_link = tmp_path / ".tp-snapshot"
    if not _make_dir_link(base_link, real):
        pytest.skip("cannot create a symlink or junction on this host")
    with pytest.raises(snapshot.SnapshotError):
        snapshot.safe_rmtree(base_link / victim.name, base_link)
    assert (victim / "keep.txt").exists()


def test_sweep_removes_only_our_stale_dirs(tmp_path):
    base = tmp_path / ".tp-snapshot"
    old, fresh, foreign = _marked(base, "a" * 32), _marked(base, "c" * 32), base / ("d" * 32)
    foreign.mkdir()
    (foreign / "x").write_text("x", encoding="utf-8")
    past = 1_000_000.0
    os.utime(old, (past, past))
    os.utime(foreign, (past, past))
    assert snapshot.sweep_stale(base) == 1
    assert not old.exists() and fresh.exists() and (foreign / "x").exists()


# ---- extraction ------------------------------------------------------------------------------------------------------

def test_snapshot_holds_committed_content_only(repo, tmp_path):
    (repo / "tests" / "test_a.py").write_text("DIRTY HALF EDIT\n", encoding="utf-8")        # tracked, uncommitted
    (repo / "tests" / "test_zz_probe.py").write_text("untracked\n", encoding="utf-8")       # untracked
    snap = snapshot.create(repo, {"rev": "HEAD", "location": "web", "exclude": ["docs"]}, tmp_path / "art")
    try:
        assert snap.root.parent == repo / "web" / ".tp-snapshot"
        assert (snap.root / "tests" / "test_a.py").read_text(encoding="utf-8") == "COMMITTED\n"
        assert not (snap.root / "tests" / "test_zz_probe.py").exists()
        assert not (snap.root / "web" / "node_modules").exists(), "untracked runtime dirs must not be copied"
        assert not (snap.root / "docs").exists(), "exclude is honoured"
        assert (snap.root / "web" / "src" / "a.ts").is_file()
        assert not any(snapshot.is_link(p) for p in snap.root.rglob("*")), "a snapshot contains no links"
    finally:
        snap.cleanup()
    assert not snap.root.exists()
    assert not (repo / "web" / ".tp-snapshot").exists(), "the empty snapshot root is removed too"
    assert (repo / "web" / "node_modules" / "dep.js").read_text(encoding="utf-8") == "real"
    assert (repo / "tests" / "test_a.py").read_text(encoding="utf-8") == "DIRTY HALF EDIT\n", "live tree untouched"


def test_snapshot_without_location_lives_under_the_artifact_dir(repo, tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    snap = snapshot.create(repo, {"rev": "HEAD"}, art)
    assert snap.base == art / "snapshot" and (snap.root / "tests" / "test_a.py").is_file()
    snap.cleanup()
    assert not snap.root.exists()


def test_snapshot_failure_is_an_error_not_a_live_tree_fallback(repo, tmp_path):
    with pytest.raises(snapshot.SnapshotError):
        snapshot.create(repo, {"rev": "no-such-rev"}, tmp_path)
    with pytest.raises(snapshot.SnapshotError, match="git archive"):
        snapshot.create(repo, {"rev": "HEAD", "include": ["nothing-here"]}, tmp_path / "a")
    assert not (repo / "web" / ".tp-snapshot").exists()


# ---- which targets use a snapshot ---------------------------------------------------------------------------------

def test_wanted_applies_to_whole_tiers_only(tmp_path, repo):
    p = _project(tmp_path, repo)
    rt, tiers = p.profile["runtime"], p.profile["tiers"]
    assert snapshot.wanted(rt, tiers["fast"], "pytest", "host", None, None)
    assert snapshot.wanted(rt, tiers["vitest"], "vitest", "host", None, None)
    assert snapshot.wanted(rt, tiers["gates"], "commands", "host", None, None)
    # path / changed --paths stay live so a developer can run the file they are editing
    assert not snapshot.wanted(rt, tiers["path"], "pytest", "host", ["tests/test_a.py"], None)
    assert not snapshot.wanted(rt, tiers["fast"], "pytest", "host", None, ["x.py"])
    assert not snapshot.wanted(rt, tiers["vitest"], "vitest", "host", ["web/src/a.test.ts"], None)
    # a tier can opt out; exec runtimes keep their container snapshot; no config means live
    assert not snapshot.wanted(rt, tiers["live_gates"], "commands", "host", None, None)
    assert not snapshot.wanted({**rt, "kind": "exec"}, tiers["fast"], "pytest", None, None, None)
    assert not snapshot.wanted({k: v for k, v in rt.items() if k != "snapshot"}, tiers["fast"], "pytest", "host", None, None)
    assert not snapshot.wanted(rt, tiers["vitest"], "vitest", "container", None, None)


# ---- runner wiring (no Docker, no Node: _run is faked) ----------------------------------------------------------------

def test_run_container_mounts_the_snapshot_not_the_live_repo(tmp_path, repo, monkeypatch):
    p = _project(tmp_path, repo)
    seen: list[list[str]] = []
    mounted_during: list[bool] = []

    def fake_run(cmd, timeout, out_file=None, cwd=None, env=None):
        seen.append(cmd)
        mount = next(c for c in cmd if c.endswith(":/opt/app:ro")).split(":/opt/app")[0]
        mounted_during.append((Path(mount) / "tests" / "test_a.py").read_text(encoding="utf-8") == "COMMITTED\n")
        return 1, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    (repo / "tests" / "test_a.py").write_text("DIRTY\n", encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    exe = runner.execute_framework(p, p.tier("fast"), None, "claude", art, _NoLegion(), "fast")
    assert mounted_during == [True]
    mount_src = next(c for c in seen[0] if c.endswith(":/opt/app:ro"))
    assert ".tp-snapshot" in mount_src and mount_src.split(":/opt/app")[0] != repo.resolve().as_posix()
    assert "snapshot" in (art / "snapshot.log").read_text(encoding="utf-8")
    assert exe.status == "error"  # fake run produced no report; the point is the mount
    assert not (repo / "web" / ".tp-snapshot").exists(), "cleaned up after the run"


def test_path_target_mounts_the_live_repo(tmp_path, repo, monkeypatch):
    p = _project(tmp_path, repo)
    seen: list[list[str]] = []
    monkeypatch.setattr(runner, "_run", lambda cmd, timeout, out_file=None, cwd=None, env=None: (seen.append(cmd), (1, ""))[1])
    art = tmp_path / "art"
    art.mkdir()
    runner.execute_framework(p, p.tier("path"), ["tests/test_a.py"], "claude", art, _NoLegion(), "tests/test_a.py")
    mount_src = next(c for c in seen[0] if c.endswith(":/opt/app:ro")).split(":/opt/app")[0]
    assert mount_src == repo.resolve().as_posix()
    assert not (art / "snapshot.log").exists()


def test_tmpfs_masks_follow_the_mount_root(tmp_path, repo):
    p = _project(tmp_path, repo)
    rt = p.profile["runtime"]
    assert runner.tmpfs_masks(repo, rt) == []  # /opt/app/data does not exist in the tree
    (repo / "data").mkdir()
    assert runner.tmpfs_masks(repo, rt) == ["/opt/app/data"]


def test_host_vitest_runs_in_the_snapshot_subdir_and_cleans_up(tmp_path, repo, monkeypatch):
    p = _project(tmp_path, repo)
    calls: list[Path] = []

    def fake_run(cmd, timeout, out_file=None, cwd=None, env=None):
        calls.append(Path(cwd))
        assert (Path(cwd) / "src" / "a.ts").read_text(encoding="utf-8") == "committed\n"
        # Node resolution: the real node_modules is an ancestor of the snapshot's web dir.
        assert any((a / "node_modules" / "dep.js").is_file() for a in [Path(cwd), *Path(cwd).parents])
        assert not (Path(cwd) / "node_modules").exists()
        return 1, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "npx")
    (repo / "web" / "src" / "a.ts").write_text("half edit\n", encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    exe = runner.execute_framework(p, p.tier("vitest"), None, "claude", art, _NoLegion(), "vitest")
    assert exe.status == "error" and len(calls) == 1 and ".tp-snapshot" in calls[0].as_posix()
    assert calls[0].name == "web"
    assert not (repo / "web" / ".tp-snapshot").exists()
    assert (repo / "web" / "node_modules" / "dep.js").is_file()


def test_gates_run_in_the_snapshot_and_get_the_live_root_for_untracked_tools(tmp_path, repo, monkeypatch):
    p = _project(tmp_path, repo)
    seen: list[dict] = []

    def fake_run(cmd, timeout, out_file=None, cwd=None, env=None):
        seen.append({"cmd": cmd, "cwd": Path(cwd), "env": env})
        return 0, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    art = tmp_path / "art"
    art.mkdir()
    exe = runner.execute_framework(p, p.tier("gates"), None, "claude", art, _NoLegion(), "gates")
    assert exe.status == "passed"
    got = seen[0]
    assert ".tp-snapshot" in got["cwd"].as_posix()
    assert got["cmd"][-1] == f"{repo.as_posix()}/x"
    assert got["env"]["PYTHON"] == f"{repo.as_posix()}/.venv/py"
    assert got["env"]["TP_SNAPSHOT_ROOT"] == got["cwd"].as_posix() and got["env"]["TP_LIVE_ROOT"] == repo.as_posix()
    # opted-out tier runs in the live tree
    seen.clear()
    runner.execute_framework(p, p.tier("live_gates"), None, "claude", art, _NoLegion(), "live_gates")
    assert seen[0]["cwd"] == repo


def test_snapshot_is_cleaned_up_when_the_framework_raises(tmp_path, repo, monkeypatch):
    p = _project(tmp_path, repo)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "run_commands", boom)
    art = tmp_path / "art"
    art.mkdir()
    with pytest.raises(RuntimeError):
        runner.execute_framework(p, p.tier("gates"), None, "claude", art, _NoLegion(), "gates")
    assert not (repo / "web" / ".tp-snapshot").exists()


def test_unbuildable_snapshot_is_an_error_verdict_never_a_live_run(tmp_path, repo, monkeypatch):
    p = _project(tmp_path, repo)
    p.profile["runtime"]["snapshot"]["rev"] = "no-such-rev"
    ran: list[int] = []
    monkeypatch.setattr(runner, "_run", lambda *a, **k: (ran.append(1), (0, ""))[1])
    art = tmp_path / "art"
    art.mkdir()
    exe = runner.execute_framework(p, p.tier("fast"), None, "claude", art, _NoLegion(), "fast")
    assert exe.status == "error" and "snapshot" in (exe.error_summary or "") and not ran


class _NoLegion:
    def quarantine(self, _pid):
        return []
