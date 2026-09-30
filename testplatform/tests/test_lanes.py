from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import profile, runner, server  # noqa: E402
from tplib.lock import HEAVY, LIGHT, ProjectLock, split_stem  # noqa: E402

PROFILE = """
framework: pytest
default_target: fast
path_tier: path
vitest_path_tier: vitest
runtime: {kind: exec, container: c}
tiers:
  fast: {paths: [tests], timeout_s: 60}
  changed: {mode: changed, paths: [tests], timeout_s: 60, testmon_datafile: /tmp/x/.testmondata, test_globs: ["tests/**/test_*.py"]}
  full: {paths: [tests], timeout_s: 60}
  path: {timeout_s: 30}
  vitest: {framework: vitest, timeout_s: 60}
"""


def _project(tmp_path: Path, light: str = "", profile_extra: str = "") -> profile.Project:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".testplatform.yml").write_text(PROFILE + profile_extra, encoding="utf-8")
    reg = tmp_path / "projects.yml"
    reg.write_text(f"projects:\n  demo:\n    root: {repo.as_posix()}\n    legion_project_id: 9\n{light}", encoding="utf-8")
    return profile.load_project("demo", reg)


def test_lane_classification(tmp_path):
    project = _project(tmp_path)
    for heavy in (None, "fast", "full", "vitest", "changed"):
        assert runner.lane_of(project, heavy) == HEAVY
    assert runner.lane_of(project, "tests/test_a.py") == LIGHT
    assert runner.lane_of(project, "tests/test_a.py::T::t") == LIGHT
    assert runner.lane_of(project, "frontend/src/X.test.tsx") == LIGHT
    assert runner.lane_of(project, "changed", ["a.py"]) == LIGHT
    assert runner.lane_of(project, "fast", None) == HEAVY


def test_light_concurrency_config_precedence_and_validation(tmp_path):
    assert _project(tmp_path).light_concurrency == 1
    assert _project(tmp_path, "    light_concurrency: 3\n").light_concurrency == 3
    assert _project(tmp_path, "    light_concurrency: 3\n", "lanes: {light_concurrency: 2}\n").light_concurrency == 2
    for bad in ("0", "99", "true", '"2"'):
        with pytest.raises(profile.ProfileError):
            _project(tmp_path, f"    light_concurrency: {bad}\n")
    with pytest.raises(profile.ProfileError):
        _project(tmp_path, "", "lanes: {light_concurrency: 0}\n")


def test_shipped_ada_profile_gets_six_light_slots():
    ada_root = Path(str(profile.load_registry(profile.PROJECTS_FILE)["projects"]["ada"]["root"]))
    if not (ada_root / profile.PROFILE_NAME).exists():
        pytest.skip(f"ADA checkout not present at {ada_root} (containerised run)")
    project = profile.load_project("ada")
    assert project.light_concurrency == 6


def _can_start(project: profile.Project, target: str, lock_dir: Path, paths=None):
    """(started, lock): would a request for `target` start immediately, given the locks already held?"""
    lock = runner.lock_for(project, target, paths, lock_dir)
    ok, _ = lock.acquire(runner.request_key(target, paths))
    return ok, lock


def test_light_run_proceeds_while_heavy_holds(tmp_path):
    project = _project(tmp_path, "    light_concurrency: 3\n")
    ok, heavy = _can_start(project, "full", tmp_path)
    assert ok
    heavy.set_run_id(1)
    ok2, light = _can_start(project, "tests/test_a.py", tmp_path)
    assert ok2, "a light request must not queue behind a whole-suite run"
    ok3, other_heavy = _can_start(project, "fast", tmp_path)
    assert not ok3, "the heavy lane stays exclusive"
    assert other_heavy.holders()[0].run_id == 1
    heavy.release()
    light.release()


def test_sabotage_single_lane_makes_light_wait_behind_heavy(tmp_path, monkeypatch):
    """Sabotage: classifying every request as heavy is exactly the old one-lock behaviour; the pair above must fail."""
    monkeypatch.setattr(runner, "lane_of", lambda *a, **k: HEAVY)
    project = _project(tmp_path, "    light_concurrency: 3\n")
    ok, heavy = _can_start(project, "full", tmp_path)
    assert ok
    ok2, _ = _can_start(project, "tests/test_a.py", tmp_path)
    assert not ok2
    heavy.release()


def test_heavy_lane_is_exclusive_and_does_not_block_on_light(tmp_path):
    project = _project(tmp_path, "    light_concurrency: 2\n")
    slots = [_can_start(project, f"tests/test_{i}.py", tmp_path) for i in range(2)]
    assert all(ok for ok, _ in slots)
    ok, heavy = _can_start(project, "full", tmp_path)
    assert ok, "light runs must not block a heavy run either"
    ok2, _ = _can_start(project, "fast", tmp_path)
    assert not ok2
    heavy.release()
    for _, lock in slots:
        lock.release()


def test_light_cap_is_honoured_and_a_freed_slot_is_reusable(tmp_path):
    project = _project(tmp_path, "    light_concurrency: 2\n")
    a = _can_start(project, "tests/test_a.py", tmp_path)
    b = _can_start(project, "tests/test_b.py", tmp_path)
    c_ok, c = _can_start(project, "tests/test_c.py", tmp_path)
    assert a[0] and b[0] and not c_ok
    assert len(c.holders()) == 2
    a[1].release()
    assert c.acquire(runner.request_key("tests/test_c.py", None))[0]
    for lock in (b[1], c):
        lock.release()
    assert not list(tmp_path.glob("demo.light*.json"))


def test_sabotage_unbounded_slots_breaks_the_cap(tmp_path):
    """Sabotage: a lane with many more slots than configured admits a third run; the cap test's assertion must not hold."""
    locks = [ProjectLock("demo", tmp_path, lane=LIGHT, slots=50) for _ in range(3)]
    assert [lk.acquire(f"k{i}")[0] for i, lk in enumerate(locks)] == [True, True, True]
    for lk in locks:
        lk.release()


def test_same_spec_attaches_in_the_light_lane_without_taking_a_slot(tmp_path):
    project = _project(tmp_path, "    light_concurrency: 3\n")
    ok, first = _can_start(project, "tests/test_a.py", tmp_path)
    assert ok
    first.set_run_id(41)
    key = runner.request_key("tests/test_a.py", None)
    second = runner.lock_for(project, "tests/test_a.py", None, tmp_path)
    ok2, held = second.acquire(key)
    assert not ok2 and held.key == key and held.run_id == 41
    assert second.wait_for_run_id(1.0).run_id == 41
    assert len(second.holders()) == 1
    other_ok, other = _can_start(project, "tests/test_b.py", tmp_path)
    assert other_ok and len(other.holders()) == 2
    first.release()
    other.release()


def test_same_spec_attaches_to_heavy_run_too(tmp_path):
    project = _project(tmp_path)
    ok, first = _can_start(project, "fast", tmp_path)
    first.set_run_id(5)
    second = runner.lock_for(project, "fast", None, tmp_path)
    ok2, held = second.acquire(runner.request_key("fast", None))
    assert ok and not ok2 and held.run_id == 5 and held.key == runner.request_key("fast", None)
    first.release()


def test_concurrent_identical_light_requests_claim_exactly_one_slot(tmp_path):
    project = _project(tmp_path, "    light_concurrency: 4\n")
    key = runner.request_key("tests/test_a.py", None)
    results: list[bool] = []
    locks = [runner.lock_for(project, "tests/test_a.py", None, tmp_path) for _ in range(12)]
    barrier = threading.Barrier(len(locks))

    def go(lock: ProjectLock) -> None:
        barrier.wait()
        results.append(lock.acquire(key)[0])

    threads = [threading.Thread(target=go, args=(lk,)) for lk in locks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert results.count(True) == 1
    assert len(list(tmp_path.glob("demo.light*.json"))) == 1
    for lk in locks:
        lk.release()


def test_concurrent_distinct_light_requests_never_exceed_the_cap(tmp_path):
    project = _project(tmp_path, "    light_concurrency: 3\n")
    locks = [runner.lock_for(project, f"tests/test_{i}.py", None, tmp_path) for i in range(10)]
    barrier = threading.Barrier(len(locks))
    results: list[bool] = []

    def go(i: int) -> None:
        barrier.wait()
        results.append(locks[i].acquire(runner.request_key(f"tests/test_{i}.py", None))[0])

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(locks))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert results.count(True) == 3
    for lk in locks:
        lk.release()


def test_dead_light_holder_is_taken_over_and_gate_is_released(tmp_path):
    (tmp_path / "demo.light0.json").write_text(json.dumps({"pid": 999999, "run_id": 8, "key": "x"}), encoding="utf-8")
    lock = ProjectLock("demo", tmp_path, lane=LIGHT, slots=2)
    assert lock.acquire("k")[0]
    assert not list(tmp_path.glob("*.gate"))
    lock.release()


def test_light_queue_is_fifo_and_separate_from_the_heavy_queue(tmp_path):
    holders = [ProjectLock("p", tmp_path, lane=LIGHT, slots=1) for _ in range(1)]
    assert holders[0].acquire("h")[0]
    order: list[str] = []

    def waiter(name: str) -> None:
        lock = ProjectLock("p", tmp_path, lane=LIGHT, slots=1)
        assert lock.wait_acquire(name, timeout=20, poll=0.05)
        order.append(name)
        time.sleep(0.05)
        lock.release()

    threads = []
    for name in ("first", "second"):
        t = threading.Thread(target=waiter, args=(name,))
        t.start()
        threads.append(t)
        time.sleep(0.15)
    assert (tmp_path / "p.light.queue").is_dir() and not (tmp_path / "p.queue").exists()
    holders[0].release()
    for t in threads:
        t.join(30)
    assert order == ["first", "second"]


def test_detached_wait_finds_own_slot_and_reports_queue_only_when_the_lane_is_full(tmp_path):
    lane = ProjectLock("p", tmp_path, lane=LIGHT, slots=2)
    holder = ProjectLock("p", tmp_path, lane=LIGHT, slots=2)
    assert holder.acquire("mine")[0]
    holder.set_run_id(3)
    assert lane.wait_for_run_id(1.0, "mine").run_id == 3
    busy = ProjectLock("p", tmp_path, lane=LIGHT, slots=2)
    assert busy.acquire("other")[0]
    busy.set_run_id(4)
    queued_view = ProjectLock("p", tmp_path, lane=LIGHT, slots=2).wait_for_run_id(1.0, "third")
    assert queued_view is not None and queued_view.key != "third"
    holder.release()
    busy.release()


def test_split_stem_and_active_runs_report_lanes(tmp_path):
    assert split_stem("ada") == ("ada", HEAVY)
    assert split_stem("ada.light2") == ("ada", LIGHT)
    (tmp_path / "ada.json").write_text(json.dumps({"pid": os.getpid(), "run_id": 1}), encoding="utf-8")
    (tmp_path / "ada.light1.json").write_text(json.dumps({"pid": os.getpid(), "run_id": 2}), encoding="utf-8")
    rows = {(r["project"], r["lane"], r["run_id"]) for r in server.active_runs(tmp_path)}
    assert rows == {("ada", HEAVY, 1), ("ada", LIGHT, 2)}


def test_service_starts_a_light_request_while_heavy_holds_and_queues_a_second_heavy(tmp_path, monkeypatch):
    project = _project(tmp_path, "    light_concurrency: 2\n")
    monkeypatch.setattr(server, "load_project", lambda name: project)
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)
    started: list[str] = []

    def fake_run(proj, target, trigger, legion, lock, on_started=None, changed_paths=None, schedule_id=None):
        started.append(target)
        if on_started:
            on_started(11, tmp_path)
        time.sleep(0.2)

    monkeypatch.setattr(runner, "start_and_run", fake_run)
    queued: list[tuple] = []
    real_thread = threading.Thread
    monkeypatch.setattr(server.threading, "Thread", lambda target, args=(), daemon=True, name="": (
        SimpleNamespace(start=lambda: queued.append(args)) if name.startswith("queued") else real_thread(
            target=target, args=args, daemon=daemon, name=name)))
    heavy = ProjectLock("demo", tmp_path)
    assert heavy.acquire(runner.request_key("full", None))[0]
    service = server.RunnerService(legion=object(), lock_dir=tmp_path)
    code, body = service.submit({"project": "demo", "target": "tests/test_a.py", "trigger": "claude"})
    assert code == 202 and body["run_id"] == 11 and "queued" not in body
    code2, body2 = service.submit({"project": "demo", "target": "fast", "trigger": "claude"})
    assert code2 == 202 and body2.get("queued") is True and len(queued) == 1
    heavy.release()
    time.sleep(0.4)


class _StubLegion:
    def __init__(self) -> None:
        self._ids = itertools.count(100)
        self.posted: dict[int, dict] = {}
        self.lock = threading.Lock()

    def create_run(self, body):
        with self.lock:
            return next(self._ids)

    def post_results(self, run_id, payload):
        with self.lock:
            self.posted[run_id] = payload
        return {"text": f"verdict for {run_id}: {payload['status']} {payload['totals']['total']}"}

    def hashes(self, project_id):
        return {}

    def quarantine(self, project_id):
        return []


def test_concurrent_light_runs_keep_run_ids_artifacts_and_verdicts_separate(tmp_path, monkeypatch):
    project = _project(tmp_path, "    light_concurrency: 3\n")
    legion = _StubLegion()
    gate = threading.Barrier(3)

    def fake_execute(proj, tier, paths, trigger, art, leg, target, changed_paths=None):
        gate.wait(timeout=10)
        case = {"node_id": f"{paths[0]}::t", "file": paths[0], "status": "passed", "duration_ms": 1, "attempts": 1,
                "body_hash": "h", "feature_slug": None, "requirement_ids": []}
        return runner.Execution("passed", [case], None, 0, 0)

    monkeypatch.setattr(runner, "execute_framework", fake_execute)
    monkeypatch.setattr(runner, "git_sha", lambda root: "abc")
    outcomes: dict[str, runner.Outcome] = {}
    lock_dir = tmp_path / "locks"

    def go(name: str) -> None:
        lock = runner.lock_for(project, name, None, lock_dir)
        ok, _ = lock.acquire(runner.request_key(name, None))
        assert ok
        try:
            outcomes[name] = runner.start_and_run(project, name, "claude", legion, lock, artifacts_root=tmp_path / "art")
        finally:
            lock.release()

    names = [f"tests/test_{c}.py" for c in "abc"]
    threads = [threading.Thread(target=go, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(outcomes) == 3
    assert len({o.run_id for o in outcomes.values()}) == 3
    assert len({o.artifact_dir for o in outcomes.values()}) == 3
    for name, out in outcomes.items():
        assert out.status == "passed" and str(out.run_id) in out.text
        assert (out.artifact_dir / "verdict.txt").read_text(encoding="utf-8") == out.text
        assert legion.posted[out.run_id]["cases"][0]["node_id"] == f"{name}::t"


def _capture_pytest_cmd(project: profile.Project, target: str, paths, tmp_path: Path, monkeypatch) -> list[str]:
    seen: list[list[str]] = []
    monkeypatch.setattr(runner, "ensure_container", lambda runtime, root: None)
    monkeypatch.setattr(runner, "pull_file", lambda *a, **k: 1)

    def fake_run(cmd, timeout, out_file=None, cwd=None, env=None):
        seen.append(cmd)
        return 0, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    tier_name, tier, resolved = runner.resolve_target(project, target)
    art = tmp_path / "art"
    art.mkdir(exist_ok=True)
    runner.run_pytest(project, tier, resolved, "claude", art, _StubLegion(), target, paths)
    return next(c for c in seen if "pytest" in c)


def test_light_pytest_never_loads_the_testmon_plugin_but_heavy_tiers_are_untouched(tmp_path, monkeypatch):
    project = _project(tmp_path, "    light_concurrency: 3\n")
    path_cmd = _capture_pytest_cmd(project, "tests/test_a.py", None, tmp_path, monkeypatch)
    assert "no:testmon" in path_cmd
    heavy_cmd = _capture_pytest_cmd(project, "fast", None, tmp_path, monkeypatch)
    assert "no:testmon" not in heavy_cmd


def test_report_files_are_unique_per_run(tmp_path, monkeypatch):
    project = _project(tmp_path)
    a = _capture_pytest_cmd(project, "tests/test_a.py", None, tmp_path, monkeypatch)
    b = _capture_pytest_cmd(project, "tests/test_a.py", None, tmp_path, monkeypatch)
    pick = lambda cmd: next(x for x in cmd if x.startswith("--json-report-file="))  # noqa: E731
    assert pick(a) != pick(b)


def test_small_named_selection_runs_in_process_not_xdist():
    """A handful of files must not spawn xdist workers (each re-imports the backend; contention kills them in a loop)."""
    from tplib import runner
    few = [f"backend/tests/test_{i}.py" for i in range(10)]
    many = [f"backend/tests/test_{i}.py" for i in range(200)]
    assert runner.effective_workers(2, few, runner.DEFAULT_PARALLEL_MIN_FILES) == 0
    assert runner.effective_workers(2, many, runner.DEFAULT_PARALLEL_MIN_FILES) == 2
    assert runner.effective_workers(4, None, runner.DEFAULT_PARALLEL_MIN_FILES) == 4
    assert runner.effective_workers(4, few, 0) == 4
    assert runner.effective_workers(0, many, 150) == 0


def _fair_setup(tmp_path, holder_group):
    lock = ProjectLock("p", tmp_path, lane=LIGHT, slots=1)
    (tmp_path / "p.light0.json").write_text(
        json.dumps({"pid": os.getpid(), "run_id": 1, "key": "h", "group": holder_group}), encoding="utf-8")
    queue = tmp_path / "p.light.queue"
    queue.mkdir()
    older = queue / "00000000000000000001-a"
    newer = queue / "00000000000000000002-b"
    older.write_text(json.dumps({"pid": os.getpid(), "group": "sweep"}), encoding="utf-8")
    newer.write_text(json.dumps({"pid": os.getpid(), "group": "other"}), encoding="utf-8")
    return lock, queue, older, newer


def test_group_holding_a_slot_yields_to_a_waiter_from_another_group(tmp_path):
    lock, queue, older, newer = _fair_setup(tmp_path, holder_group="sweep")
    assert lock._is_next(queue, newer)
    assert not lock._is_next(queue, older)


def test_fifo_holds_when_no_waiting_group_holds_a_slot(tmp_path):
    lock, queue, older, newer = _fair_setup(tmp_path, holder_group="someone-else")
    assert lock._is_next(queue, older)
    assert not lock._is_next(queue, newer)


def test_legacy_plain_pid_ticket_still_parses(tmp_path):
    lock = ProjectLock("p", tmp_path, lane=LIGHT, slots=1)
    queue = tmp_path / "p.light.queue"
    queue.mkdir()
    t = queue / "00000000000000000001-a"
    t.write_text(str(os.getpid()), encoding="utf-8")
    assert lock._is_next(queue, t)
