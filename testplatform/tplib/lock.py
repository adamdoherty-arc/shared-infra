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
from typing import Any

from .profile import ARTIFACTS_ROOT

LOCK_DIR = ARTIFACTS_ROOT / ".locks"
HEAVY = "heavy"
LIGHT = "light"
GATE_STALE_S = 15.0
CLAIM_GRACE_S = 30.0
_SLOT_STEM = re.compile(r"^(?P<project>.+)\.light(?P<slot>\d+)$")


def split_stem(stem: str) -> tuple[str, str]:
    """Lock file stem -> (project, lane); heavy is `<project>`, light slots are `<project>.light<N>`."""
    m = _SLOT_STEM.match(stem)
    return (m.group("project"), LIGHT) if m else (stem, HEAVY)


_TICKETS = itertools.count()


def _unlink_quiet(path: Path, attempts: int = 40) -> None:
    """Remove a file another process may be reading right now: Windows refuses with a sharing violation until the
    reader closes it, and a leader scanning the queue must never be able to kill the waiter it is scanning."""
    for _ in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.025)


def caller_group() -> str:
    """Fair-share identity of the requester: one Claude session's sweep is one group, however many runs it fans out."""
    return os.environ.get("TESTCTL_GROUP") or os.environ.get("CLAUDE_CODE_SESSION_ID") or f"pid:{os.getpid()}"


ERROR_ACCESS_DENIED = 5


def _win_pid_alive(kernel32: Any, pid: int, last_error: Callable[[], int]) -> bool:
    """A process this token cannot open still exists: OpenProcess fails with ERROR_ACCESS_DENIED for a SYSTEM-owned
    holder (hostcron jobs, `testctl serve`) when the caller is the interactive user, and reading that as "dead"
    let a user run take over a live scheduled run's lock and reap its pytest."""
    import ctypes
    from ctypes import wintypes
    query_limited = 0x1000
    still_active = 259
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(query_limited, False, pid)
    if not handle:
        return last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        return _win_pid_alive(ctypes.WinDLL("kernel32", use_last_error=True), pid, ctypes.get_last_error)
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

    def __init__(self, project: str, lock_dir: Path = LOCK_DIR, lane: str = HEAVY, slots: int = 1,
                 admit: Callable[[int], Held | None] | None = None):
        if lane not in (HEAVY, LIGHT):
            raise ValueError(f"unknown lane {lane!r}")
        self.project = project
        self.dir = lock_dir
        self.lane = lane
        self.slots = 1 if lane == HEAVY else max(1, int(slots))
        self.admit = admit
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
        if self.lane == HEAVY:
            blocker = self.admit(0) if self.admit is not None else None
            if blocker is not None:
                self._attach = blocker
                return False, blocker
            ok, held = self._try_slot(self._slot_path(0), key)
            self._attach = None if ok else held
            return ok, held
        with _Gate(self._gate_path):
            same = next((h for h in self.holders() if key is not None and h.key == key), None)
            if same is not None:
                self._attach = same
                return False, same
            blocker = self.admit(len(self.holders())) if self.admit is not None else None
            if blocker is not None:
                self._attach = blocker
                return False, blocker
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
            _unlink_quiet(self.path)
        self.mine = False

    @property
    def _claimed_dir(self) -> Path:
        return self.dir / f"{self.project}.light.claimed"

    @property
    def _handoff_dir(self) -> Path:
        return self.dir / f"{self.project}.light.handoff"

    def wait_acquire(self, key: str | None, timeout: float, poll: float = 1.0,
                     on_wait: Callable[[int, Held | None], None] | None = None) -> bool:
        """Queue behind the lane, fair-share across caller groups then first come first served; True once this
        process owns a slot. A group already holding slots yields to waiters from groups holding fewer, so one
        session's parallel sweep cannot occupy every slot while other sessions wait. A waiter that passes no
        `spec` is never claimed into a batch (see `wait_slot_or_handoff`)."""
        state, _ = self.wait_slot_or_handoff(key, timeout, poll, on_wait)
        return state == "slot"

    def wait_slot_or_handoff(self, key: str | None, timeout: float, poll: float = 1.0,
                             on_wait: Callable[[int, Held | None], None] | None = None,
                             spec: dict[str, Any] | None = None) -> tuple[str, dict[str, Any] | None]:
        """Wait in the lane's queue. Returns ("slot", None) once this process owns a slot, ("handoff", payload) when
        a leader claimed this waiter's ticket and ran its request in the leader's own pytest process, or
        ("timeout", None). A `{"retry": true}` payload means the leader could not deliver a result for this request:
        the caller queues again."""
        queue_dir = self._queue_dir
        queue_dir.mkdir(parents=True, exist_ok=True)
        ticket = queue_dir / f"{time.time_ns():020d}-{os.getpid()}-{next(_TICKETS)}"
        ticket.write_text(json.dumps({"pid": os.getpid(), "group": caller_group(), "key": key,
                                      "queued_at": time.time(), "spec": spec}), encoding="utf-8")
        deadline = time.time() + timeout
        last_position = -1
        try:
            while time.time() < deadline:
                if not ticket.exists():
                    payload = self._await_handoff(ticket.name, deadline)
                    return ("handoff", payload) if payload is not None else ("timeout", None)
                if self._is_next(queue_dir, ticket):
                    ok, _ = self.acquire(key)
                    if ok:
                        if spec is None or self._take_ticket(ticket):
                            return "slot", None
                        self.release()
                        continue
                if on_wait is not None:
                    position = self.queue_position(ticket)
                    if position != last_position:
                        last_position = position
                        busy = self.holders()
                        on_wait(position, busy[0] if busy else None)
                time.sleep(poll)
            return "timeout", None
        finally:
            _unlink_quiet(ticket)

    def _take_ticket(self, ticket: Path) -> bool:
        """Settle, atomically, that this waiter runs on its own slot rather than inside a leader's batch.

        A leader claims a ticket by renaming it; the waiter takes its own the same way, so exactly one of the two
        renames succeeds. False means the leader (or a sharing violation) got there first: the caller gives the slot
        back and either receives the leader's result or tries again.
        """
        self._claimed_dir.mkdir(parents=True, exist_ok=True)
        mine = self._claimed_dir / f"{ticket.name}.self"
        try:
            ticket.rename(mine)
        except OSError:
            return False
        _unlink_quiet(mine)
        return True

    def claim_peers(self, batch_class: str, max_peers: int, max_files: int) -> list[dict[str, Any]]:
        """Claim queued waiters whose request can share this process's pytest run; returns their specs.

        A claim is an atomic rename of the waiter's ticket into the lane's claimed directory, so a waiter is taken by
        exactly one leader and never also starts on its own. Only waiters that published a `spec` of the same
        `batch_class` with a live owner are taken, up to `max_peers` and `max_files` distinct targets in total.
        """
        out: list[dict[str, Any]] = []
        if self.lane != LIGHT or not self._queue_dir.exists():
            return out
        self._claimed_dir.mkdir(parents=True, exist_ok=True)
        files = 0
        for ticket in sorted(self._queue_dir.iterdir()):
            if len(out) >= max_peers:
                break
            try:
                data = json.loads(ticket.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            spec = data.get("spec") if isinstance(data, dict) else None
            if not isinstance(spec, dict) or spec.get("batch_class") != batch_class:
                continue
            if not pid_alive(int(data.get("pid", 0))):
                continue
            targets = list(spec.get("targets") or [])
            if files + len(targets) > max_files:
                continue
            claimed = self._claimed_dir / ticket.name
            try:
                ticket.rename(claimed)
            except OSError:
                continue
            claimed.write_text(json.dumps({**data, "leader_pid": os.getpid(), "claimed_at": time.time()}),
                               encoding="utf-8")
            files += len(targets)
            out.append({"ticket": ticket.name, "key": data.get("key"), "spec": spec, "pid": data.get("pid")})
        return out

    def deliver(self, ticket_name: str, payload: dict[str, Any]) -> None:
        """Hand a claimed waiter its result (or a retry) and drop the claim marker."""
        self._handoff_dir.mkdir(parents=True, exist_ok=True)
        target = self._handoff_dir / f"{ticket_name}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, target)

    def _await_handoff(self, ticket_name: str, deadline: float) -> dict[str, Any] | None:
        """Wait for the leader's result for a claimed ticket; a retry payload when the leader died without one."""
        handoff = self._handoff_dir / f"{ticket_name}.json"
        claimed = self._claimed_dir / ticket_name
        grace_until = time.time() + CLAIM_GRACE_S
        while time.time() < deadline:
            if handoff.exists():
                try:
                    payload = json.loads(handoff.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(0.05)
                    continue
                _unlink_quiet(handoff)
                _unlink_quiet(claimed)
                return payload
            try:
                leader = int(json.loads(claimed.read_text(encoding="utf-8")).get("leader_pid", 0))
            except (OSError, ValueError):
                leader = 0
            if (leader and not pid_alive(leader)) or (not leader and time.time() > grace_until):
                _unlink_quiet(claimed)
                return {"retry": True, "reason": "leader died before delivering"}
            time.sleep(0.25)
        return None

    def queued_duplicate(self, key: str | None) -> int | None:
        """1-based queue place of a live waiter already holding `key` in this lane, else None: a repeat of a
        queued request must not take a second place and run the same tier twice."""
        if key is None or not self._queue_dir.exists():
            return None
        place = 0
        for ticket in sorted(self._queue_dir.iterdir()):
            try:
                data = json.loads(ticket.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not pid_alive(int(data.get("pid", 0))):
                continue
            place += 1
            if data.get("key") == key:
                return place
        return None

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
