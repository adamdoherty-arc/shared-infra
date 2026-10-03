from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_lanes import _project  # noqa: E402
from tplib import profile, runner, server  # noqa: E402
from tplib.lock import HEAVY, ProjectLock, queue_snapshot  # noqa: E402

MAX_FAST_PER_TEST_TIMEOUT_S = 60


def _service(tmp_path: Path, monkeypatch, project, started: list[str]) -> server.RunnerService:
    monkeypatch.setattr(server, "load_project", lambda name: project)
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(server, "QUEUE_TIMEOUT_S", 6)

    def fake_run(proj, target, trigger, legion, lock, on_started=None, changed_paths=None, schedule_id=None):
        started.append(target)
        if on_started:
            on_started(900 + len(started), tmp_path)

    monkeypatch.setattr(runner, "start_and_run", fake_run)
    return server.RunnerService(legion=object(), lock_dir=tmp_path)


def test_second_heavy_request_for_a_running_target_attaches_instead_of_spawning(tmp_path, monkeypatch):
    """Would pass trivially if it only checked 'a run is active' for ANY target: the attach must be keyed on the
    SAME target, return the running run's id, and start nothing."""
    project = _project(tmp_path)
    started: list[str] = []
    service = _service(tmp_path, monkeypatch, project, started)
    holder = ProjectLock("demo", tmp_path)
    assert holder.acquire(runner.request_key("fast", None))[0]
    holder.set_run_id(41)
    code, body = service.submit({"project": "demo", "target": "fast", "trigger": "claude"})
    assert code == 409 and body["attached_run_id"] == 41 and body["accepted"] is False
    assert started == [], "an attach must never start a second heavy run"
    holder.release()


def test_heavy_request_for_a_different_target_queues_with_a_position_and_does_not_start(tmp_path, monkeypatch):
    project = _project(tmp_path)
    started: list[str] = []
    service = _service(tmp_path, monkeypatch, project, started)
    holder = ProjectLock("demo", tmp_path)
    assert holder.acquire(runner.request_key("fast", None))[0]
    holder.set_run_id(42)
    code, body = service.submit({"project": "demo", "target": "full", "trigger": "claude"})
    assert code == 202 and body["queued"] is True and body["run_id"] is None
    assert body["queue_position"] == 1 and body["waiting_for_run_id"] == 42 and body["lane"] == HEAVY
    code2, body2 = service.submit({"project": "demo", "target": "full", "trigger": "claude"})
    assert body2["duplicate"] is True and body2["queue_position"] == 1, "a repeat request must not take a second place"
    assert started == []
    assert [r["position"] for r in queue_snapshot(tmp_path)] == [1]
    holder.release()
    deadline = time.time() + 12
    while not started and time.time() < deadline:
        time.sleep(0.2)
    assert started == ["full"], "the queued request runs once the holder is done"


def test_queue_positions_count_every_waiter_in_arrival_order(tmp_path):
    project = _project(tmp_path)
    holder = ProjectLock("demo", tmp_path)
    assert holder.acquire(runner.request_key("fast", None))[0]
    holder.set_run_id(7)
    seen: list[tuple[str, int]] = []

    def wait(target: str) -> None:
        lock = runner.lock_for(project, target, None, tmp_path)
        lock.wait_acquire(runner.request_key(target, None), 4, poll=0.1,
                          on_wait=lambda pos, held: seen.append((target, pos)))

    first = threading.Thread(target=wait, args=("full",), daemon=True)
    first.start()
    time.sleep(0.4)
    second = threading.Thread(target=wait, args=("changed",), daemon=True)
    second.start()
    time.sleep(0.6)
    assert [r["position"] for r in queue_snapshot(tmp_path)] == [1, 2]
    assert ("full", 1) in seen and ("changed", 2) in seen
    holder.release()
    first.join(8)
    second.join(8)


def test_worker_budget_rejects_an_overcommitted_profile(tmp_path):
    ok = "lanes: {cpu_budget: 8}\n"
    assert _project(tmp_path, "    light_concurrency: 2\n", ok.replace("8", "8")) is not None
    over = {"tiers": {"fast": {"workers": 7, "framework": "pytest"}}, "framework": "pytest",
            "lanes": {"cpu_budget": 8}}
    assert profile.worker_budget_problem(over, 2) is not None
    over["tiers"]["fast"]["workers"] = 6
    assert profile.worker_budget_problem(over, 2) is None


def test_light_run_is_in_process_while_a_heavy_run_is_live(tmp_path):
    project = _project(tmp_path, "    light_concurrency: 2\n", "lanes: {cpu_budget: 8}\n")
    assert runner.light_worker_cap(project, tmp_path) == 4
    heavy = ProjectLock("demo", tmp_path)
    assert heavy.acquire("k")[0]
    assert runner.light_worker_cap(project, tmp_path) == 0
    heavy.release()


def test_shipped_ada_fast_tier_has_a_fail_fast_per_test_timeout_and_a_fitting_worker_budget():
    ada_root = Path(str(profile.load_registry(profile.PROJECTS_FILE)["projects"]["ada"]["root"]))
    pfile = ada_root / profile.PROFILE_NAME
    if not pfile.exists():
        pytest.skip(f"ADA checkout not present at {ada_root}")
    prof = yaml.safe_load(pfile.read_text(encoding="utf-8"))
    args = list(prof["pytest"]["common_args"]) + list(prof["tiers"]["fast"].get("args", []))
    timeouts = [int(a.split("=", 1)[1]) for a in args if a.startswith("--timeout=")]
    assert timeouts and timeouts[-1] <= MAX_FAST_PER_TEST_TIMEOUT_S, f"fast per-test timeout is {timeouts}"
    assert "--timeout-method=signal" in args, "the thread method kills the xdist worker and crashes the run"
    project = profile.load_project("ada")
    assert profile.worker_budget_problem(prof, project.light_concurrency) is None


def test_a_profile_whose_fast_tier_timeout_exceeds_the_cap_is_refused(tmp_path):
    base = {"framework": "pytest", "pytest": {"common_args": ["--timeout=420"]}, "tiers": {"fast": {"args": []}}}
    assert "exceeds" in (profile.fast_timeout_problem(base) or "")
    base["tiers"]["fast"]["args"] = ["--timeout=60"]
    assert profile.fast_timeout_problem(base) is None
    base["tiers"]["fast"]["args"] = ["--timeout=61"]
    assert profile.fast_timeout_problem(base) is not None
    assert profile.fast_timeout_problem({"framework": "pytest", "tiers": {"fast": {}}}) is None


MEM = {"framework": "pytest", "tiers": {"fast": {"workers": 4, "framework": "pytest"}},
       "lanes": {"mem_budget_mb": 10240, "worker_rss_mb": 1800, "light_rss_mb": 650, "parent_rss_mb": 450,
                 "headroom_mb": 500}}


def test_memory_budget_sizes_workers_by_ram_and_trusts_a_larger_measurement():
    """Would pass trivially if only the configured estimate were consulted: a measured worker peak larger than
    the estimate must flip a fitting profile to refused."""
    assert profile.memory_budget_problem(MEM, 2) is None
    assert profile.memory_budget_problem(MEM, 2, measured_mb=2300) is None
    refused = profile.memory_budget_problem(MEM, 2, measured_mb=2400)
    assert refused and "exceeds mem_budget_mb" in refused
    assert profile.memory_budget_problem({**MEM, "tiers": {"fast": {"workers": 6, "framework": "pytest"}}}, 2)
    assert profile.memory_budget_problem({"tiers": {}}, 2) is None


def test_light_runs_fit_beside_a_heavy_run_only_when_memory_is_left():
    assert profile.light_slots_while_heavy(MEM, 2, None) == 2
    tight = profile.light_slots_while_heavy(MEM, 2, measured_mb=2200)
    assert tight == 1, "a larger measured worker leaves room for fewer light runs"
    assert profile.light_slots_while_heavy(MEM, 2, measured_mb=2400) == 0
    assert profile.light_slots_while_heavy({"tiers": {}}, 2, None) == 2


def test_a_heavy_tier_that_declares_its_footprint_leaves_room_for_light_runs():
    """Sabotage/control for heavy_footprint_mb: the fuzz tier's declared 600 MB frees both light slots beside it,
    while a tier that declares nothing, an unknown tier and an unreadable key keep the widest-pytest estimate."""
    prof = {**MEM, "tiers": {**MEM["tiers"], "fuzz": {"framework": "schemathesis", "rss_mb": 600},
                             "gates": {"framework": "commands"}}}
    assert profile.light_slots_while_heavy(prof, 2, measured_mb=2400, heavy_tier="fuzz") == 2
    for tier in ("gates", "nope", None):
        assert profile.light_slots_while_heavy(prof, 2, measured_mb=2400, heavy_tier=tier) == 0, tier
    assert profile.heavy_footprint_mb(prof, 2400, "fuzz") == 600.0


def test_a_light_request_starts_beside_a_live_fuzz_run(tmp_path, monkeypatch):
    lanes = "lanes: {mem_budget_mb: 10240, worker_rss_mb: 2300, light_rss_mb: 650, parent_rss_mb: 450, headroom_mb: 500}"
    project = _project(tmp_path, "    light_concurrency: 2" + chr(10), lanes + chr(10))
    project.profile["tiers"]["fast"]["workers"] = 4
    project.profile["tiers"]["fuzz"] = {"framework": "schemathesis", "rss_mb": 600}
    monkeypatch.setattr(runner, "measured_worker_rss_mb", lambda name: None)
    heavy = ProjectLock("demo", tmp_path)
    assert heavy.acquire(runner.request_key("fuzz", None))[0]
    heavy.set_run_id(7)
    light = runner.lock_for(project, "tests/test_a.py", None, tmp_path)
    ok, holder = light.acquire(runner.request_key("tests/test_a.py", None))
    assert ok, f"a 600 MB fuzz run must not hold the light lane: {holder}"
    light.release()
    heavy.release()
    assert heavy.acquire(runner.request_key("fast", None))[0]
    blocked = runner.lock_for(project, "tests/test_b.py", None, tmp_path)
    assert not blocked.acquire(runner.request_key("tests/test_b.py", None))[0], "control: a fast run still blocks"
    heavy.release()


def test_light_request_queues_behind_a_live_heavy_run_when_no_memory_is_left(tmp_path, monkeypatch):
    lanes = "lanes: {mem_budget_mb: 10240, worker_rss_mb: 2300, light_rss_mb: 650, parent_rss_mb: 450, headroom_mb: 500}"
    project = _project(tmp_path, "    light_concurrency: 2" + chr(10), lanes + chr(10))
    project.profile["tiers"]["fast"]["workers"] = 4
    monkeypatch.setattr(runner, "measured_worker_rss_mb", lambda name: None)
    light = runner.lock_for(project, "tests/test_a.py", None, tmp_path)
    assert light.acquire(runner.request_key("tests/test_a.py", None))[0], "no heavy run, so the light run starts"
    light.release()
    heavy = ProjectLock("demo", tmp_path)
    assert heavy.acquire("heavy-key")[0]
    heavy.set_run_id(5)
    blocked = runner.lock_for(project, "tests/test_b.py", None, tmp_path)
    ok, holder = blocked.acquire(runner.request_key("tests/test_b.py", None))
    assert not ok and holder is not None and holder.run_id == 5, "it must wait behind the heavy run, not overlap it"
    heavy.release()
    again = runner.lock_for(project, "tests/test_b.py", None, tmp_path)
    assert again.acquire(runner.request_key("tests/test_b.py", None))[0]
    again.release()


def test_heavy_request_is_refused_when_the_recorded_peak_overcommits_memory(tmp_path, monkeypatch):
    lanes = "lanes: {mem_budget_mb: 10240, worker_rss_mb: 1800, light_rss_mb: 650, parent_rss_mb: 450, headroom_mb: 500}"
    project = _project(tmp_path, "    light_concurrency: 2" + chr(10), lanes + chr(10))
    project.profile["tiers"]["full"]["workers"] = 4
    monkeypatch.setattr(runner, "measured_worker_rss_mb", lambda name: 2600.0)
    monkeypatch.setattr(runner.sel, "preflight_changed", lambda *a, **k: None)
    with pytest.raises(runner.RunError, match="memory budget"):
        runner.preflight(project, "full", None)
    (project.root / "tests").mkdir(exist_ok=True)
    (project.root / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    runner.preflight(project, "tests/test_a.py", None)


def test_a_repeat_of_a_queued_heavy_request_is_detected_but_a_different_target_is_not(tmp_path):
    """Would pass trivially if queued_duplicate matched ANY live ticket instead of the same request key: a
    different tier must still queue, only the identical request is a duplicate."""
    project = _project(tmp_path)
    holder = ProjectLock("demo", tmp_path)
    assert holder.acquire(runner.request_key("fast", None))[0]
    holder.set_run_id(8)
    waiter = threading.Thread(
        target=lambda: runner.lock_for(project, "full", None, tmp_path).wait_acquire(
            runner.request_key("full", None), 4, poll=0.1), daemon=True)
    waiter.start()
    time.sleep(0.6)
    probe = runner.lock_for(project, "full", None, tmp_path)
    assert probe.queued_duplicate(runner.request_key("full", None)) == 1
    assert probe.queued_duplicate(runner.request_key("changed", None)) is None
    assert probe.queued_duplicate(None) is None
    holder.release()
    waiter.join(8)


def _queue_heavy(tmp_path: Path, project: str, tier: str, waited_s: float) -> None:
    queue = tmp_path / f"{project}.queue"
    queue.mkdir(parents=True, exist_ok=True)
    queued_at = time.time() - waited_s
    (queue / f"{int(queued_at * 1e9):020d}-1-0").write_text(json.dumps(
        {"pid": os.getpid(), "group": "waiter", "key": runner.request_key(tier, None), "queued_at": queued_at}),
        encoding="utf-8")


def _starvation_project(tmp_path: Path, monkeypatch):
    lanes = ("lanes: {mem_budget_mb: 10240, worker_rss_mb: 2300, light_rss_mb: 650, parent_rss_mb: 450, "
             "headroom_mb: 500, light_concurrency_max: 6}")
    project = _project(tmp_path, "    light_concurrency: 2" + chr(10), lanes + chr(10))
    project.profile["tiers"]["fast"]["workers"] = 4
    project.profile["tiers"]["e2e"] = {"framework": "playwright", "rss_mb": 0, "timeout_s": 60}
    monkeypatch.setattr(runner, "measured_worker_rss_mb", lambda name: None)
    return project


def test_sabotage_a_heavy_request_that_waited_too_long_stops_new_light_runs(tmp_path, monkeypatch):
    """2026-10-03: ADA's fast, e2e_bitcoin, e2e and bitcoin requests waited over an hour because one targeted run
    after another kept the light lane busy and a heavy run starts only once the lane drains."""
    project = _starvation_project(tmp_path, monkeypatch)
    _queue_heavy(tmp_path, "demo", "fast", runner.HEAVY_PRIORITY_AFTER_S + 1)
    blocker = runner.light_blocker(project, 0, tmp_path, available_mb=60000)
    assert blocker is not None and blocker.run_id is None and runner.held_tier(blocker) == "fast"


def test_control_a_fresh_heavy_request_or_one_with_room_beside_it_lets_light_runs_start(tmp_path, monkeypatch):
    project = _starvation_project(tmp_path, monkeypatch)
    _queue_heavy(tmp_path, "demo", "fast", 30)
    assert runner.light_blocker(project, 0, tmp_path, available_mb=60000) is None
    other = tmp_path / "other"
    other.mkdir()
    _queue_heavy(other, "demo", "e2e", runner.HEAVY_PRIORITY_AFTER_S + 1)
    assert runner.light_blocker(project, 1, other, available_mb=60000) is None


def test_a_heavy_tier_that_needs_no_container_memory_starts_beside_live_light_runs(tmp_path, monkeypatch):
    project = _starvation_project(tmp_path, monkeypatch)
    light = runner.lock_for(project, "tests/test_a.py", None, tmp_path)
    assert light.acquire(runner.request_key("tests/test_a.py", None))[0]
    e2e = runner.lock_for(project, "e2e", None, tmp_path)
    ok, holder = e2e.acquire(runner.request_key("e2e", None))
    assert ok, f"an e2e run on the host must not wait for the light lane: {holder}"
    e2e.release()
    fast = runner.lock_for(project, "fast", None, tmp_path)
    ok, holder = fast.acquire(runner.request_key("fast", None))
    assert not ok and holder is not None and holder.lane == "light", "control: fast still waits for the lane"
    light.release()
