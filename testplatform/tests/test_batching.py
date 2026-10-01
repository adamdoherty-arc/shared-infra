from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_lanes import _project  # noqa: E402
from tplib import profile, runner  # noqa: E402
from tplib.lock import LIGHT, ProjectLock  # noqa: E402

LANES = ("lanes: {light_concurrency: 2, light_concurrency_max: 6, mem_budget_mb: 10240, light_rss_mb: 650, "
         "cpu_budget: 8, parent_rss_mb: 450, worker_rss_mb: 1700}\n")


def _case(node_id: str, status: str = "passed") -> dict:
    return {"node_id": node_id, "file": node_id.split("::")[0], "status": status, "duration_ms": 1, "attempts": 1,
            "body_hash": "h", "feature_slug": None, "requirement_ids": []}


def test_idle_light_slots_scale_with_free_resources_and_never_reach_zero():
    prof = {"lanes": {"mem_budget_mb": 10240, "light_rss_mb": 650, "cpu_budget": 8, "parent_rss_mb": 450}}
    assert profile.light_slots_idle(prof, 2, 6, 20000) == 6
    assert profile.light_slots_idle(prof, 2, 6, None) == 6
    assert profile.light_slots_idle(prof, 2, 6, 2500) == 2
    assert profile.light_slots_idle(prof, 2, 6, 200) == 1
    assert profile.light_slots_idle(prof, 2, 2, 200) == 2
    tight = {"lanes": {"mem_budget_mb": 2000, "light_rss_mb": 650, "cpu_budget": 8, "parent_rss_mb": 450}}
    assert profile.light_slots_idle(tight, 2, 6, 20000) == 2
    cpu = {"lanes": {"mem_budget_mb": 10240, "light_rss_mb": 650, "cpu_budget": 3, "parent_rss_mb": 450}}
    assert profile.light_slots_idle(cpu, 2, 6, 20000) == 3


def test_light_lane_widens_when_idle(tmp_path, monkeypatch):
    project = _project(tmp_path, "", LANES)
    monkeypatch.setattr(runner, "host_available_mb", lambda: 20000.0)
    assert (project.light_concurrency, project.light_slots_max) == (2, 6)
    locks = [runner.lock_for(project, f"tests/test_{i}.py", None, tmp_path) for i in range(7)]
    assert [lk.acquire(f"k{i}")[0] for i, lk in enumerate(locks)] == [True] * 6 + [False]
    for lk in locks[:6]:
        lk.release()


def test_sabotage_a_profile_without_a_max_keeps_exactly_the_old_two_slots(tmp_path):
    project = _project(tmp_path, "    light_concurrency: 2\n")
    assert project.light_slots_max == 2
    locks = [runner.lock_for(project, f"tests/test_{i}.py", None, tmp_path) for i in range(3)]
    assert [lk.acquire(f"k{i}")[0] for i, lk in enumerate(locks)] == [True, True, False]
    for lk in locks[:2]:
        lk.release()


def test_host_memory_pressure_holds_the_lane_at_the_base(tmp_path, monkeypatch):
    project = _project(tmp_path, "", LANES)
    monkeypatch.setattr(runner, "host_available_mb", lambda: 2500.0)
    locks = [runner.lock_for(project, f"tests/test_{i}.py", None, tmp_path) for i in range(3)]
    assert [lk.acquire(f"k{i}")[0] for i, lk in enumerate(locks)] == [True, True, False]
    for lk in locks[:2]:
        lk.release()


def test_heavy_waits_until_the_widened_light_lane_drains_to_the_heavy_compatible_level(tmp_path, monkeypatch):
    project = _project(tmp_path, "", LANES)
    monkeypatch.setattr(runner, "host_available_mb", lambda: 20000.0)
    held = []
    for i in range(5):
        lock = runner.lock_for(project, f"tests/test_{i}.py", None, tmp_path)
        assert lock.acquire(f"k{i}")[0]
        held.append(lock)
    heavy = runner.lock_for(project, "full", None, tmp_path)
    ok, blocker = heavy.acquire("full")
    assert not ok and blocker is not None and blocker.lane == LIGHT
    for lock in held[:3]:
        lock.release()
    assert heavy.acquire("full")[0]
    heavy.release()
    for lock in held[3:]:
        lock.release()


def test_preflight_rejects_a_target_that_names_no_file_before_anything_queues(tmp_path):
    project = _project(tmp_path)
    (project.root / "tests").mkdir()
    (project.root / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    with pytest.raises(runner.RunError, match="names no existing file"):
        runner.preflight(project, "nope", None)
    with pytest.raises(runner.RunError, match="names no existing file"):
        runner.preflight(project, "tests/test_missing.py::T::t", None)
    runner.preflight(project, "tests/test_a.py", None)
    runner.preflight(project, "tests/test_a.py::test_a", None)


def test_batch_spec_only_for_plain_pytest_path_requests(tmp_path):
    project = _project(tmp_path)
    plain = runner.batch_spec(project, "tests/test_a.py", "claude", None)
    node = runner.batch_spec(project, "tests/test_a.py::T::t", "claude", None)
    assert plain["batch_class"] != node["batch_class"] and plain["targets"] == ["tests/test_a.py"]
    assert runner.batch_spec(project, "tests/test_a.py", "schedule", None)["batch_class"] != plain["batch_class"]
    assert runner.batch_spec(project, "fast", "claude", None) is None
    assert runner.batch_spec(project, "changed", "claude", ["a.py"]) is None
    assert runner.batch_spec(project, "frontend/src/X.test.tsx", "claude", None) is None


def test_split_execution_routes_each_case_to_the_member_that_asked_for_it():
    exe = runner.Execution("failed", [_case("tests/a.py::t1"), _case("tests/b.py::t2", "failed"),
                                      _case("tests/b.py::t3"), _case("tests/dir/c.py::t4")], None, 2, 1)
    a, b, d = runner.split_execution(exe, [["tests/a.py"], ["tests/b.py::t2", "tests/b.py::t3"], ["tests/dir"]])
    assert [c["node_id"] for c in a.cases] == ["tests/a.py::t1"] and a.status == "passed"
    assert b.status == "failed" and len(b.cases) == 2
    assert [c["node_id"] for c in d.cases] == ["tests/dir/c.py::t4"]
    assert a.quarantined_deselected == 2


def test_sabotage_a_shared_run_without_per_test_evidence_gives_every_member_a_retry():
    assert runner.split_execution(runner.Execution("timeout", [_case("tests/a.py::t")]), [["tests/a.py"]]) == [None]
    assert runner.split_execution(runner.Execution("error", [], "killed"), [["tests/a.py"], ["tests/b.py"]]) == [None, None]


def test_sibling_target_prefix_is_not_a_match():
    exe = runner.Execution("passed", [_case("tests/test_ab.py::t"), _case("tests/test_a.py::t")])
    (only,) = runner.split_execution(exe, [["tests/test_a.py"]])
    assert [c["node_id"] for c in only.cases] == ["tests/test_a.py::t"]


class _Legion:
    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.results: dict[int, dict] = {}
        self._lock = threading.Lock()

    def create_run(self, body: dict) -> int:
        with self._lock:
            self.runs.append(body)
            return 500 + len(self.runs)

    def post_results(self, run_id: int, payload: dict) -> dict:
        self.results[run_id] = payload
        return {"text": f"{payload['status'].upper()} {payload['totals']['total']} tests"}

    def hashes(self, project_id: int) -> dict:
        return {}

    def quarantine(self, project_id: int) -> list:
        return []


def test_queued_requests_share_one_pytest_run_and_each_gets_its_own_run_row(tmp_path, monkeypatch):
    project = _project(tmp_path, "", LANES)
    (project.root / "tests").mkdir(exist_ok=True)
    for name in ("test_a.py", "test_b.py", "test_c.py"):
        (project.root / "tests" / name).write_text("def test_x():\n    pass\n", encoding="utf-8")
    executed: list[list[str]] = []

    def fake_execute(proj, tier, paths, trigger, art, legion, target, changed_paths=None):
        executed.append(list(paths))
        time.sleep(0.4)
        return runner.Execution("passed", [_case(f"{p}::test_x") for p in paths], None, 0, 0)

    monkeypatch.setattr(runner, "execute_framework", fake_execute)
    monkeypatch.setattr(runner, "ARTIFACTS_ROOT", tmp_path / "art")
    monkeypatch.setattr(runner, "drain_pending", lambda *a, **k: [])
    monkeypatch.setattr(runner, "git_sha", lambda root: "sha")
    monkeypatch.setattr(runner, "host_available_mb", lambda: 2500.0)
    legion = _Legion()
    pads = []
    for i in range(2):
        pad = runner.lock_for(project, f"tests/pad{i}.py", None, tmp_path)
        assert pad.acquire(f"pad{i}")[0]
        pads.append(pad)
    outcomes: dict[str, runner.Outcome | None] = {}

    def request(name: str) -> None:
        target = f"tests/{name}"
        lock = runner.lock_for(project, target, None, tmp_path)
        outcomes[name] = runner.wait_and_run(project, target, "claude", legion, lock,
                                             runner.request_key(target, None), None, 30,
                                             artifacts_root=tmp_path / "art")

    threads = [threading.Thread(target=request, args=(n,)) for n in ("test_a.py", "test_b.py", "test_c.py")]
    for t in threads:
        t.start()
    deadline = time.time() + 10
    while len(list((tmp_path / "demo.light.queue").glob("*"))) < 3 and time.time() < deadline:
        time.sleep(0.05)
    pads[0].release()
    for t in threads:
        t.join(40)
    pads[1].release()
    shown = {n: (o.status, o.text[:300]) if o else None for n, o in outcomes.items()}
    assert all(o is not None and o.status == "passed" for o in outcomes.values()), shown
    assert len(executed) == 1 and sorted(executed[0]) == ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
    assert len(legion.runs) == 3 and len({o.run_id for o in outcomes.values()}) == 3
    for name, outcome in outcomes.items():
        totals = legion.results[outcome.run_id]["totals"]
        assert totals["total"] == 1, f"{name} must see only its own test, got {totals}"
    assert not list((tmp_path / "demo.light.claimed").glob("*"))
    assert not list((tmp_path / "demo.light.handoff").glob("*"))


def test_sabotage_a_retry_handoff_sends_the_claimed_waiter_back_to_the_queue(tmp_path):
    leader = ProjectLock("demo", tmp_path, lane=LIGHT, slots=1)
    peer = ProjectLock("demo", tmp_path, lane=LIGHT, slots=1)
    spec = {"batch_class": "path|claude|file", "targets": ["tests/a.py"]}
    assert leader.acquire("lead")[0]
    box: dict = {}

    def wait() -> None:
        box["res"] = peer.wait_slot_or_handoff("peer", 20, poll=0.1, spec=spec)

    t = threading.Thread(target=wait)
    t.start()
    deadline = time.time() + 5
    while not list((tmp_path / "demo.light.queue").glob("*")) and time.time() < deadline:
        time.sleep(0.05)
    claimed = leader.claim_peers("path|claude|file", 4, 10)
    assert len(claimed) == 1
    leader.deliver(claimed[0]["ticket"], {"retry": True, "reason": "leader failed before delivering"})
    t.join(10)
    leader.release()
    assert box["res"][0] == "handoff" and box["res"][1]["retry"] is True


def test_claim_ignores_other_classes_and_specless_waiters(tmp_path):
    leader = ProjectLock("demo", tmp_path, lane=LIGHT, slots=1)
    assert leader.acquire("lead")[0]
    qdir = tmp_path / "demo.light.queue"
    qdir.mkdir(exist_ok=True)
    base = {"pid": os.getpid(), "group": "g", "key": "k", "queued_at": time.time()}
    (qdir / "001").write_text(json.dumps({**base, "spec": None}), encoding="utf-8")
    (qdir / "002").write_text(json.dumps({**base, "spec": {"batch_class": "path|claude|node", "targets": ["t::x"]}}),
                              encoding="utf-8")
    (qdir / "003").write_text(json.dumps({**base, "spec": {"batch_class": "path|claude|file", "targets": ["a.py"]}}),
                              encoding="utf-8")
    got = leader.claim_peers("path|claude|file", 4, 10)
    leader.release()
    assert [g["ticket"] for g in got] == ["003"]
    assert (qdir / "001").exists() and (qdir / "002").exists() and not (qdir / "003").exists()


def test_release_survives_a_sharing_violation_instead_of_leaving_the_slot_held(tmp_path, monkeypatch):
    lock = ProjectLock("demo", tmp_path, lane=LIGHT, slots=1)
    assert lock.acquire("k")[0]
    slot = lock.path
    real = Path.unlink
    refusals = {"left": 3}

    def flaky_unlink(self, *args, **kwargs):
        if self == slot and refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(32, "sharing violation")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    lock.release()
    assert not slot.exists() and refusals["left"] == 0
    assert ProjectLock("demo", tmp_path, lane=LIGHT, slots=1).acquire("other")[0]
