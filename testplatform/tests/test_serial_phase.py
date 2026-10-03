"""A whole pytest tier's serial phase (`serial_marker`) and the snapshot pinned to the reported sha.

Origin: ADA sprint 15335 (2026-10-03). Nightly full run 3208 lost 2,355 tests to an OOM-killed xdist worker, so
TestMonteCarloOracle (one process grows by ~2.7 GB) took a memory_heavy marker and every parallel tier deselected
it. That also took it out of `testctl run ada:bitcoin`, the preflight scripts/bitcoin_prod_release.py runs before it
releases the live-money engine: runs 3718 and 3769 executed 0 of its 12 cases and the engine was released on both.
A tier's serial phase runs such tests in one process after the tier's workers exit, on the same snapshot, under one
verdict. Each check below has a sabotage case beside its control.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import profile, runner  # noqa: E402

PROFILE = """
framework: pytest
default_target: fast
path_tier: path
runtime:
  kind: exec
  container: c
  snapshot: {dest: /snap, include: [tests]}
pytest:
  common_args: [--rootdir=/app]
tiers:
  fast:
    paths: [tests]
    marker: not heavy
    serial_marker: heavy
    workers: 4
    timeout_s: 60
    min_executed: 1
    floors_file: floors.json
  bitcoin:
    paths: [tests]
    marker: not heavy
    serial_marker: heavy
    serial_min_executed: 1
    serial_timeout_s: 30
    workers: 4
    timeout_s: 60
    min_executed: 1
    args: [-k, bitcoin]
  plain: {paths: [tests], marker: not heavy, workers: 4, timeout_s: 60}
  path: {timeout_s: 30}
"""

FLOORS = {"floors": {"oracle": {"classname_prefixes": ["test_heavy"], "min_executed": 1}}}
PARALLEL_CASE = {"nodeid": "tests/test_bitcoin_a.py::t", "outcome": "passed"}
SERIAL_CASE = {"nodeid": "tests/test_heavy.py::test_oracle", "outcome": "passed"}


def _project(tmp_path: Path, text: str = PROFILE) -> profile.Project:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".testplatform.yml").write_text(text, encoding="utf-8")
    (repo / "floors.json").write_text(json.dumps(FLOORS), encoding="utf-8")
    reg = tmp_path / "projects.yml"
    reg.write_text(f"projects:\n  demo:\n    root: {repo.as_posix()}\n    legion_project_id: 9\n", encoding="utf-8")
    return profile.load_project("demo", reg)


class _Legion:
    def __init__(self, quarantined: list[str] | None = None) -> None:
        self.quarantined = quarantined or []
        self.posted: dict = {}

    def quarantine(self, project_id):
        return list(self.quarantined)

    def create_run(self, body):
        self.created = body
        return 7

    def post_results(self, run_id, payload):
        self.posted[run_id] = payload
        return {"text": f"{payload['status']} {payload['totals']['total']}"}

    def hashes(self, project_id):
        return {}


def _container(monkeypatch, parallel: list[dict], serial: list[dict], serial_rc: int = 0) -> dict:
    """A fake ada-tests: each pytest call's report is `parallel` or `serial`, told apart by the artifact directory
    the report is pulled into (`<art>/serial/` for the serial phase)."""
    seen: dict = {"sync": [], "reap": 0, "pytest": []}
    monkeypatch.setattr(runner, "ensure_container", lambda runtime, root: None)

    def reap(runtime, *a, **k):
        seen["reap"] += 1
        return 0

    def sync(project, runtime, out_log):
        seen["sync"].append(runtime["snapshot"].get("rev", "HEAD"))
        return "/snap"

    def run(cmd, timeout, out_file=None, cwd=None, env=None):
        if "pytest" in cmd:
            seen["pytest"].append(cmd)
            return (serial_rc if "-n0" in cmd else 0), ""
        return 0, ""

    def pull(container, remote, dest):
        if not str(remote).endswith(".json"):
            return 1
        tests = serial if dest.parent.name == "serial" else parallel
        dest.write_text(json.dumps({"tests": tests, "summary": {"collected": len(tests)}}), encoding="utf-8")
        return 0

    monkeypatch.setattr(runner, "reap_orphaned_heavy_pytest", reap)
    monkeypatch.setattr(runner, "sync_snapshot", sync)
    monkeypatch.setattr(runner, "_run", run)
    monkeypatch.setattr(runner, "pull_file", pull)
    return seen


def _run_tier(project: profile.Project, name: str, tmp_path: Path, legion: _Legion | None = None) -> runner.Execution:
    _, tier, paths = runner.resolve_target(project, name)
    art = tmp_path / "art"
    art.mkdir(exist_ok=True)
    return runner._execute_framework(project, tier, paths, "claude", art, legion or _Legion(), name, None, None)


def _arg_after(cmd: list[str], flag: str) -> str:
    """The value after `flag` among pytest's own arguments (the docker and timeout prefix carry -w, -k and -m too)."""
    own = cmd[cmd.index("pytest") + 1:]
    return own[own.index(flag) + 1]


def test_control_the_serial_phase_is_one_process_on_the_serial_marker_with_the_tiers_own_args(tmp_path):
    tier = _project(tmp_path).tier("bitcoin")
    serial = runner.serial_phase_tier(tier)
    assert serial["workers"] == 0 and serial["marker"] == "heavy"
    assert serial["args"] == ["-k", "bitcoin"] and serial["paths"] == ["tests"]
    assert serial["min_executed"] == 1 and serial["timeout_s"] == 30
    assert not set(serial) & {"serial_marker", "floors_file", "testmon_build", "mode"}
    assert runner.serial_phase_tier(_project(tmp_path).tier("plain")) is None


def test_a_whole_tier_run_runs_its_workers_then_one_serial_process_on_the_same_snapshot(tmp_path, monkeypatch):
    seen = _container(monkeypatch, [PARALLEL_CASE], [SERIAL_CASE])
    exe = _run_tier(_project(tmp_path), "bitcoin", tmp_path)
    assert exe.status == "passed", exe.error_summary
    assert {c["node_id"] for c in exe.cases} == {PARALLEL_CASE["nodeid"], SERIAL_CASE["nodeid"]}
    assert len(seen["sync"]) == 1 and seen["reap"] == 1, "the serial phase must reuse the parallel phase's snapshot"
    parallel, serial = seen["pytest"]
    assert _arg_after(parallel, "-n") == "4" and _arg_after(parallel, "-m") == "not heavy"
    assert "-n0" in serial and _arg_after(serial, "-m") == "heavy" and _arg_after(serial, "-k") == "bitcoin"
    assert serial[serial.index("-w") + 1] == "/snap" and "--rootdir=/snap" in serial
    assert (tmp_path / "art" / "serial" / "report.json").exists()


def test_sabotage_a_serial_phase_that_resyncs_the_snapshot_is_seen(tmp_path, monkeypatch):
    seen = _container(monkeypatch, [PARALLEL_CASE], [SERIAL_CASE])
    project = _project(tmp_path)
    tier = project.tier("bitcoin")
    art = tmp_path / "art"
    (art / "serial").mkdir(parents=True)
    runner.run_pytest(project, {k: v for k, v in tier.items() if k != "floors_file"}, None, "claude", art, _Legion(),
                      "bitcoin")
    runner.run_pytest(project, runner.serial_phase_tier(tier), None, "claude", art / "serial", _Legion(), "bitcoin")
    assert len(seen["sync"]) == 2 and seen["reap"] == 2


def test_control_a_tier_without_serial_marker_and_a_path_run_stay_one_invocation(tmp_path, monkeypatch):
    seen = _container(monkeypatch, [PARALLEL_CASE], [SERIAL_CASE])
    project = _project(tmp_path)
    assert _run_tier(project, "plain", tmp_path).status == "passed"
    assert len(seen["pytest"]) == 1
    art = tmp_path / "art"
    runner._execute_framework(project, project.tier("bitcoin"), ["tests/test_bitcoin_a.py"], "claude", art,
                              _Legion(), "bitcoin", None, None)
    assert len(seen["pytest"]) == 2, "a path run of a serial tier is still one invocation"


def test_sabotage_a_serial_phase_that_selects_nothing_fails_a_tier_that_requires_it(tmp_path, monkeypatch):
    _container(monkeypatch, [PARALLEL_CASE], [], serial_rc=5)
    exe = _run_tier(_project(tmp_path), "bitcoin", tmp_path)
    assert exe.status == "error"
    assert exe.error_summary.startswith("serial phase: executed 0 tests, tier requires at least 1")


def test_control_a_serial_phase_may_select_nothing_when_the_tier_does_not_require_it(tmp_path, monkeypatch):
    _container(monkeypatch, [PARALLEL_CASE], [], serial_rc=5)
    text = PROFILE.replace("    serial_min_executed: 1\n", "")
    assert _run_tier(_project(tmp_path, text), "bitcoin", tmp_path).status == "passed"


def test_control_floors_are_judged_on_both_phases_cases(tmp_path, monkeypatch):
    _container(monkeypatch, [PARALLEL_CASE], [SERIAL_CASE])
    exe = _run_tier(_project(tmp_path), "fast", tmp_path)
    assert exe.status == "passed", exe.error_summary


def test_sabotage_a_floor_no_phase_meets_is_still_a_breach(tmp_path, monkeypatch):
    _container(monkeypatch, [PARALLEL_CASE], [])
    exe = _run_tier(_project(tmp_path), "fast", tmp_path)
    assert exe.status == "error"
    assert exe.error_summary == "service floor breach: oracle: prefixes matched zero tests ['test_heavy']"


@pytest.mark.parametrize("first", [runner.Execution("timeout", [], "exceeded 60s"),
                                   runner.Execution("error", [], "could not build the clean-tree snapshot")])
def test_a_parallel_phase_that_timed_out_or_ran_nothing_ends_the_run(tmp_path, monkeypatch, first):
    project = _project(tmp_path)
    calls: list[dict] = []

    def fake_run_pytest(project, tier, *a, **k):
        calls.append(tier)
        return first

    monkeypatch.setattr(runner, "run_pytest", fake_run_pytest)
    assert _run_tier(project, "fast", tmp_path) is first
    assert len(calls) == 1 and calls[0]["workers"] == 4 and "floors_file" not in calls[0]


def test_merged_verdict_counts_the_quarantine_list_once_and_labels_the_serial_phase(tmp_path):
    project = _project(tmp_path)
    case = {"node_id": "tests/test_heavy.py::test_oracle", "status": "failed"}
    parallel = runner.Execution("passed", [], None, 3, 0)
    serial = runner.Execution("failed", [case], "1 failed", 3, 1)
    merged = runner.merge_phases(project, project.tier("bitcoin"), parallel, serial)
    assert merged.status == "failed" and merged.quarantined_deselected == 3
    assert merged.error_summary == "serial phase: 1 failed" and merged.rc == 1


def test_pin_snapshot_rev_pins_head_to_the_reported_sha_and_nothing_else(tmp_path):
    project = _project(tmp_path)
    head = "0123456789abcdef0123456789abcdef"
    pinned = runner.pin_snapshot_rev(project, f"{head}+1234567")
    assert pinned.profile["runtime"]["snapshot"]["rev"] == head
    assert "rev" not in project.profile["runtime"]["snapshot"], "the loaded profile is never mutated"
    assert runner.pin_snapshot_rev(project, "unknown") is project
    branch = _project(tmp_path, PROFILE.replace("{dest: /snap,", "{dest: /snap, rev: origin/master,"))
    assert runner.pin_snapshot_rev(branch, head) is branch
    bare = _project(tmp_path, PROFILE.replace("  snapshot: {dest: /snap, include: [tests]}\n", ""))
    assert runner.pin_snapshot_rev(bare, head) is bare


def test_start_and_run_snapshots_the_commit_it_reports(tmp_path, monkeypatch):
    head = "fedcba9876543210fedcba9876543210"
    seen = _container(monkeypatch, [PARALLEL_CASE], [SERIAL_CASE])
    monkeypatch.setattr(runner, "git_sha", lambda root: f"{head}+abcdef0")
    monkeypatch.setattr(runner, "drain_pending", lambda *a, **k: [])
    legion = _Legion()
    outcome = runner.start_and_run(_project(tmp_path), "bitcoin", "claude", legion, runner.NullLock(),
                                   artifacts_root=tmp_path / "artifacts")
    assert outcome.status == "passed"
    assert legion.created["git_sha"] == f"{head}+abcdef0"
    assert seen["sync"] == [head], "the snapshot must be the commit the verdict names, not HEAD at sync time"
    assert legion.posted[7]["totals"]["total"] == 2


@pytest.mark.parametrize("tier, message", [
    ("{framework: vitest, timeout_s: 60, serial_marker: heavy}", "serial_marker must be a non-empty"),
    ("{mode: changed, paths: [tests], timeout_s: 60, serial_marker: heavy}", "serial_marker must be a non-empty"),
    ("{paths: [tests], timeout_s: 60, serial_marker: '  '}", "serial_marker must be a non-empty"),
    ("{paths: [tests], timeout_s: 60, serial_min_executed: 1}", "sets serial_min_executed without a serial_marker"),
    ("{paths: [tests], timeout_s: 60, serial_marker: heavy, serial_min_executed: -1}", "must be an integer >= 0"),
    ("{paths: [tests], timeout_s: 60, serial_marker: heavy, serial_timeout_s: 0}", "must be an integer >= 1"),
    ("{paths: [tests], timeout_s: 60, serial_marker: heavy, serial_timeout_s: true}", "must be an integer >= 1"),
])
def test_sabotage_a_misplaced_or_malformed_serial_phase_is_refused_at_load(tmp_path, tier, message):
    text = PROFILE + f"  odd: {tier}\n"
    with pytest.raises(profile.ProfileError, match=message):
        _project(tmp_path, text)
