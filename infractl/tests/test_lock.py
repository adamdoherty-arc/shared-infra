"""core/lock.py — flock timeout + drift-guard window. `acquire_write_lock`/
`check_drift` both call `get_settings()` internally (fresh Settings() each
call, see settings.py's module docstring), so tests drive them via
monkeypatch.setenv rather than an injected Settings instance."""
from __future__ import annotations

import asyncio
import fcntl
import time

import pytest

from infractl.core.lock import (
    LockTimeoutError, WriterConflictError, acquire_write_lock, check_drift,
)


@pytest.fixture(autouse=True)
def _bifrost_dir_env(tmp_path, monkeypatch):
    d = tmp_path / "bifrost"
    d.mkdir()
    (d / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("INFRACTL_BIFROST_DIR", str(d))
    return d


@pytest.mark.asyncio
async def test_acquire_and_release_lock():
    async with acquire_write_lock(timeout_s=5):
        pass  # no exception = acquired and released cleanly


@pytest.mark.asyncio
async def test_lock_timeout_when_flock_held_externally(_bifrost_dir_env):
    lock_path = _bifrost_dir_env / ".infractl.lock"
    fh = open(lock_path, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LockTimeoutError):
            async with acquire_write_lock(timeout_s=1):
                pass
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_holders():
    order: list[str] = []

    async def holder(name: str, hold_s: float):
        async with acquire_write_lock(timeout_s=5):
            order.append(f"{name}-start")
            await asyncio.sleep(hold_s)
            order.append(f"{name}-end")

    await asyncio.gather(holder("a", 0.1), holder("b", 0.05))
    # whichever ran first must fully finish before the other starts —
    # never interleaved.
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


def test_drift_guard_raises_within_window(_bifrost_dir_env):
    with pytest.raises(WriterConflictError):
        check_drift(window_s=30)  # config.json was just written by the fixture


def test_drift_guard_passes_after_window(_bifrost_dir_env):
    old = time.time() - 60
    import os
    os.utime(_bifrost_dir_env / "config.json", (old, old))
    check_drift(window_s=30)  # no raise


def test_drift_guard_passes_with_matching_reference_mtime(_bifrost_dir_env):
    mtime = (_bifrost_dir_env / "config.json").stat().st_mtime
    check_drift(reference_mtime=mtime, window_s=30)  # no raise: it's infractl's own write
