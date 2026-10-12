#!/usr/bin/env python3
"""hostcron - single owner for the host-side recurring jobs on this box.

Replaces a dozen Windows Task Scheduler entries. Those entries were registered
with LogonType=Interactive, so every fire rendered a console window on the
desktop (worst case every 20 minutes). A Windows *service* runs in session 0
and structurally cannot render one, which is why this is a service rather than
a tidier set of scheduled tasks.

Why host-side and not in a container: these jobs need the real working tree
(git history, uncommitted state), the node toolchain (tsc/pnpm/eslint), the
docker CLI, and wsl.exe. ada-arq-worker has none of those mounted or installed
-- it has neither /app/.git nor /app/.claude -- and granting it the Docker
socket to close the gap would be a large privilege expansion for what is
essentially CI against the working tree.

Design rules this file holds to:
  - Every job has a hard timeout. Nothing runs unbounded. An overrunning job
    has its whole process tree killed and is reported as `timeout`. A job may
    opt in to a graceful stop first (`grace_s`): a console break, a bounded
    wait, then the same kill.
  - A job still running when its next tick comes due is SKIPPED, not stacked.
  - Every run lands in a JSONL ledger with rc + duration, so "did it run?" is
    answerable without reading logs.
  - Subprocesses spawn with CREATE_NO_WINDOW, so nothing flashes even if the
    service is started interactively for debugging.
  - A failing tick never kills the loop.

Stdlib only: this runs on the bare host interpreter under NSSM, with no
virtualenv and no pip step to drift.
"""
from __future__ import annotations

import functools
import json
import logging
import ntpath
import os
import signal
import subprocess
import sys
import threading
import time
import types
import urllib.request
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEDULE_PATH = Path(os.environ.get("HOSTCRON_SCHEDULE", HERE / "schedule.json"))
STATE_DIR = Path(os.environ.get("HOSTCRON_STATE_DIR", HERE / "state"))
LOG_DIR = Path(os.environ.get("HOSTCRON_LOG_DIR", HERE / "logs"))
HEARTBEAT = STATE_DIR / "heartbeat.json"
LEDGER = STATE_DIR / "runs.jsonl"

# CREATE_NO_WINDOW (0x08000000) is the whole point of this service.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

BREAK_HELPER_TIMEOUT_S = 15
STATUS_CONTROL_C_EXIT = 3221225786
GRACE_REFUSED_BATCH = "grace refused: batch/cmd job continues after a break"
ORPHAN_SETTLE_S = 3.0
ORPHAN_POLL_S = 0.25
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CONSOLE_HOSTS = ("conhost.exe", "openconsole.exe")

log = logging.getLogger("hostcron")

_stop = threading.Event()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "hostcron.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# schedule matching


def _field_matches(spec: object, value: int) -> bool:
    """`spec` is None (= any), an int, or a list of ints."""
    if spec is None:
        return True
    if isinstance(spec, bool):
        return False
    if isinstance(spec, int):
        return value == spec
    if isinstance(spec, (list, tuple, set)):
        return value in spec
    return False


def _is_due(job: dict, now: datetime) -> bool:
    s = job.get("schedule", {})
    return (
        _field_matches(s.get("minute"), now.minute)
        and _field_matches(s.get("hour"), now.hour)
        and _field_matches(s.get("dow"), now.isoweekday() % 7)  # 0 = Sunday
        and _field_matches(s.get("dom"), now.day)
    )


# ---------------------------------------------------------------------------
# reporting


def _append_ledger(row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        log.exception("could not write ledger row")


def _load_env(path: Path) -> dict:
    out: dict = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _notify_discord(content: str) -> None:
    """Page ops on failure. Best-effort: never raises into the loop."""
    env = _load_env(Path(r"C:\code\ADA\.env"))
    token = os.environ.get("DISCORD_BOT_TOKEN") or env.get("DISCORD_BOT_TOKEN", "")
    channel = os.environ.get("DISCORD_OPS_ALERTS_CHANNEL_ID") or env.get(
        "DISCORD_OPS_ALERTS_CHANNEL_ID", ""
    )
    if not token or not channel:
        return
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/channels/%s/messages" % channel,
            data=json.dumps({"content": content[:1900]}).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bot " + token,
                "Content-Type": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=15).read()  # noqa: S310 - fixed https host
    except Exception:  # noqa: BLE001 - notification must never break the loop
        log.warning("discord notify failed", exc_info=True)


# ---------------------------------------------------------------------------
# execution

_running: dict = {}
_running_lock = threading.Lock()


def _kill_tree(proc: subprocess.Popen) -> None:
    """taskkill /T, because these jobs shell out several levels deep
    (cmd -> python -> npx -> node). Killing only the direct child would orphan
    the work that is actually running long."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        log.exception("taskkill failed for pid %s", proc.pid)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _runner_env(
    job: dict, run_id: str, started_epoch: float, timeout_s: int, grace_s: int
) -> dict:
    return {
        "HOSTCRON_JOB": str(job["name"]),
        "HOSTCRON_RUN_ID": run_id,
        "HOSTCRON_PID": str(os.getpid()),
        "HOSTCRON_STARTED_EPOCH": str(int(started_epoch)),
        "HOSTCRON_DEADLINE_EPOCH": str(int(started_epoch + timeout_s)),
        "HOSTCRON_TIMEOUT_S": str(timeout_s),
        "HOSTCRON_GRACE_S": str(grace_s),
    }


def _job_env(job: dict, runner_env: dict) -> dict:
    if job.get("runner_env", True):
        return {**os.environ, **job.get("env", {}), **runner_env}
    return {
        k: v
        for k, v in {**os.environ, **job.get("env", {})}.items()
        if not k.startswith("HOSTCRON_")
    }


def _batch_like(cmd: object) -> bool:
    if isinstance(cmd, str):
        return True
    if not isinstance(cmd, (list, tuple)) or not cmd:
        return False
    first = ntpath.basename(str(cmd[0])).lower()
    return first in ("cmd", "cmd.exe") or first.endswith((".cmd", ".bat"))


def _grace_for(job: dict) -> tuple[int, str]:
    try:
        requested = int(job.get("grace_s", 0))
    except (TypeError, ValueError):
        return 0, "grace refused: grace_s is not an integer"
    if requested <= 0:
        return 0, ""
    if _batch_like(job.get("cmd")):
        return 0, GRACE_REFUSED_BATCH
    return requested, ""


def _send_console_break(pid: int) -> int:
    try:
        done = subprocess.run(
            [sys.executable, str(HERE / "console_break.py"), str(pid)],
            capture_output=True,
            timeout=BREAK_HELPER_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        log.exception("console break helper failed for pid %s", pid)
        return -1
    rc = done.returncode
    if rc == STATUS_CONTROL_C_EXIT:
        return 0
    if rc != 0:
        tail = (getattr(done, "stderr", b"") or b"")[-300:]
        log.warning(
            "console break helper rc=%s for pid %s: %s",
            rc,
            pid,
            tail.decode("utf-8", "replace").strip(),
        )
    return rc


@functools.lru_cache(maxsize=1)
def _win_api() -> types.SimpleNamespace:
    import ctypes
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", wintypes.LARGE_INTEGER),
            ("TotalKernelTime", wintypes.LARGE_INTEGER),
            ("ThisPeriodTotalUserTime", wintypes.LARGE_INTEGER),
            ("ThisPeriodTotalKernelTime", wintypes.LARGE_INTEGER),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class IdList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 256),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return types.SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        k=kernel32,
        Accounting=Accounting,
        IdList=IdList,
    )


class _JobObject:
    """A Windows Job Object that counts and terminates a grace job's descendants.
    No KILL_ON_JOB_CLOSE flag: closing the handle never kills anything."""

    def __init__(self, handle: int) -> None:
        self._h = handle

    @classmethod
    def create(cls) -> _JobObject:
        if sys.platform != "win32":
            raise OSError("job objects are win32 only")
        api = _win_api()
        handle = api.k.CreateJobObjectW(None, None)
        if not handle:
            raise api.ctypes.WinError(api.ctypes.get_last_error())
        return cls(handle)

    def assign(self, proc: subprocess.Popen) -> None:
        api = _win_api()
        if not api.k.AssignProcessToJobObject(self._h, int(proc._handle)):
            raise api.ctypes.WinError(api.ctypes.get_last_error())

    def _query(self, info_class: int, info) -> None:
        api = _win_api()
        if not api.k.QueryInformationJobObject(
            self._h, info_class, api.ctypes.byref(info), api.ctypes.sizeof(info), None
        ):
            raise api.ctypes.WinError(api.ctypes.get_last_error())

    def active(self) -> int:
        info = _win_api().Accounting()
        self._query(1, info)
        return int(info.ActiveProcesses)

    def _member_images(self) -> list:
        api = _win_api()
        ids = api.IdList()
        self._query(3, ids)
        images = []
        for pid in ids.ProcessIdList[: ids.NumberOfProcessIdsInList]:
            image = ""
            handle = api.k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    buf = api.ctypes.create_unicode_buffer(1024)
                    size = api.wintypes.DWORD(1024)
                    if api.k.QueryFullProcessImageNameW(
                        handle, 0, buf, api.ctypes.byref(size)
                    ):
                        image = ntpath.basename(buf.value).lower()
                finally:
                    api.k.CloseHandle(handle)
            images.append(image)
        return images

    def orphans(self) -> int:
        """Members left in the job, not counting the console host that CREATE_NO_WINDOW
        gives the job and that outlives the main process while anything is attached."""
        total = self.active()
        if total <= 0:
            return 0
        try:
            return sum(1 for img in self._member_images() if img not in CONSOLE_HOSTS)
        except Exception:  # noqa: BLE001 - names are a refinement; the raw count still holds
            return total

    def terminate(self) -> None:
        api = _win_api()
        if not api.k.TerminateJobObject(self._h, 1):
            raise api.ctypes.WinError(api.ctypes.get_last_error())

    def close(self) -> None:
        if self._h:
            _win_api().k.CloseHandle(self._h)
            self._h = None


def _open_job_object(proc: subprocess.Popen, name: str, run_id: str):
    jobobj = None
    try:
        jobobj = _JobObject.create()
        jobobj.assign(proc)
        return jobobj
    except Exception:  # noqa: BLE001 - a safety net must never fail the job
        log.warning(
            "job object unavailable for %s run_id=%s; its orphans will not be reaped",
            name,
            run_id,
            exc_info=True,
        )
        if jobobj is not None:
            try:
                jobobj.close()
            except Exception:  # noqa: BLE001
                pass
        return None


def _reap_orphans(jobobj, name: str, run_id: str) -> int:
    """Terminate whatever outlived the job's main process; returns how many."""
    if jobobj is None:
        return 0
    try:
        remaining = jobobj.orphans()
        waited = 0.0
        while remaining > 0 and waited < ORPHAN_SETTLE_S:
            time.sleep(ORPHAN_POLL_S)
            waited += ORPHAN_POLL_S
            remaining = jobobj.orphans()
        if remaining <= 0:
            return 0
        jobobj.terminate()
        log.warning(
            "terminated %s orphan process(es) of %s run_id=%s", remaining, name, run_id
        )
        return remaining
    except Exception:  # noqa: BLE001
        log.warning("orphan reap failed for %s run_id=%s", name, run_id, exc_info=True)
        return 0
    finally:
        try:
            jobobj.close()
        except Exception:  # noqa: BLE001
            pass


def _stop_job(proc: subprocess.Popen, grace_s: int) -> tuple[str, str]:
    """Stop an overrunning job: a hard tree kill, or a console break then a bounded
    wait then the hard kill. Returns (how, detail)."""
    if grace_s <= 0:
        _kill_tree(proc)
        return "hard", ""
    rc = _send_console_break(proc.pid)
    if rc != 0:
        _kill_tree(proc)
        return "hard", f"break not delivered (helper rc={rc}); hard kill"
    sent = time.monotonic()
    try:
        proc.wait(grace_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return "hard", f"grace {grace_s}s expired; hard kill"
    secs = round(time.monotonic() - sent, 1)
    return "graceful", f"graceful stop: exited rc={proc.returncode} after {secs}s"


def _timeout_detail(timeout_s: int, grace_s: int, stop_detail: str) -> str:
    if grace_s <= 0:
        return f"exceeded {timeout_s}s"
    return f"exceeded {timeout_s}s; {stop_detail}"


def _artifact_check(job: dict, run_id: str) -> tuple[bool, str]:
    """A job that declares an artifact is only `ok` when the artifact names THIS run
    and is not in a rejected status; an exit 0 that wrote nothing is a no-op."""
    artifact = job.get("artifact")
    if artifact is None:
        return True, ""
    if not isinstance(artifact, dict) or not artifact.get("path"):
        return False, "no-op: artifact needs an object with a path (schedule.json)"
    shown = str(artifact["path"])
    path = Path(shown)
    if not path.is_absolute() and job.get("cwd"):
        path = Path(str(job["cwd"])) / path
    key = artifact.get("run_id_key", "hostcron_run_id")
    reject = artifact.get("reject_status", [])
    if isinstance(reject, str):
        reject = [reject]

    def no_op(why: str) -> tuple[bool, str]:
        return False, f"no-op: {shown} not written by this run ({why})"

    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return no_op("file missing")
    except (OSError, ValueError) as exc:
        return no_op(f"unreadable: {type(exc).__name__}")
    if not isinstance(obj, dict):
        return no_op("not a JSON object")
    if obj.get(key) != run_id:
        return no_op(f"{key} is {obj.get(key)!r}, expected {run_id}")
    if obj.get("status") in reject:
        return no_op(f"status {obj.get('status')!r} is rejected")
    return True, ""


def _run_job(job: dict) -> None:
    name = job["name"]
    timeout_s = int(job.get("timeout_s", 1800))
    started = time.time()
    started_iso = _utc()
    run_id = uuid.uuid4().hex[:12]
    job_log = LOG_DIR / (name + ".log")
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    grace_s, grace_note = _grace_for(job)
    if grace_note:
        log.warning("job %s run_id=%s: %s", name, run_id, grace_note)

    log.info("start %s (timeout %ss) run_id=%s", name, timeout_s, run_id)
    rc = None
    status = "ok"
    detail = ""
    stop = None
    jobobj = None

    try:
        with job_log.open("a", encoding="utf-8") as fh:
            fh.write("\n===== %s :: %s =====\n" % (started_iso, name))
            fh.flush()
            proc = subprocess.Popen(  # noqa: S603 - argv from the operator-owned schedule
                job["cmd"],
                cwd=job.get("cwd") or None,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                env=_job_env(
                    job, _runner_env(job, run_id, started, timeout_s, grace_s)
                ),
            )
            if grace_s > 0:
                jobobj = _open_job_object(proc, name, run_id)
            try:
                rc = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                stop, stop_detail = _stop_job(proc, grace_s)
                rc = None
                status = "timeout"
                detail = _timeout_detail(timeout_s, grace_s, stop_detail)
    except Exception as exc:  # noqa: BLE001 - a bad job must not kill the service
        status = "error"
        detail = "%s: %s" % (type(exc).__name__, exc)
        log.exception("job %s failed to launch", name)

    duration = round(time.time() - started, 1)
    orphans = _reap_orphans(jobobj, name, run_id)
    if status == "ok":
        ok_codes = job.get("ok_exit_codes", [0])
        if rc not in ok_codes:
            status = "failed"
            detail = "rc=%s" % rc
    if status == "ok":
        artifact_ok, artifact_detail = _artifact_check(job, run_id)
        if not artifact_ok:
            status = "failed"
            detail = artifact_detail

    row = {
        "ts": started_iso,
        "job": name,
        "status": status,
        "rc": rc,
        "duration_s": duration,
        "detail": detail,
        "run_id": run_id,
        "stop": stop,
    }
    if orphans:
        row["orphans_killed"] = orphans
    _append_ledger(row)
    log.info("end   %s status=%s rc=%s in %ss run_id=%s", name, status, rc, duration, run_id)

    if status != "ok" and job.get("notify_on_failure", True):
        _notify_discord(
            "hostcron `%s` %s (%s) after %ss - log: %s"
            % (name, status, detail, duration, job_log)
        )

    with _running_lock:
        _running.pop(name, None)


def _maybe_start(job: dict) -> None:
    name = job["name"]
    with _running_lock:
        prior = _running.get(name)
        if prior is not None and prior.is_alive():
            log.warning("skip %s: previous run still active (no stacking)", name)
            _append_ledger(
                {"ts": _utc(), "job": name, "status": "skipped_overlap", "rc": None}
            )
            return
        t = threading.Thread(
            target=_run_job, args=(job,), name="job:" + name, daemon=True
        )
        _running[name] = t
    t.start()


# ---------------------------------------------------------------------------
# main loop


_last_good_jobs: list = []


def _load_schedule() -> list:
    global _last_good_jobs
    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        jobs = [j for j in data.get("jobs", []) if j.get("enabled", True)]
    except Exception:  # noqa: BLE001
        log.exception(
            "could not read schedule %s; running the last good schedule (%d jobs)",
            SCHEDULE_PATH,
            len(_last_good_jobs),
        )
        return list(_last_good_jobs)
    _last_good_jobs = jobs
    return jobs


def _beat(tick: int, job_count: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        HEARTBEAT.write_text(
            json.dumps(
                {"ts": _utc(), "tick": tick, "jobs": job_count, "pid": os.getpid()}
            ),
            encoding="utf-8",
        )
    except OSError:
        log.exception("heartbeat write failed")


def _handle_signal(signum, _frame) -> None:
    log.info("signal %s received, stopping after current tick", signum)
    _stop.set()


def main() -> int:
    _setup_logging()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    log.info("hostcron starting (schedule=%s)", SCHEDULE_PATH)
    tick = 0
    last_minute = None

    while not _stop.is_set():
        now = datetime.now()
        minute_key = now.strftime("%Y-%m-%dT%H:%M")
        if minute_key != last_minute:
            last_minute = minute_key
            tick += 1
            # re-read every minute so schedule edits apply without a restart
            jobs = _load_schedule()
            _beat(tick, len(jobs))
            for job in jobs:
                try:
                    if _is_due(job, now):
                        _maybe_start(job)
                except Exception:  # noqa: BLE001
                    log.exception("scheduling error for %s", job.get("name"))
        _stop.wait(5)

    log.info("hostcron stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
