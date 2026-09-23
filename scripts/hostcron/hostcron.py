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
    has its whole process tree killed and is reported as `timeout`.
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

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
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


def _run_job(job: dict) -> None:
    name = job["name"]
    timeout_s = int(job.get("timeout_s", 1800))
    started = time.time()
    started_iso = _utc()
    job_log = LOG_DIR / (name + ".log")
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log.info("start %s (timeout %ss)", name, timeout_s)
    rc = None
    status = "ok"
    detail = ""

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
                env={**os.environ, **job.get("env", {})},
            )
            try:
                rc = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                rc = None
                status = "timeout"
                detail = "exceeded %ss" % timeout_s
    except Exception as exc:  # noqa: BLE001 - a bad job must not kill the service
        status = "error"
        detail = "%s: %s" % (type(exc).__name__, exc)
        log.exception("job %s failed to launch", name)

    duration = round(time.time() - started, 1)
    if status == "ok":
        ok_codes = job.get("ok_exit_codes", [0])
        if rc not in ok_codes:
            status = "failed"
            detail = "rc=%s" % rc

    _append_ledger(
        {
            "ts": started_iso,
            "job": name,
            "status": status,
            "rc": rc,
            "duration_s": duration,
            "detail": detail,
        }
    )
    log.info("end   %s status=%s rc=%s in %ss", name, status, rc, duration)

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


def _load_schedule() -> list:
    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.exception("could not read schedule %s", SCHEDULE_PATH)
        return []
    return [j for j in data.get("jobs", []) if j.get("enabled", True)]


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
