"""Sabotage + control tests for scripts/ops_selfcheck.py and scripts/ops_reconcile.py
(Legion sprint 15209). A watcher that never fails is indistinguishable from a broken one, so
every failing-input test has a passing control beside it."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


selfcheck = _load("ops_selfcheck")
reconcile = _load("ops_reconcile")


def _cp(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout, "")


def test_hostcron_two_consecutive_failures_fail_and_recovery_passes(tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}))
    monkeypatch.setattr(selfcheck, "RUNS", runs)
    monkeypatch.setattr(selfcheck, "HEARTBEAT", hb)
    (tmp_path / "hostcron").mkdir()
    (tmp_path / "hostcron" / "schedule.json").write_text(json.dumps({"jobs": [{"name": "j"}]}))
    monkeypatch.setattr(selfcheck, "HERE", tmp_path)

    def rows(*statuses):
        runs.write_text("\n".join(json.dumps({"job": "j", "status": s}) for s in statuses))

    rows("ok", "failed", "timeout")
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_hostcron(fails, metrics)
    assert any("2 consecutive failures" in f for f in fails)
    assert metrics['ops_hostcron_consecutive_failures{hostcron_job="j"}'] == 2

    rows("failed", "failed", "ok")  # control: a job that recovered must NOT be flagged
    fails, metrics = [], {}
    selfcheck.check_hostcron(fails, metrics)
    assert not [f for f in fails if "consecutive" in f]


def test_exposure_flags_unlisted_docker_port_and_passes_allowlisted(monkeypatch):
    docker_out = "qdrant-x\t0.0.0.0:6333->6333/tcp, [::]:6333->6333/tcp\nada-backend\t0.0.0.0:8006->8003/tcp\n"

    def fake_sh(cmd, timeout=60):
        return _cp(docker_out) if cmd[0] == "docker" else _cp("")

    monkeypatch.setattr(selfcheck, "sh", fake_sh)
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_exposure(fails, metrics)
    assert metrics["ops_unexpected_exposed_ports"] == 1 and "qdrant-x:6333" in fails[0]

    monkeypatch.setattr(selfcheck, "sh", lambda cmd, timeout=60: _cp(
        "ada-backend\t0.0.0.0:8006->8003/tcp\n" if cmd[0] == "docker" else ""))
    fails, metrics = [], {}
    selfcheck.check_exposure(fails, metrics)  # control: only allowlisted ports -> clean
    assert metrics["ops_unexpected_exposed_ports"] == 0 and not fails


def test_offvolume_missing_manifest_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(selfcheck, "OFFVOLUME", tmp_path / "nope.json")
    fails: list[str] = []
    selfcheck.check_offvolume(fails)
    assert fails and "unreadable" in fails[0]


def test_reconcile_refuses_when_nothing_is_running(monkeypatch, capsys):
    """Sabotage: a hung docker returning an empty list must not read as 'everything is absent'."""
    calls: list[list[str]] = []

    def fake_sh(cmd, timeout=60, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["docker", "info"]:
            return _cp("27.0")
        return _cp("")  # empty docker ps

    monkeypatch.setattr(reconcile, "sh", fake_sh)
    monkeypatch.setattr(sys, "argv", ["ops_reconcile.py"])
    assert reconcile.main() == 1
    assert not any("compose" in c for c in calls)


def test_reconcile_heals_absent_container_with_single_service_up(monkeypatch, tmp_path):
    """Control: with the rest of the estate running, an absent must_run container is brought
    back with exactly `up -d --no-deps <service>` and nothing else."""
    must = reconcile.EXPECTED["must_run"]
    target = "legion-db-backup"
    ps_out = "\n".join(f"{n}\trunning" for n in must if n != target)
    calls: list[list[str]] = []

    def fake_sh(cmd, timeout=60, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["docker", "info"]:
            return _cp("27.0")
        if cmd[:2] == ["docker", "ps"]:
            return _cp(ps_out)
        return _cp("")

    monkeypatch.setattr(reconcile, "sh", fake_sh)
    monkeypatch.setattr(reconcile, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(reconcile, "PAUSE", tmp_path / "no-pause")
    monkeypatch.setattr(reconcile, "notify", lambda m: None)
    monkeypatch.setattr(sys, "argv", ["ops_reconcile.py"])
    assert reconcile.main() == 0
    ups = [c for c in calls if "compose" in c]
    assert len(ups) == 1 and ups[0][-4:] == ["up", "-d", "--no-deps", "legion-db-backup"]
    assert "down" not in ups[0] and "--force-recreate" not in ups[0]


def test_reconcile_pause_switch_blocks_healing(monkeypatch, tmp_path):
    must = reconcile.EXPECTED["must_run"]
    ps_out = "\n".join(f"{n}\trunning" for n in must if n != "legion-db-backup")
    calls: list[list[str]] = []

    def fake_sh(cmd, timeout=60, cwd=None):
        calls.append(cmd)
        return _cp("27.0" if cmd[:2] == ["docker", "info"] else ps_out)

    pause = tmp_path / "pause"
    pause.write_text("hold")
    monkeypatch.setattr(reconcile, "sh", fake_sh)
    monkeypatch.setattr(reconcile, "STATE", tmp_path / "s.json")
    monkeypatch.setattr(reconcile, "PAUSE", pause)
    monkeypatch.setattr(sys, "argv", ["ops_reconcile.py"])
    reconcile.main()
    assert not any("compose" in c for c in calls)


def test_default_pg_password_accepted_is_a_failure_and_refused_is_a_pass(monkeypatch):
    monkeypatch.setattr(selfcheck, "sh", lambda cmd, timeout=60: _cp("1\n", 0))
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_default_pg_password(fails, metrics)  # sabotage: default password works
    assert metrics["ops_pg_default_password_accepted"] == 1 and fails

    monkeypatch.setattr(selfcheck, "sh", lambda cmd, timeout=60: _cp(
        'psql: error: connection to server failed: FATAL:  password authentication failed for user "postgres"', 2))
    fails, metrics = [], {}
    selfcheck.check_default_pg_password(fails, metrics)  # control: refused -> clean
    assert metrics["ops_pg_default_password_accepted"] == 0 and not fails

    monkeypatch.setattr(selfcheck, "sh", lambda cmd, timeout=60: _cp("Error: No such container", 1))
    fails, metrics = [], {}
    selfcheck.check_default_pg_password(fails, metrics)  # unreachable is inconclusive, never a silent pass
    assert fails and "inconclusive" in fails[0]


def test_retired_job_failures_are_ignored(tmp_path, monkeypatch):
    """A job removed from schedule.json must not keep the self-check red forever."""
    runs = tmp_path / "runs.jsonl"
    runs.write_text("\n".join(json.dumps({"job": "gone", "status": "timeout"}) for _ in range(6)))
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}))
    (tmp_path / "hostcron").mkdir()
    (tmp_path / "hostcron" / "schedule.json").write_text(json.dumps({"jobs": [{"name": "other"}]}))
    monkeypatch.setattr(selfcheck, "RUNS", runs)
    monkeypatch.setattr(selfcheck, "HEARTBEAT", hb)
    monkeypatch.setattr(selfcheck, "HERE", tmp_path)
    fails: list[str] = []
    selfcheck.check_hostcron(fails, {})
    assert not [f for f in fails if "gone" in f]


def test_host_memory_low_is_a_failure_and_plenty_is_a_pass(monkeypatch):
    import ctypes

    def fake_status(avail_gib):
        def _call(ref):
            st = ref._obj
            st.ullTotalPhys = 64 * 2**30
            st.ullAvailPhys = int(avail_gib * 2**30)
            return 1
        return _call

    class K32:
        GlobalMemoryStatusEx = staticmethod(fake_status(0.2))

    class Win:
        kernel32 = K32

    monkeypatch.setattr(ctypes, "windll", Win, raising=False)
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_host_memory(fails, metrics)  # sabotage: 200 MB available
    assert fails and metrics["ops_host_mem_available_bytes"] < 2**30

    K32.GlobalMemoryStatusEx = staticmethod(fake_status(20))
    fails, metrics = [], {}
    selfcheck.check_host_memory(fails, metrics)  # control: 20 GiB available
    assert not fails and metrics["ops_host_mem_used_ratio"] < 0.7
