"""hostcron's opt-in graceful stop, per-run env, no-op artifact guard and orphan reaper.

Linux-container tests, so pure logic only. The Windows pieces (the console-break helper run for real, the Job
Object, taskkill) are proven by the host tier (`testctl run shared-infra:host`). hostcron.py is loaded by path
with HOSTCRON_SCHEDULE, HOSTCRON_STATE_DIR and HOSTCRON_LOG_DIR pointed at a scratch dir BEFORE it is imported, so
nothing here can read the live schedule or write the live ledger or logs.

What is pinned, each with a source-mutant sabotage test and an unmodified-source control:
  - a job that does not opt in keeps today's exact timeout detail, "exceeded <N>s", and a hard kill
  - grace is refused for cmd.exe, .cmd, .bat and string commands (a batch job continues after a break)
  - the job's own env can never override HOSTCRON_* (the deadline a job budgets against)
  - an exit 0 that wrote no artifact for THIS run, or left it in a rejected status, is `failed`
  - the helper's exit code decides between a graceful wait and a hard kill; 0xC000013A means delivered
  - orphans left behind by a grace job are terminated and a job object failure never fails the job
  - console_break.py keeps the call sequence the host matrix proved (b1, b3, E4)

This gate would pass trivially if it checked only that the helper call returned, if job env could override
HOSTCRON_DEADLINE_EPOCH, if a running marker satisfied the artifact guard, or if non-grace jobs got new detail text.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from collections.abc import Callable
from pathlib import Path

import pytest

HOSTCRON_PY = Path(__file__).resolve().parents[1] / "hostcron" / "hostcron.py"
CONSOLE_BREAK_PY = HOSTCRON_PY.with_name("console_break.py")
LIVE_SCHEDULE = HOSTCRON_PY.with_name("schedule.json")

_SCRATCH = Path(tempfile.mkdtemp(prefix="hostcron-graceful-"))
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)
_ENV_REDIRECT = {
    "HOSTCRON_SCHEDULE": str(_SCRATCH / "schedule.json"),
    "HOSTCRON_STATE_DIR": str(_SCRATCH / "state"),
    "HOSTCRON_LOG_DIR": str(_SCRATCH / "logs"),
}

PY_CMD = ["C:/Python314/python.exe", "C:/code/ADA/scripts/dr_restore_and_diff.py", "--target", "ada"]
REFUSED = "grace refused: batch/cmd job continues after a break"


def load_hostcron(source: str | None = None, name: str = "hostcron_under_test") -> types.ModuleType:
    text = HOSTCRON_PY.read_text(encoding="utf-8") if source is None else source
    module = types.ModuleType(name)
    module.__file__ = str(HOSTCRON_PY)
    with pytest.MonkeyPatch.context() as env:
        for key, value in _ENV_REDIRECT.items():
            env.setenv(key, value)
        exec(compile(text, str(HOSTCRON_PY), "exec"), module.__dict__)
    return module


def mutated_source(path: Path, old: str, new: str) -> str:
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"mutation anchor matched {source.count(old)} times: {old!r}"
    return source.replace(old, new)


def subprocess_shim(**overrides) -> types.SimpleNamespace:
    members = {
        "Popen": None,
        "run": None,
        "TimeoutExpired": subprocess.TimeoutExpired,
        "DEVNULL": subprocess.DEVNULL,
        "STDOUT": subprocess.STDOUT,
        "PIPE": subprocess.PIPE,
    }
    members.update(overrides)
    return types.SimpleNamespace(**members)


def forbidden(what: str) -> Callable:
    def refuse(*_args, **_kwargs):
        raise AssertionError(f"{what} must never be called")

    return refuse


class FakeProc:
    def __init__(self, waits=(0,), pid: int = 4242):
        self.pid = pid
        self.returncode = None
        self.waits = list(waits)
        self.wait_calls: list = []
        self.before_wait: Callable | None = None
        self._handle = 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.before_wait is not None:
            self.before_wait()
        outcome = self.waits.pop(0) if self.waits else 0
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        self.returncode = outcome
        return outcome

    def send_signal(self, *_args, **_kwargs):
        raise AssertionError("a job is stopped through the helper or taskkill, never a direct signal")

    def kill(self):
        raise AssertionError("_kill_tree is patched in these checks; a direct kill is a bug")


class OrphanCounter:
    def __init__(self, counts, terminate_error: Exception | None = None):
        self.counts = list(counts)
        self.terminate_error = terminate_error
        self.assigned: list = []
        self.terminated = 0
        self.closed = 0

    def orphans(self) -> int:
        return self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]

    def assign(self, proc) -> None:
        self.assigned.append(proc)

    def terminate(self) -> None:
        self.terminated += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def close(self) -> None:
        self.closed += 1


def fake_run(returncode: int, calls: list, stderr: bytes = b""):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return types.SimpleNamespace(returncode=returncode, stdout=b"", stderr=stderr)

    return run


def raising_run(error: Exception):
    def run(_argv, **_kwargs):
        raise error

    return run


def drive(
    hc,
    tmp: Path,
    job: dict,
    *,
    waits,
    helper_rc: int = 0,
    orphan_counts=None,
    job_object_error=None,
    writes=None,
):
    seen = types.SimpleNamespace(kills=[], helper_calls=[], notified=[], popen=[], job_objects=[], creates=0)
    proc = FakeProc(waits)

    class FakeJobObject(OrphanCounter):
        @classmethod
        def create(cls):
            seen.creates += 1
            if orphan_counts is None:
                raise AssertionError("a job without grace must never create a job object")
            if job_object_error is not None:
                raise job_object_error
            made = cls(orphan_counts)
            seen.job_objects.append(made)
            return made

    def popen(argv, **kwargs):
        seen.popen.append((argv, kwargs))
        if writes is not None:
            proc.before_wait = lambda: writes(kwargs["env"])
        return proc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hc, "LOG_DIR", tmp / "logs")
        mp.setattr(hc, "STATE_DIR", tmp / "state")
        mp.setattr(hc, "LEDGER", tmp / "state" / "runs.jsonl")
        mp.setattr(hc, "ORPHAN_SETTLE_S", 0.0)
        mp.setattr(hc, "_notify_discord", seen.notified.append)
        mp.setattr(hc, "_kill_tree", lambda p: seen.kills.append(p.pid))
        mp.setattr(hc, "_send_console_break", lambda pid: seen.helper_calls.append(pid) or helper_rc)
        mp.setattr(hc, "subprocess", subprocess_shim(Popen=popen))
        mp.setattr(hc, "_JobObject", FakeJobObject)
        hc._run_job(job)
    rows = [json.loads(line) for line in (tmp / "state" / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    return rows[0], seen, proc


CHECKS: dict[str, Callable[[types.ModuleType, Path], None]] = {}


def check(fn):
    CHECKS[fn.__name__.removeprefix("check_")] = fn
    return fn


def run_check(name: str, module: types.ModuleType, base: Path) -> None:
    workdir = base / name
    workdir.mkdir(parents=True, exist_ok=True)
    CHECKS[name](module, workdir)


@check
def check_runner_env(hc, tmp):
    job = {"name": "ada-dr-restore-drill-weekly"}
    env = hc._runner_env(job, "abc123def456", 1_700_000_000, 13_500, 1_200)
    assert env == {
        "HOSTCRON_JOB": "ada-dr-restore-drill-weekly",
        "HOSTCRON_RUN_ID": "abc123def456",
        "HOSTCRON_PID": str(os.getpid()),
        "HOSTCRON_STARTED_EPOCH": "1700000000",
        "HOSTCRON_DEADLINE_EPOCH": "1700013500",
        "HOSTCRON_TIMEOUT_S": "13500",
        "HOSTCRON_GRACE_S": "1200",
    }
    assert all(isinstance(value, str) for value in env.values())
    fractional = hc._runner_env(job, "abc123def456", 1_700_000_000.9, 13_500, 0)
    assert fractional["HOSTCRON_STARTED_EPOCH"] == "1700000000"
    assert int(fractional["HOSTCRON_DEADLINE_EPOCH"]) == 1_700_000_000 + 13_500


@check
def check_job_env_cannot_override(hc, tmp):
    runner = hc._runner_env({"name": "j"}, "abc123def456", 1_700_000_000, 3_000, 1_200)
    job = {"name": "j", "env": {"HOSTCRON_DEADLINE_EPOCH": "1", "HOSTCRON_RUN_ID": "forged", "KEEP": "yes"}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOSTCRON_DEADLINE_EPOCH", "2")
        mp.setenv("HOSTCRON_PID", "99")
        env = hc._job_env(job, runner)
    for key, value in runner.items():
        assert env[key] == value, key
    assert env["KEEP"] == "yes"


@check
def check_job_env_runner_env_off(hc, tmp):
    runner = hc._runner_env({"name": "serve"}, "abc123def456", 1_700_000_000, 120, 0)
    job = {"name": "serve", "runner_env": False, "env": {"HOSTCRON_X": "1", "KEEP": "yes"}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOSTCRON_DEADLINE_EPOCH", "2")
        mp.setenv("HOSTCRON_PID", "99")
        env = hc._job_env(job, runner)
    assert [key for key in env if key.startswith("HOSTCRON_")] == []
    assert env["KEEP"] == "yes"


@check
def check_grace_python_keeps_grace(hc, tmp):
    assert hc._grace_for({"cmd": PY_CMD, "grace_s": 1200}) == (1200, "")


@check
def check_grace_missing_field_is_zero(hc, tmp):
    assert hc._grace_for({"cmd": PY_CMD}) == (0, "")
    assert hc._grace_for({"cmd": ["cmd.exe", "/c", "x.cmd"]}) == (0, "")
    assert hc._grace_for({"cmd": PY_CMD, "grace_s": 0}) == (0, "")
    assert hc._grace_for({"cmd": PY_CMD, "grace_s": -5}) == (0, "")


@check
def check_grace_refused_cmd_exe(hc, tmp):
    assert hc._grace_for({"cmd": ["cmd.exe", "/c", "x.cmd"], "grace_s": 1200}) == (0, REFUSED)
    assert hc._grace_for({"cmd": [r"C:\Windows\System32\CMD.EXE", "/c", "x"], "grace_s": 1200}) == (0, REFUSED)
    assert hc._grace_for({"cmd": ["cmd", "/c", "x"], "grace_s": 1200}) == (0, REFUSED)


@check
def check_grace_refused_batch_files(hc, tmp):
    assert hc._grace_for({"cmd": [r"C:\code\ADA\scripts\run-ratchet-sweep.cmd"], "grace_s": 1200}) == (0, REFUSED)
    assert hc._grace_for({"cmd": ["C:/code/x/run.BAT", "arg"], "grace_s": 1200}) == (0, REFUSED)


@check
def check_grace_refused_string_cmd(hc, tmp):
    assert hc._grace_for({"cmd": "python x.py", "grace_s": 1200}) == (0, REFUSED)


@check
def check_grace_bad_value_is_refused_not_raised(hc, tmp):
    grace, why = hc._grace_for({"cmd": PY_CMD, "grace_s": "abc"})
    assert grace == 0 and why.startswith("grace refused:")


def artifact_job(path: Path, **extra) -> dict:
    artifact = {"path": str(path), "run_id_key": "hostcron_run_id", "reject_status": ["running"], **extra}
    return {"name": "dr", "cmd": PY_CMD, "artifact": artifact}


@check
def check_artifact_matching_run_id_is_ok(hc, tmp):
    path = tmp / "latest.json"
    path.write_text(json.dumps({"hostcron_run_id": "abc123def456", "status": "ok"}), encoding="utf-8")
    assert hc._artifact_check(artifact_job(path), "abc123def456") == (True, "")
    path.write_text(json.dumps({"hostcron_run_id": "abc123def456", "status": "failed"}), encoding="utf-8")
    assert hc._artifact_check(artifact_job(path), "abc123def456") == (True, "")


@check
def check_artifact_stale_run_id_no_op(hc, tmp):
    path = tmp / "latest.json"
    path.write_text(json.dumps({"hostcron_run_id": "oldrun000000", "status": "ok"}), encoding="utf-8")
    ok, detail = hc._artifact_check(artifact_job(path), "abc123def456")
    assert ok is False
    assert detail.startswith("no-op: ") and str(path) in detail and "oldrun000000" in detail


@check
def check_artifact_missing_file_no_op(hc, tmp):
    path = tmp / "never-written.json"
    ok, detail = hc._artifact_check(artifact_job(path), "abc123def456")
    assert ok is False
    assert detail.startswith("no-op: ") and "file missing" in detail


@check
def check_artifact_running_status_no_op(hc, tmp):
    path = tmp / "latest.json"
    path.write_text(json.dumps({"hostcron_run_id": "abc123def456", "status": "running"}), encoding="utf-8")
    ok, detail = hc._artifact_check(artifact_job(path), "abc123def456")
    assert ok is False
    assert detail.startswith("no-op: ") and "running" in detail


@check
def check_artifact_unreadable_files_fail_closed(hc, tmp):
    path = tmp / "latest.json"
    for body in ("{not json", json.dumps(["not", "an", "object"]), ""):
        path.write_text(body, encoding="utf-8")
        ok, detail = hc._artifact_check(artifact_job(path), "abc123def456")
        assert ok is False and detail.startswith("no-op: "), body


@check
def check_artifact_field_absent_is_ok(hc, tmp):
    assert hc._artifact_check({"name": "x", "cmd": PY_CMD}, "abc123def456") == (True, "")


@check
def check_artifact_malformed_field_fails_closed(hc, tmp):
    for bad in ({}, "some/path.json", {"run_id_key": "hostcron_run_id"}):
        ok, detail = hc._artifact_check({"name": "x", "artifact": bad}, "abc123def456")
        assert ok is False and detail.startswith("no-op: "), bad


@check
def check_artifact_honours_a_custom_run_id_key(hc, tmp):
    path = tmp / "latest.json"
    path.write_text(json.dumps({"rid": "abc123def456", "status": "ok"}), encoding="utf-8")
    assert hc._artifact_check(artifact_job(path, run_id_key="rid"), "abc123def456") == (True, "")
    assert hc._artifact_check(artifact_job(path), "abc123def456")[0] is False


@check
def check_stop_job_grace_zero_never_calls_helper(hc, tmp):
    proc = FakeProc()
    kills, helper_calls = [], []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hc, "_kill_tree", kills.append)
        mp.setattr(hc, "_send_console_break", lambda pid: helper_calls.append(pid) or 0)
        result = hc._stop_job(proc, 0)
    assert result == ("hard", "")
    assert kills == [proc] and helper_calls == [] and proc.wait_calls == []


@check
def check_stop_job_helper_failure_is_a_hard_kill(hc, tmp):
    for helper_rc in (2, 3, 1, -1):
        proc = FakeProc(waits=[0])
        kills: list = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(hc, "_kill_tree", kills.append)
            mp.setattr(hc, "_send_console_break", lambda _pid, rc=helper_rc: rc)
            how, detail = hc._stop_job(proc, 1200)
        assert how == "hard"
        assert detail == f"break not delivered (helper rc={helper_rc}); hard kill"
        assert kills == [proc] and proc.wait_calls == []


@check
def check_stop_job_graceful_exit(hc, tmp):
    proc = FakeProc(waits=[7])
    kills, helper_calls = [], []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hc, "_kill_tree", kills.append)
        mp.setattr(hc, "_send_console_break", lambda pid: helper_calls.append(pid) or 0)
        mp.setattr(hc.os, "kill", forbidden("os.kill"))
        how, detail = hc._stop_job(proc, 1200)
    assert how == "graceful"
    assert re.fullmatch(r"graceful stop: exited rc=7 after \d+(\.\d+)?s", detail), detail
    assert kills == [] and helper_calls == [proc.pid] and proc.wait_calls == [1200]


@check
def check_stop_job_grace_expiry_is_a_hard_kill(hc, tmp):
    proc = FakeProc(waits=["timeout"])
    kills: list = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hc, "_kill_tree", kills.append)
        mp.setattr(hc, "_send_console_break", lambda _pid: 0)
        result = hc._stop_job(proc, 1200)
    assert result == ("hard", "grace 1200s expired; hard kill")
    assert kills == [proc] and proc.wait_calls == [1200]


@check
def check_timeout_detail(hc, tmp):
    assert hc._timeout_detail(3000, 0, "") == "exceeded 3000s"
    assert hc._timeout_detail(3000, 0, "stale text") == "exceeded 3000s"
    graceful = hc._timeout_detail(3000, 1200, "graceful stop: exited rc=0 after 4.1s")
    assert graceful == "exceeded 3000s; graceful stop: exited rc=0 after 4.1s"


@check
def check_helper_rc_mapping(hc, tmp):
    for returned, expected in ((3221225786, 0), (0, 0), (1, 1), (2, 2), (3, 3)):
        calls: list = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(hc, "subprocess", subprocess_shim(run=fake_run(returned, calls)))
            assert hc._send_console_break(4242) == expected, returned
        argv, kwargs = calls[0]
        assert argv == [sys.executable, str(hc.HERE / "console_break.py"), "4242"]
        assert kwargs == {"capture_output": True, "timeout": 15, "creationflags": hc.CREATE_NO_WINDOW}
    for error in (subprocess.TimeoutExpired("helper", 15), OSError("no interpreter")):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(hc, "subprocess", subprocess_shim(run=raising_run(error)))
            assert hc._send_console_break(4242) == -1


@check
def check_open_job_object_failures_return_none(hc, tmp):
    class CreateFails:
        @classmethod
        def create(cls):
            raise OSError("no job objects here")

    made: list = []

    class AssignFails(OrphanCounter):
        @classmethod
        def create(cls):
            made.append(cls([0]))
            return made[-1]

        def assign(self, proc) -> None:
            raise OSError("access denied")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hc, "_JobObject", CreateFails)
        assert hc._open_job_object(FakeProc(), "j", "abc123def456") is None
        mp.setattr(hc, "_JobObject", AssignFails)
        assert hc._open_job_object(FakeProc(), "j", "abc123def456") is None
    assert made[0].closed == 1


@check
def check_reap_orphans(hc, tmp):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hc, "ORPHAN_SETTLE_S", 0.5)
        mp.setattr(hc, "ORPHAN_POLL_S", 0.01)
        assert hc._reap_orphans(None, "j", "r") == 0
        gone = OrphanCounter([0])
        assert hc._reap_orphans(gone, "j", "r") == 0 and gone.terminated == 0 and gone.closed == 1
        drains = OrphanCounter([2, 1, 0])
        assert hc._reap_orphans(drains, "j", "r") == 0 and drains.terminated == 0 and drains.closed == 1
        mp.setattr(hc, "ORPHAN_SETTLE_S", 0.05)
        stuck = OrphanCounter([2])
        assert hc._reap_orphans(stuck, "j", "r") == 2 and stuck.terminated == 1 and stuck.closed == 1
        failing = OrphanCounter([2], terminate_error=OSError("denied"))
        assert hc._reap_orphans(failing, "j", "r") == 0 and failing.closed == 1


@check
def check_run_job_plain_timeout_is_unchanged(hc, tmp):
    job = {"name": "plain", "cmd": ["python", "x.py"], "timeout_s": 3000, "cwd": "C:/code/ADA"}
    row, seen, proc = drive(hc, tmp, job, waits=["timeout"])
    assert row["status"] == "timeout" and row["rc"] is None
    assert row["detail"] == "exceeded 3000s"
    assert row["stop"] == "hard"
    assert set(row) == {"ts", "job", "status", "rc", "duration_s", "detail", "run_id", "stop"}
    assert re.fullmatch(r"[0-9a-f]{12}", row["run_id"])
    assert seen.kills == [proc.pid] and seen.helper_calls == [] and seen.creates == 0
    assert proc.wait_calls == [3000]
    argv, kwargs = seen.popen[0]
    assert argv == ["python", "x.py"] and kwargs["cwd"] == "C:/code/ADA"
    assert kwargs["creationflags"] == hc.CREATE_NO_WINDOW | hc.CREATE_NEW_PROCESS_GROUP
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.STDOUT
    env = kwargs["env"]
    assert env["HOSTCRON_RUN_ID"] == row["run_id"] and env["HOSTCRON_JOB"] == "plain"
    assert env["HOSTCRON_TIMEOUT_S"] == "3000" and env["HOSTCRON_GRACE_S"] == "0"
    assert env["HOSTCRON_PID"] == str(os.getpid())
    assert int(env["HOSTCRON_DEADLINE_EPOCH"]) == int(env["HOSTCRON_STARTED_EPOCH"]) + 3000
    assert seen.notified and "exceeded 3000s" in seen.notified[0]


@check
def check_run_job_graceful_stop(hc, tmp):
    job = {"name": "dr", "cmd": PY_CMD, "timeout_s": 3000, "grace_s": 1200}
    row, seen, proc = drive(hc, tmp, job, waits=["timeout", 7], orphan_counts=[0])
    assert row["status"] == "timeout" and row["stop"] == "graceful" and row["rc"] is None
    assert row["detail"].startswith("exceeded 3000s; graceful stop: exited rc=7 after ")
    assert "orphans_killed" not in row
    assert proc.wait_calls == [3000, 1200]
    assert seen.helper_calls == [proc.pid] and seen.kills == []
    assert seen.popen[0][1]["env"]["HOSTCRON_GRACE_S"] == "1200"
    (jobobj,) = seen.job_objects
    assert jobobj.assigned == [proc] and jobobj.terminated == 0 and jobobj.closed == 1


@check
def check_run_job_reaps_orphans(hc, tmp):
    job = {"name": "dr", "cmd": PY_CMD, "timeout_s": 3000, "grace_s": 1200}
    row, seen, proc = drive(hc, tmp, job, waits=["timeout", 0], orphan_counts=[2])
    assert row["orphans_killed"] == 2 and row["stop"] == "graceful"
    (jobobj,) = seen.job_objects
    assert jobobj.terminated == 1 and jobobj.closed == 1


@check
def check_run_job_reaps_orphans_of_a_clean_exit(hc, tmp):
    job = {"name": "dr", "cmd": PY_CMD, "timeout_s": 3000, "grace_s": 1200}
    row, seen, proc = drive(hc, tmp, job, waits=[0], orphan_counts=[1])
    assert row["status"] == "ok" and row["stop"] is None and row["orphans_killed"] == 1


@check
def check_run_job_job_object_failure_never_fails_the_job(hc, tmp):
    job = {"name": "dr", "cmd": PY_CMD, "timeout_s": 3000, "grace_s": 1200}
    row, seen, proc = drive(hc, tmp, job, waits=[0], orphan_counts=[0], job_object_error=OSError("boom"))
    assert row["status"] == "ok" and row["detail"] == "" and "orphans_killed" not in row
    assert seen.creates == 1 and seen.notified == []


@check
def check_run_job_refuses_grace_for_cmd_exe(hc, tmp):
    job = {"name": "bat", "cmd": ["cmd.exe", "/c", "C:/code/x/run.cmd"], "timeout_s": 3000, "grace_s": 1200}
    row, seen, proc = drive(hc, tmp, job, waits=["timeout"])
    assert row["detail"] == "exceeded 3000s" and row["stop"] == "hard"
    assert seen.helper_calls == [] and seen.creates == 0 and seen.kills == [proc.pid]
    assert seen.popen[0][1]["env"]["HOSTCRON_GRACE_S"] == "0"


@check
def check_run_job_exit_code_failure_detail_is_unchanged(hc, tmp):
    job = {"name": "plain", "cmd": ["python", "x.py"], "timeout_s": 3000}
    row, seen, proc = drive(hc, tmp, job, waits=[3])
    assert row["status"] == "failed" and row["rc"] == 3 and row["detail"] == "rc=3" and row["stop"] is None
    ok_row, _, _ = drive(hc, tmp / "ok", {**job, "ok_exit_codes": [0, 3]}, waits=[3])
    assert ok_row["status"] == "ok" and ok_row["detail"] == ""


@check
def check_run_job_artifact_no_op(hc, tmp):
    path = tmp / "latest.json"
    path.write_text(json.dumps({"hostcron_run_id": "previousrun0", "status": "ok"}), encoding="utf-8")
    job = {**artifact_job(path), "timeout_s": 3000}
    row, seen, proc = drive(hc, tmp, job, waits=[0])
    assert row["status"] == "failed" and row["rc"] == 0
    assert row["detail"].startswith("no-op: ") and "previousrun0" in row["detail"]
    assert seen.notified, "a no-op pages ops like any other failure"


@check
def check_run_job_artifact_running_is_failed(hc, tmp):
    path = tmp / "latest.json"

    def drill_leaves_running_marker(env):
        path.write_text(json.dumps({"hostcron_run_id": env["HOSTCRON_RUN_ID"], "status": "running"}), encoding="utf-8")

    job = {**artifact_job(path), "timeout_s": 3000}
    row, seen, proc = drive(hc, tmp, job, waits=[0], writes=drill_leaves_running_marker)
    assert row["status"] == "failed" and row["detail"].startswith("no-op: ") and "running" in row["detail"]


@check
def check_run_job_artifact_written_by_this_run_is_ok(hc, tmp):
    path = tmp / "latest.json"

    def drill_writes_its_report(env):
        path.write_text(json.dumps({"hostcron_run_id": env["HOSTCRON_RUN_ID"], "status": "ok"}), encoding="utf-8")

    job = {**artifact_job(path), "timeout_s": 3000}
    row, seen, proc = drive(hc, tmp, job, waits=[0], writes=drill_writes_its_report)
    assert row["status"] == "ok" and row["detail"] == "" and seen.notified == []
    assert json.loads(path.read_text(encoding="utf-8"))["hostcron_run_id"] == row["run_id"]


@check
def check_schedule_grace_jobs_keep_their_grace(hc, tmp):
    jobs = json.loads(LIVE_SCHEDULE.read_text(encoding="utf-8"))["jobs"]
    refused = []
    for job in jobs:
        asked = int(job.get("grace_s", 0) or 0)
        if asked > 0 and hc._grace_for(job)[0] != asked:
            refused.append(job["name"])
    assert refused == [], f"grace_s is refused for {refused}: a batch or string cmd continues after a break"


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_check_passes_on_the_real_source(name, tmp_path):
    run_check(name, load_hostcron(), tmp_path)


def test_control_unmodified_source_passes_every_check(tmp_path):
    module = load_hostcron(HOSTCRON_PY.read_text(encoding="utf-8"), name="hostcron_control")
    for name in CHECKS:
        run_check(name, module, tmp_path)


MUTANTS = [
    (
        "cmd_exe_keeps_its_grace",
        '    return first in ("cmd", "cmd.exe") or first.endswith((".cmd", ".bat"))\n',
        '    return first.endswith((".cmd", ".bat"))\n',
        ["grace_refused_cmd_exe", "run_job_refuses_grace_for_cmd_exe"],
    ),
    (
        "string_cmd_keeps_its_grace",
        "    if isinstance(cmd, str):\n        return True\n",
        "    if isinstance(cmd, str):\n        return False\n",
        ["grace_refused_string_cmd"],
    ),
    (
        "artifact_check_dropped_from_run_job",
        "artifact_ok, artifact_detail = _artifact_check(job, run_id)\n",
        'artifact_ok, artifact_detail = True, ""\n',
        ["run_job_artifact_no_op", "run_job_artifact_running_is_failed"],
    ),
    (
        "artifact_run_id_not_compared",
        "    if obj.get(key) != run_id:\n",
        "    if False:\n",
        ["artifact_stale_run_id_no_op", "run_job_artifact_no_op"],
    ),
    (
        "reject_status_ignored",
        '    if obj.get("status") in reject:\n',
        "    if False:\n",
        ["artifact_running_status_no_op", "run_job_artifact_running_is_failed"],
    ),
    (
        "runner_env_merged_before_job_env",
        '        return {**os.environ, **job.get("env", {}), **runner_env}\n',
        '        return {**os.environ, **runner_env, **job.get("env", {})}\n',
        ["job_env_cannot_override"],
    ),
    (
        "runner_env_false_keeps_hostcron_vars",
        '        if not k.startswith("HOSTCRON_")\n',
        "        if True\n",
        ["job_env_runner_env_off"],
    ),
    (
        "non_grace_timeout_detail_changed",
        '    if grace_s <= 0:\n        return f"exceeded {timeout_s}s"\n',
        "",
        ["timeout_detail", "run_job_plain_timeout_is_unchanged"],
    ),
    (
        "helper_rc_not_checked",
        '    if rc != 0:\n        _kill_tree(proc)\n        return "hard", f"break not delivered',
        '    if False:\n        _kill_tree(proc)\n        return "hard", f"break not delivered',
        ["stop_job_helper_failure_is_a_hard_kill"],
    ),
    (
        "ctrl_c_exit_not_mapped_to_delivered",
        "    if rc == STATUS_CONTROL_C_EXIT:\n",
        "    if False:\n",
        ["helper_rc_mapping"],
    ),
    (
        "grace_zero_calls_the_helper",
        '    if grace_s <= 0:\n        _kill_tree(proc)\n        return "hard", ""\n',
        '    if grace_s < 0:\n        _kill_tree(proc)\n        return "hard", ""\n',
        ["stop_job_grace_zero_never_calls_helper"],
    ),
    (
        "orphans_never_terminated",
        "        jobobj.terminate()\n        log.warning(\n",
        "        log.warning(\n",
        ["reap_orphans", "run_job_reaps_orphans"],
    ),
]


def test_every_sabotage_names_real_checks():
    unknown = [name for _, _, _, names in MUTANTS for name in names if name not in CHECKS]
    assert unknown == []


@pytest.mark.parametrize(("mutant_id", "old", "new", "must_fail"), MUTANTS, ids=[m[0] for m in MUTANTS])
def test_sabotage_mutant_is_caught(mutant_id, old, new, must_fail, tmp_path):
    module = load_hostcron(mutated_source(HOSTCRON_PY, old, new), name=f"hostcron_mutant_{mutant_id}")
    for name in must_fail:
        with pytest.raises(AssertionError):
            run_check(name, module, tmp_path)


def console_break_problems(source: str) -> list[str]:
    problems = []
    for text in ("CTRL_BREAK_EVENT = 1\n", "def _swallow(_ctrl_type: int) -> bool:\n        return True\n"):
        if text not in source:
            problems.append(f"missing: {text.strip()!r}")
    sequence = [
        "kernel32.FreeConsole()\n    if not kernel32.AttachConsole(pid):\n        return 2\n",
        "kernel32.SetConsoleCtrlHandler(None, True)\n",
        "kernel32.SetConsoleCtrlHandler(ctypes.cast(swallow, ctypes.c_void_p), True)\n",
        "kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)\n",
        "time.sleep(0.3)\n",
        "kernel32.FreeConsole()\n    return 0 if ok else 3\n",
    ]
    cursor = 0
    for text in sequence:
        found = source.find(text, cursor)
        if found < 0:
            problems.append(f"missing or out of order: {text.splitlines()[0]!r}")
        else:
            cursor = found + len(text)
    return problems


BREAK_MUTANTS = [
    ("ctrl_c_instead_of_ctrl_break", "CTRL_BREAK_EVENT = 1\n", "CTRL_BREAK_EVENT = 0\n"),
    (
        "swallow_handler_never_registered",
        "    kernel32.SetConsoleCtrlHandler(ctypes.cast(swallow, ctypes.c_void_p), True)\n",
        "",
    ),
    (
        "swallow_handler_returns_false",
        "    def _swallow(_ctrl_type: int) -> bool:\n        return True\n",
        "    def _swallow(_ctrl_type: int) -> bool:\n        return False\n",
    ),
    ("no_wait_before_detach", "    time.sleep(0.3)\n", ""),
    ("attach_failure_not_reported", "    if not kernel32.AttachConsole(pid):\n        return 2\n", ""),
]


def test_console_break_keeps_the_sequence_the_host_matrix_proved():
    source = CONSOLE_BREAK_PY.read_text(encoding="utf-8")
    compile(source, str(CONSOLE_BREAK_PY), "exec")
    assert console_break_problems(source) == []


@pytest.mark.parametrize(("mutant_id", "old", "new"), BREAK_MUTANTS, ids=[m[0] for m in BREAK_MUTANTS])
def test_console_break_sabotage_is_caught(mutant_id, old, new):
    assert console_break_problems(mutated_source(CONSOLE_BREAK_PY, old, new)) != []
