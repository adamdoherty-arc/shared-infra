from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import runner
from .artifacts import prune
from .legion import LegionClient
from .lock import LOCK_DIR, ProjectLock, pid_alive
from .profile import PLATFORM_ROOT, ProfileError, legion_url, load_project

DEFAULT_PORT = 8790
PRUNE_INTERVAL_S = 6 * 3600
QUEUE_TIMEOUT_S = 4 * 3600
DRAIN_INTERVAL_S = 300
START_WAIT_S = 20.0
LOG_DIR = PLATFORM_ROOT / "logs"


def active_runs(lock_dir: Path = LOCK_DIR) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not lock_dir.exists():
        return rows
    for path in sorted(lock_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = int(data.get("pid", 0))
        if pid_alive(pid):
            rows.append({"project": path.stem, "run_id": data.get("run_id"), "pid": pid,
                         "started_at": data.get("started_at")})
    return rows


class RunnerService:
    def __init__(self, legion: LegionClient | None = None, lock_dir: Path = LOCK_DIR):
        self.legion = legion or LegionClient(legion_url())
        self.lock_dir = lock_dir
        self._queued: set[str] = set()
        self._queue_lock = threading.Lock()

    def _run_queued(self, project, body: dict[str, Any], trigger: str, key: str, queue_id: str) -> None:
        lock = ProjectLock(project.name, self.lock_dir)
        acquired = lock.wait_acquire(key, QUEUE_TIMEOUT_S)
        with self._queue_lock:
            self._queued.discard(queue_id)
        if not acquired:
            sys.stderr.write(f"queued run for {project.name} gave up after {QUEUE_TIMEOUT_S}s\n")
            return
        try:
            runner.start_and_run(project, body.get("target"), trigger, self.legion, lock,
                                 changed_paths=body.get("paths") or None)
        except Exception as exc:
            sys.stderr.write(f"queued run failed: {type(exc).__name__}: {exc}\n")
        finally:
            lock.release()

    def submit(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        name = body.get("project")
        trigger = body.get("trigger", "manual")
        if not name:
            return 400, {"accepted": False, "error": "project is required"}
        if trigger not in runner.TRIGGERS:
            return 400, {"accepted": False, "error": f"trigger must be one of {runner.TRIGGERS}"}
        try:
            project = load_project(name)
            runner.preflight(project, body.get("target"), body.get("paths") or None)
        except (ProfileError, runner.RunError) as exc:
            return 404, {"accepted": False, "error": str(exc)}
        lock = ProjectLock(name, self.lock_dir)
        key = runner.request_key(body.get("target") or project.default_target, body.get("paths") or None)
        ok, held = lock.acquire(key)
        if not ok and held is not None and held.key != key:
            queue_id = f"{name}:{key}"
            with self._queue_lock:
                already = queue_id in self._queued
                self._queued.add(queue_id)
            if not already:
                threading.Thread(target=self._run_queued, args=(project, body, trigger, key, queue_id), daemon=True,
                                 name=f"queued-{name}").start()
            return 202, {"accepted": True, "run_id": None, "queued": True, "duplicate": already}
        if not ok:
            held = lock.wait_for_run_id(5.0) or held
            return 409, {"accepted": False, "attached_run_id": held.run_id if held else None}
        started = threading.Event()
        box: dict[str, Any] = {}

        def on_started(run_id: int | None, art: Path) -> None:
            box["run_id"] = run_id
            box["artifact_path"] = art.as_posix()
            started.set()

        def work() -> None:
            try:
                runner.start_and_run(project, body.get("target"), trigger, self.legion, lock, on_started,
                                     changed_paths=body.get("paths") or None)
            finally:
                started.set()
                lock.release()

        threading.Thread(target=work, name=f"run-{name}", daemon=True).start()
        started.wait(START_WAIT_S)
        return 202, {"accepted": True, "run_id": box.get("run_id"), "artifact_path": box.get("artifact_path")}


def make_handler(service: RunnerService):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: Any) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path.startswith("/runs/active"):
                self._send(200, active_runs(service.lock_dir))
            elif self.path.startswith("/health"):
                self._send(200, {"ok": True, "pid": os.getpid()})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.startswith("/admin/exit"):
                if self.client_address[0] != "127.0.0.1":
                    self._send(403, {"error": "loopback only"})
                    return
                self._send(200, {"exiting": True})
                threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
                return
            if not self.path.startswith("/run"):
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, {"accepted": False, "error": "invalid JSON"})
                return
            code, payload = service.submit(body)
            self._send(code, payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")  # noqa: UP031

    return Handler


def _prune_loop() -> None:
    while True:
        try:
            prune()
        except OSError:
            pass
        time.sleep(PRUNE_INTERVAL_S)


def _drain_loop(legion: LegionClient) -> None:
    while True:
        time.sleep(DRAIN_INTERVAL_S)
        try:
            runner.drain_pending(legion)
        except Exception as exc:
            sys.stderr.write(f"pending drain failed: {type(exc).__name__}: {exc}\n")


def serve(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    service = RunnerService()
    threading.Thread(target=_prune_loop, daemon=True).start()
    threading.Thread(target=_drain_loop, args=(service.legion,), daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    httpd.daemon_threads = True
    sys.stderr.write(f"testctl serve listening on {host}:{port}\n")
    httpd.serve_forever()


def is_up(port: int = DEFAULT_PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def request_exit(port: int = DEFAULT_PORT) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/exit", data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def ensure_running(port: int = DEFAULT_PORT) -> str:
    if is_up(port):
        return "already running"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR / "serve.log", "ab")
    flags = 0x00000008 | 0x08000000 | 0x00000200 if sys.platform == "win32" else 0
    subprocess.Popen([sys.executable, str(PLATFORM_ROOT / "testctl.py"), "serve"], stdout=out, stderr=out,
                     stdin=subprocess.DEVNULL, cwd=str(PLATFORM_ROOT), creationflags=flags,
                     close_fds=True)
    for _ in range(20):
        time.sleep(0.5)
        if is_up(port):
            return "started"
    return "start attempted, not answering yet"
