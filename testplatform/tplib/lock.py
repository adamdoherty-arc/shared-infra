from __future__ import annotations

import itertools
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .profile import ARTIFACTS_ROOT

LOCK_DIR = ARTIFACTS_ROOT / ".locks"
HEAVY = "heavy"
LIGHT = "light"
GATE_STALE_S = 15.0
_SLOT_STEM = re.compile(r"^(?P<project>.+)\.light(?P<slot>\d+)$")


def split_stem(stem: str) -> tuple[str, str]:
    """Lock file stem -> (project, lane); heavy is `<project>`, light slots are `<project>.light<N>`."""
    m = _SLOT_STEM.match(stem)
    return (m.group("project"), LIGHT) if m else (stem, HEAVY)


_TICKETS = itertools.count()


def caller_group() -> str:
    """Fair-share identity of the requester: one Claude session's sweep is one group, however many runs it fans out."""
    return os.environ.get("TESTCTL_GROUP") or os.environ.get("CLAUDE_CODE_SESSION_ID") or f"pid:{os.getpid()}"


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
    lane: str = HEAVY
    group: str | None = None


class ProjectLock:
    """One lock per (project, lane). The heavy lane has a single slot; the light lane has `slots` of them.

    Heavy runs (whole tiers) stay one at a time. Light runs (a path or changed-paths request) take any free slot
    of their own lane, so they neither wait behind a heavy run nor exceed the configured concurrency. Slot
    files are `<project>.json` (heavy) and `<project>.light<N>.json`; the light lane is scanned and claimed
    under a short-lived gate file so two identical requests can never take two slots.
    """

    def __init__(self, project: str, lock_dir: Path = LOCK_DIR, lane: str = HEAVY, slots: int = 1):
        if lane not in (HEAVY, LIGHT):
            raise ValueError(f"unknown lane {lane!r}")
        self.project = project
        self.dir = lock_dir
        self.lane = lane
        self.slots = 1 if lane == HEAVY else max(1, int(slots))
        self.path = self._slot_path(0)
        self.mine = False
        self.key: str | None = None
        self.stale_taken: Held | None = None
        self._attach: Held | None = None

    def _slot_path(self, slot: int) -> Path:
        return self.dir / (f"{self.project}.json" if self.lane == HEAVY else f"{self.project}.light{slot}.json")

    @property
    def _queue_dir(self) -> Path:
        return self.dir / (f"{self.project}.queue" if self.lane == HEAVY else f"{self.project}.light.queue")

    @property
    def _gate_path(self) -> Path:
        return self.dir / f"{self.project}.{self.lane}.gate"

    def _read_at(self, path: Path) -> Held | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = int(data.get("pid", 0))
        return Held(self.project, path, pid, data.get("run_id"), stale=not pid_alive(pid), key=data.get("key"),
                    lane=self.lane, group=data.get("group"))

    def read(self) -> Held | None:
        return self._read_at(self.path)

    def holders(self) -> list[Held]:
        """Live holders across every slot of this lane."""
        out = []
        for slot in range(self.slots):
            held = self._read_at(self._slot_path(slot))
            if held is not None and not held.stale:
                out.append(held)
        return out

    def _try_slot(self, path: Path, key: str | None) -> tuple[bool, Held | None]:
        for _ in range(4):
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                held = self._read_at(path)
                if held is None:
                    time.sleep(0.05)
                    continue
                if held.stale:
                    self.stale_taken = held
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                return False, held
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "run_id": None, "started_at": time.time(), "key": key,
                       "group": caller_group()}, fh)
            self.path = path
            self.mine = True
            return True, None
        return False, self._read_at(path)

    def acquire(self, key: str | None = None) -> tuple[bool, Held | None]:
        """Own a slot (True, None), or (False, holder): the same-key holder to attach to, else a busy holder."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = key
        if self.slots == 1:
            ok, held = self._try_slot(self._slot_path(0), key)
            self._attach = None if ok else held
            return ok, held
        with _Gate(self._gate_path):
            same = next((h for h in self.holders() if key is not None and h.key == key), None)
            if same is not None:
                self._attach = same
                return False, same
            for slot in range(self.slots):
                ok, _ = self._try_slot(self._slot_path(slot), key)
                if ok:
                    self._attach = None
                    return True, None
            busy = self.holders()
            self._attach = busy[0] if busy else None
            return False, self._attach

    def set_run_id(self, run_id: int) -> None:
        self.path.write_text(json.dumps({"pid": os.getpid(), "run_id": run_id, "started_at": time.time(),
                                         "key": self.key, "group": caller_group()}), encoding="utf-8")

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

    def wait_acquire(self, key: str | None, timeout: float, poll: float = 5.0,
                     on_wait: Callable[[int, Held | None], None] | None = None) -> bool:
        """Queue behind the lane, fair-share across caller groups then first come first served; True once this
        process owns a slot. A group already holding slots yields to waiters from groups holding fewer, so one
        session's parallel sweep cannot occupy every slot while other sessions wait."""
        queue_dir = self._queue_dir
        queue_dir.mkdir(parents=True, exist_ok=True)
        ticket = queue_dir / f"{time.time_ns():020d}-{os.getpid()}-{next(_TICKETS)}"
        ticket.write_text(json.dumps({"pid": os.getpid(), "group": caller_group(), "key": key,
                                      "queued_at": time.time()}), encoding="utf-8")
        deadline = time.time() + timeout
        last_position = -1
        try:
            while time.time() < deadline:
                if self._is_next(queue_dir, ticket):
                    ok, _ = self.acquire(key)
                    if ok:
                        return True
                if on_wait is not None:
                    position = self.queue_position(ticket)
                    if position != last_position:
                        last_position = position
                        busy = self.holders()
                        on_wait(position, busy[0] if busy else None)
                time.sleep(poll)
            return False
        finally:
            ticket.unlink(missing_ok=True)

    def queue_position(self, ticket: Path) -> int:
        """1-based place in this lane's queue (1 = next to start), counting only live waiters."""
        ahead = 0
        for other in sorted(self._queue_dir.iterdir()) if self._queue_dir.exists() else []:
            if other == ticket:
                return ahead + 1
            try:
                owner, _ = self._read_ticket(other)
            except (OSError, ValueError):
                continue
            if pid_alive(owner):
                ahead += 1
        return ahead + 1

    @staticmethod
    def _read_ticket(path: Path) -> tuple[int, str]:
        raw = path.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            pid = int(data.get("pid", 0))
            return pid, str(data.get("group") or f"pid:{pid}")
        pid = int(raw or 0)
        return pid, f"pid:{pid}"

    def _is_next(self, queue_dir: Path, ticket: Path) -> bool:
        held_by: dict[str, int] = {}
        for h in self.holders():
            g = h.group or f"pid:{h.pid}"
            held_by[g] = held_by.get(g, 0) + 1
        live: list[tuple[int, str, Path]] = []
        for other in sorted(queue_dir.iterdir()):
            try:
                owner, group = self._read_ticket(other)
            except (OSError, ValueError):
                if other == ticket:
                    return True
                continue
            if other != ticket and not pid_alive(owner):
                other.unlink(missing_ok=True)
                continue
            live.append((held_by.get(group, 0), other.name, other))
        if not live:
            return True
        return min(live)[2] == ticket

    def wait_for_run_id(self, timeout: float = 15.0, key: str | None = None) -> Held | None:
        """Poll for a run id: the holder of `key` when given, else the holder acquire() attached to."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            held = self._pick(key)
            if held is None:
                if key is None:
                    return None
            elif held.run_id is not None or held.stale:
                return held
            time.sleep(0.2)
        return self._pick(key)

    def _pick(self, key: str | None) -> Held | None:
        if key is not None:
            live = self.holders()
            same = next((h for h in live if h.key == key), None)
            if same is not None:
                return same
            return live[0] if live and len(live) >= self.slots else None
        return self._read_at(self._attach.path) if self._attach is not None else self.read()


class _Gate:
    """Brief cross-process mutex (an O_EXCL file) so the light lane's scan-then-claim is atomic."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> _Gate:
        deadline = time.time() + GATE_STALE_S
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                try:
                    age = time.time() - self.path.stat().st_mtime
                    owner = int(self.path.read_text(encoding="utf-8").strip() or 0)
                except (OSError, ValueError):
                    age, owner = 0.0, -1
                if age > GATE_STALE_S or (owner > 0 and not pid_alive(owner)):
                    self._drop()
                    continue
                if time.time() > deadline:
                    raise TimeoutError(f"lane gate {self.path} busy") from None
                time.sleep(0.02)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            return self

    def _drop(self) -> None:
        for _ in range(50):
            try:
                self.path.unlink(missing_ok=True)
                return
            except PermissionError:
                time.sleep(0.01)

    def __exit__(self, *exc: object) -> None:
        self._drop()


def queue_snapshot(lock_dir: Path = LOCK_DIR) -> list[dict[str, object]]:
    """Every live waiter across every project and lane: `queued #N` for agents and the owner instead of a silent wait."""
    rows: list[dict[str, object]] = []
    if not lock_dir.exists():
        return rows
    for qdir in sorted(lock_dir.glob("*.queue")):
        stem = qdir.name[: -len(".queue")]
        light = stem.endswith(".light")
        project = stem[: -len(".light")] if light else stem
        lane = LIGHT if light else HEAVY
        live = 0
        for ticket in sorted(qdir.iterdir()):
            try:
                data = json.loads(ticket.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not pid_alive(int(data.get("pid", 0))):
                continue
            live += 1
            rows.append({"project": project, "lane": lane, "position": live, "pid": data.get("pid"),
                         "group": data.get("group"), "key": data.get("key"), "queued_at": data.get("queued_at")})
    return rows
