from __future__ import annotations

import itertools
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .profile import ARTIFACTS_ROOT

LOCK_DIR = ARTIFACTS_ROOT / ".locks"
_TICKETS = itertools.count()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        query_limited = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(query_limited, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Held:
    project: str
    path: Path
    pid: int
    run_id: int | None
    stale: bool = False
    key: str | None = None


class ProjectLock:
    def __init__(self, project: str, lock_dir: Path = LOCK_DIR):
        self.project = project
        self.dir = lock_dir
        self.path = lock_dir / f"{project}.json"
        self.mine = False
        self.key: str | None = None
        self.stale_taken: Held | None = None

    def read(self) -> Held | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = int(data.get("pid", 0))
        return Held(self.project, self.path, pid, data.get("run_id"), stale=not pid_alive(pid), key=data.get("key"))

    def acquire(self, key: str | None = None) -> tuple[bool, Held | None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = key
        for _ in range(4):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                held = self.read()
                if held is None:
                    time.sleep(0.05)
                    continue
                if held.stale:
                    self.stale_taken = held
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                return False, held
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "run_id": None, "started_at": time.time(), "key": key}, fh)
            self.mine = True
            return True, None
        return False, self.read()

    def set_run_id(self, run_id: int) -> None:
        self.path.write_text(json.dumps({"pid": os.getpid(), "run_id": run_id, "started_at": time.time(),
                                         "key": self.key}), encoding="utf-8")

    def release(self) -> None:
        if not self.mine:
            return
        held = self.read()
        if held is not None and held.pid == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.mine = False

    def wait_acquire(self, key: str | None, timeout: float, poll: float = 5.0) -> bool:
        """Queue behind whoever holds the lock, first come first served; True once this process owns it."""
        queue_dir = self.dir / f"{self.project}.queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        ticket = queue_dir / f"{time.time_ns():020d}-{os.getpid()}-{next(_TICKETS)}"
        ticket.write_text(str(os.getpid()), encoding="utf-8")
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                if self._is_next(queue_dir, ticket):
                    ok, _ = self.acquire(key)
                    if ok:
                        return True
                time.sleep(poll)
            return False
        finally:
            ticket.unlink(missing_ok=True)

    @staticmethod
    def _is_next(queue_dir: Path, ticket: Path) -> bool:
        for other in sorted(queue_dir.iterdir()):
            if other == ticket:
                return True
            try:
                owner = int(other.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                continue
            if pid_alive(owner):
                return False
            other.unlink(missing_ok=True)
        return True

    def wait_for_run_id(self, timeout: float = 15.0) -> Held | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            held = self.read()
            if held is None:
                return None
            if held.run_id is not None or held.stale:
                return held
            time.sleep(0.2)
        return self.read()
