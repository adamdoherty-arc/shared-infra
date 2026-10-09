"""Sabotage + control tests for scripts/ops_selfcheck.py and scripts/ops_reconcile.py
(Legion sprint 15209). A watcher that never fails is indistinguishable from a broken one, so
every failing-input test has a passing control beside it."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
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


def test_host_commit_charge_high_is_a_failure_naming_culprits(monkeypatch, tmp_path):
    import ctypes

    def fake_status(avail_commit_gib):
        def _call(ref):
            st = ref._obj
            st.ullTotalPhys = 64 * 2**30
            st.ullAvailPhys = 20 * 2**30
            st.ullTotalPageFile = 128 * 2**30
            st.ullAvailPageFile = int(avail_commit_gib * 2**30)
            return 1
        return _call

    class K32:
        GlobalMemoryStatusEx = staticmethod(fake_status(4))

    class Win:
        kernel32 = K32

    monkeypatch.setattr(ctypes, "windll", Win, raising=False)
    monkeypatch.setattr(selfcheck, "HOST_MEM_TOP_LOG", tmp_path / "host-mem-top.jsonl")
    monkeypatch.setattr(selfcheck, "host_memory_top_consumers",
                        lambda n=8: [{"pid": 53100, "name": "python.exe", "private_gib": 46.9, "cmd": "python x.py"}])
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_host_memory(fails, metrics)  # sabotage: 124 of 128 GiB committed
    assert fails and "python.exe(53100)" in fails[0] and metrics["ops_host_commit_used_ratio"] > 0.9
    logged = json.loads((tmp_path / "host-mem-top.jsonl").read_text(encoding="utf8").splitlines()[-1])
    assert logged["top"][0]["pid"] == 53100

    K32.GlobalMemoryStatusEx = staticmethod(fake_status(60))
    fails, metrics = [], {}
    selfcheck.check_host_memory(fails, metrics)  # control: 68 of 128 GiB committed
    assert not fails and metrics["ops_host_commit_used_ratio"] < 0.6


_EDT = timezone(timedelta(hours=-4))
_ALL_CHECKS = ("check_backups", "check_offvolume", "check_drills", "check_hostcron", "check_exposure",
               "check_default_pg_password", "check_capacity", "check_host_memory", "check_containers",
               "check_docker_boot_tasks", "check_gpu", "check_gpu_budget")


def _scratch_row(name: str, age_h: float, labels: str = "", state: str = "running",
                 created: str | None = None) -> str:
    stamp = (datetime.now(UTC) - timedelta(hours=age_h)).astimezone(_EDT).strftime("%Y-%m-%d %H:%M:%S %z") + " EDT"
    return json.dumps({"Names": name, "State": state, "Labels": labels, "CreatedAt": created or stamp})


def _docker_ps(monkeypatch, *rows: str, rc: int = 0, stderr: str = "") -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_sh(cmd, timeout=60):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, "".join(row + "\n" for row in rows), stderr)

    monkeypatch.setattr(selfcheck, "sh", fake_sh)
    return seen


def test_dr_scratch_container_past_five_hours_is_reported_as_a_leak(monkeypatch):
    """Sabotage: a drill killed before it wrote anything (2026-10-04 shape) leaves a container nobody reads."""
    seen = _docker_ps(monkeypatch,
                      _scratch_row("ada-dr-restore-test-0ff648c6", 6.0),
                      _scratch_row("legion-dr-restore-test-1a2b3c4d", 30.0, state="exited"))
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_dr_scratch_leaks(fails, metrics)
    assert seen[0][:3] == ["docker", "ps", "-a"]
    assert metrics["ops_dr_scratch_containers_leaked"] == 2
    assert len(fails) == 2
    assert "ada-dr-restore-test-0ff648c6 leaked" in fails[0]
    assert "docker rm -f -v ada-dr-restore-test-0ff648c6" in fails[0]
    assert "legion-dr-restore-test-1a2b3c4d leaked" in fails[1] and "state exited" in fails[1]


def test_dr_scratch_age_threshold_is_five_hours(monkeypatch):
    _docker_ps(monkeypatch, _scratch_row("ada-dr-restore-test-aaaaaaaa", 4.5))
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_dr_scratch_leaks(fails, metrics)  # control: inside the 4h restore cap plus margin
    assert not fails and metrics["ops_dr_scratch_containers_leaked"] == 0

    _docker_ps(monkeypatch, _scratch_row("ada-dr-restore-test-aaaaaaaa", 5.5))
    fails, metrics = [], {}
    selfcheck.check_dr_scratch_leaks(fails, metrics)  # sabotage: just past it
    assert len(fails) == 1 and metrics["ops_dr_scratch_containers_leaked"] == 1


def test_dr_scratch_probe_leaves_alone_what_is_not_a_leak(monkeypatch):
    """Control: a kept container, look-alike names and unrelated containers are never flagged."""
    _docker_ps(monkeypatch,
               _scratch_row("ada-dr-restore-test-aaaaaaaa", 9.0, labels="ada.dr-drill=1,ada.dr-drill.keep=1"),
               _scratch_row("my-ada-dr-restore-test-0ff648c6", 9.0),
               _scratch_row("ada-dr-restore-test-notahex1", 9.0),
               _scratch_row("ada-dr-restore-test-0FF648C6", 9.0),
               _scratch_row("ada-db-backup", 99.0))
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_dr_scratch_leaks(fails, metrics)
    assert not fails
    assert metrics["ops_dr_scratch_containers_leaked"] == 0


def test_dr_scratch_probe_with_nothing_to_report_is_clean(monkeypatch):
    _docker_ps(monkeypatch)
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_dr_scratch_leaks(fails, metrics)
    assert not fails and metrics["ops_dr_scratch_containers_leaked"] == 0


def test_dr_scratch_probe_that_cannot_read_docker_is_a_failure_never_a_pass(monkeypatch):
    _docker_ps(monkeypatch, rc=1, stderr="error during connect: the docker daemon is not reachable")
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_dr_scratch_leaks(fails, metrics)
    assert fails and "docker ps -a failed" in fails[0]
    assert "ops_dr_scratch_containers_leaked" not in metrics


def test_dr_scratch_probe_reports_unreadable_rows_and_unreadable_ages(monkeypatch):
    _docker_ps(monkeypatch, "not json at all",
               _scratch_row("ada-dr-restore-test-0ff648c6", 0, created="some time yesterday"))
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_dr_scratch_leaks(fails, metrics)
    assert any("unreadable docker ps row" in f for f in fails)
    assert any("age unreadable" in f and "ada-dr-restore-test-0ff648c6" in f for f in fails)
    assert metrics["ops_dr_scratch_containers_leaked"] == 1


def _run_main(monkeypatch, capsys):
    for name in _ALL_CHECKS:
        monkeypatch.setattr(selfcheck, name, lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["ops_selfcheck.py", "--no-post", "--json"])
    rc = selfcheck.main()
    return rc, json.loads(capsys.readouterr().out)


def test_main_runs_the_dr_scratch_probe_and_a_leak_fails_the_selfcheck(monkeypatch, capsys):
    """Sabotage: a probe that is written but never registered in main() would leave every leak unseen."""
    _docker_ps(monkeypatch, _scratch_row("ada-dr-restore-test-0ff648c6", 8.0))
    rc, out = _run_main(monkeypatch, capsys)
    assert rc == 1 and not out["ok"]
    assert any("ada-dr-restore-test-0ff648c6 leaked" in f for f in out["failures"])
    assert out["metrics"]["ops_dr_scratch_containers_leaked"] == 1


def test_main_with_no_scratch_container_passes(monkeypatch, capsys):
    _docker_ps(monkeypatch)
    rc, out = _run_main(monkeypatch, capsys)
    assert rc == 0 and out["ok"] and out["failures"] == []


def _drill_state(tmp_path, monkeypatch, ada: dict) -> None:
    now = datetime.now(UTC).isoformat()
    (tmp_path / "latest.json").write_text(json.dumps({"ts": now, **ada}))
    (tmp_path / "latest-legion.json").write_text(json.dumps({"ts": now, "status": "ok"}))
    monkeypatch.setattr(selfcheck, "ADA_STATE", tmp_path)


def test_drill_that_leaked_its_scratch_container_is_called_a_leak_whatever_its_status(tmp_path, monkeypatch):
    """Sabotage: the 2026-10-04 pattern, status count_timeout plus a failed docker rm."""
    _drill_state(tmp_path, monkeypatch, {
        "status": "count_timeout", "container": "ada-dr-restore-test-0ff648c6", "container_leaked": True,
        "failure": "container_leaked: docker rm -f -v ada-dr-restore-test-0ff648c6; live count timed out"})
    fails: list[str] = []
    metrics: dict = {}
    selfcheck.check_drills(fails, metrics)
    leak = [f for f in fails if "LEAKED" in f]
    assert len(leak) == 1
    assert "ada-dr-restore-test-0ff648c6" in leak[0] and "docker rm -f -v ada-dr-restore-test-0ff648c6" in leak[0]
    assert any("status=count_timeout" in f for f in fails)
    assert metrics['ops_dr_drill_selfcheck_ok{target="ada"}'] == 0


def test_drill_without_a_leak_is_not_called_one(tmp_path, monkeypatch):
    """Control: an ok drill is clean, and a plain failure is a failure but not a leak."""
    _drill_state(tmp_path, monkeypatch, {"status": "ok", "container": "ada-dr-restore-test-0ff648c6",
                                         "container_removed": True})
    fails: list[str] = []
    selfcheck.check_drills(fails, {})
    assert fails == []

    _drill_state(tmp_path, monkeypatch, {"status": "failed", "failure": "row-count parity failed",
                                         "container_removed": True})
    fails = []
    selfcheck.check_drills(fails, {})
    assert len(fails) == 1 and "status=failed" in fails[0] and "LEAKED" not in fails[0]


def _reader(files):
    def read(p):
        if p not in files:
            raise OSError(p)
        return files[p]
    return read


def test_docker_boot_shims_forwarding_to_versioned_scripts_pass():
    files = {}
    for shim, target in selfcheck.DOCKER_BOOT_SHIMS.items():
        files[shim] = f'& "{target}" @args\nexit $LASTEXITCODE\n'
        files[target] = "# versioned\n"
    assert selfcheck.docker_boot_shim_findings(_reader(files)) == []


def test_docker_boot_shim_restored_to_old_inline_script_fails():
    # sabotage: an existence-only check passes a shim overwritten with the pre-2026-10-09 inline script
    files = {}
    for shim, target in selfcheck.DOCKER_BOOT_SHIMS.items():
        files[shim] = "& docker desktop stop\nStart-Process 'Docker Desktop.exe'\n"
        files[target] = "# versioned\n"
    found = selfcheck.docker_boot_shim_findings(_reader(files))
    assert len(found) == 2 and all("does not forward" in f for f in found)


def test_docker_boot_shim_or_target_missing_fails():
    shim, target = next(iter(selfcheck.DOCKER_BOOT_SHIMS.items()))
    assert any("shim missing" in f for f in selfcheck.docker_boot_shim_findings(_reader({})))
    found = selfcheck.docker_boot_shim_findings(_reader({shim: f'& "{target}"'}))
    assert any("target missing" in f for f in found)


def test_versioned_docker_boot_scripts_keep_the_incident_guards():
    here = Path(__file__).resolve().parents[1]
    reclaim = (here / "docker-gui-reclaim.ps1").read_text(encoding="utf8")
    boot = (here / "docker-boot-start.ps1").read_text(encoding="utf8")
    stop_at = reclaim.index("& docker desktop stop")
    assert reclaim.index("Exposure-Blockers") < stop_at
    assert reclaim.index("Set-Content -Path $snapshot") < stop_at
    assert "docker start $n" in reclaim and "--restore-only" in reclaim
    assert "Interactive-Session" in boot and boot.index("if (Interactive-Session)") < boot.index("Start-Process")
