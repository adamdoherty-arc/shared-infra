"""Single-writer lock + drift guard for bifrost/config.json + config.db.

WHY a file lock AND an in-process asyncio.Lock (design decision, filed to
Legion sprint 14975): infractl runs uvicorn with a single worker process (no
`--workers N`), so the asyncio.Lock alone would already serialize every
coroutine in this process -- but the flock on
`bifrost/.infractl.lock` is what makes that guarantee hold across a
container restart mid-action (the lock file is fcntl-flock'd for the
lifetime of the holding process; a killed/restarted infractl process drops
its flock immediately, so a stuck action never wedges the lock forever) and
across the eventual case of a second infractl replica or a debug shell
`python -m infractl...` reaching the same bind mount. The asyncio.Lock alone
would NOT protect against a raw `docker exec` script editing config.json
directly; the flock is advisory but every writer in this repo (autoheal,
sync_vk_allowlists, infractl itself) already cooperates with `docker stop
shared-bifrost` as a barrier, and the flock adds a fast, local guard for the
common case of two infractl actions racing each other.

WHY a 30s drift-guard window, not shorter/longer (design decision): a
provider park/unpark cycle's own os.replace() touches config.json's mtime,
so the guard window must be at least as long as the slowest step between
"read mtime to decide" and "the write that follows" -- measured against
ADA's bifrost_model_sync.py ladder, the read-decide-write span for a single
pure-function edit is under 2s. 30s gives a wide margin for a concurrent
external writer (a human editing config.json by hand, or a not-yet-wrapped
legacy caller) without falsely blocking two infractl actions issued in
quick succession, since infractl's own writes update the ledger row's
`snapshot_dir` timestamp which the NEXT action's drift check also consults
(see `Action.execute` in core/actions.py) to distinguish "I wrote this a
moment ago" from "someone else changed it under me".
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from infractl.settings import get_settings

if os.name == "nt":  # pragma: no cover - container is always linux
    fcntl = None
else:
    import fcntl  # type: ignore


class LockTimeoutError(Exception):
    """Raised when the flock cannot be acquired within the timeout -> HTTP 423."""


class WriterConflictError(Exception):
    """Raised when config.json changed within the drift-guard window -> writer_conflict."""


_PROCESS_LOCK = asyncio.Lock()


@contextlib.asynccontextmanager
async def acquire_write_lock(timeout_s: int | None = None):
    """Acquire the process-local asyncio.Lock, then the cross-process flock
    on bifrost/.infractl.lock. Both released on exit (even on exception)."""
    settings = get_settings()
    timeout_s = timeout_s if timeout_s is not None else settings.infractl_lock_timeout_s

    try:
        await asyncio.wait_for(_PROCESS_LOCK.acquire(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise LockTimeoutError("in-process lock busy") from exc

    lock_path: Path = settings.lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        deadline = time.monotonic() + timeout_s
        acquired = False
        while time.monotonic() < deadline:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                await asyncio.sleep(0.2)
        if not acquired:
            raise LockTimeoutError(f"flock on {lock_path} busy after {timeout_s}s")
        yield
    finally:
        if fcntl is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
        _PROCESS_LOCK.release()


def check_drift(reference_mtime: float | None = None, window_s: int | None = None) -> None:
    """Refuse if config.json's mtime changed within `window_s` seconds of now
    AND that change was not the `reference_mtime` infractl itself just wrote
    (passed in by the caller after its own prior write, e.g. the snapshot
    step recording the pre-action mtime). Raises WriterConflictError."""
    settings = get_settings()
    window_s = window_s if window_s is not None else settings.infractl_drift_window_s
    path = settings.config_json_path
    if not path.exists():
        return
    mtime = path.stat().st_mtime
    now = time.time()
    if reference_mtime is not None and abs(mtime - reference_mtime) < 1e-6:
        return
    if now - mtime < window_s:
        raise WriterConflictError(
            f"config.json mtime {mtime:.3f} is within the {window_s}s drift-guard "
            f"window (now={now:.3f}) — a writer other than this action touched it"
        )


def current_config_mtime() -> float | None:
    settings = get_settings()
    path = settings.config_json_path
    return path.stat().st_mtime if path.exists() else None
