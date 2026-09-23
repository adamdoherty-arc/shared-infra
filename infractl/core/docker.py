"""Docker Unix-socket client — stdlib http.client + socket.AF_UNIX, no docker
SDK dependency. Lifted verbatim from bifrost/auth_autoheal.py's
`_UnixHTTPConnection`/`_docker`/`_demux`/`docker_logs`/`docker_stop`/
`docker_start` (same repo, same pattern, so both writers speak the exact
same unversioned Docker Engine API surface) and extended with inspect /
restart / exec-create+start / list, which auth_autoheal.py doesn't need but
infractl's probes + T1/T2 actions do.
"""
from __future__ import annotations

import json
import socket
import time
from http.client import HTTPConnection

from infractl.settings import get_settings


class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, sock_path: str, timeout: int = 90) -> None:
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


class DockerError(RuntimeError):
    pass


def _docker(method: str, path: str, body: bytes | None = None,
            headers: dict | None = None, read_body: bool = True,
            sock_path: str | None = None) -> tuple[int, bytes, str]:
    sock_path = sock_path or get_settings().infractl_docker_socket
    conn = _UnixHTTPConnection(sock_path)
    try:
        hdrs = {"Content-Type": "application/json"} if body else {}
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        ctype = resp.getheader("Content-Type", "") or ""
        data = resp.read() if read_body else b""
        return resp.status, data, ctype
    finally:
        conn.close()


def _demux(raw: bytes, ctype: str) -> str:
    """Demultiplex a non-TTY Docker log/exec stream (8-byte frame headers).
    Falls back to raw decode for a TTY / already-flat stream."""
    if not raw:
        return ""
    framed = "multiplexed" in ctype.lower() or raw[0] in (0, 1, 2)
    if not framed:
        return raw.decode("utf-8", "replace")
    out = bytearray()
    i, n = 0, len(raw)
    while i + 8 <= n:
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        i += 8
        out += raw[i:i + size]
        i += size
    if i < n:
        out += raw[i:]
    return out.decode("utf-8", "replace")


def list_containers(all_: bool = True) -> list[dict]:
    status, body, _ = _docker("GET", f"/containers/json?all={1 if all_ else 0}")
    if status != 200:
        raise DockerError(f"list containers -> HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def inspect(container: str) -> dict:
    status, body, _ = _docker("GET", f"/containers/{container}/json")
    if status != 200:
        raise DockerError(f"inspect {container} -> HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def is_healthy(container: str) -> tuple[bool, str]:
    """Returns (ok, state) where state is docker's own Health.Status when a
    healthcheck is defined, else State.Status ("running" is treated as ok)."""
    try:
        data = inspect(container)
    except DockerError as exc:
        return False, f"inspect_failed:{exc}"
    state = data.get("State", {})
    health = state.get("Health", {})
    if health:
        status = health.get("Status", "unknown")
        return status == "healthy", status
    status = state.get("Status", "unknown")
    return status == "running", status


def logs(container: str, since_s: int, tail: int | None = None) -> str:
    since_ts = int(time.time()) - since_s
    path = f"/containers/{container}/logs?stdout=1&stderr=1&timestamps=0&since={since_ts}"
    if tail:
        path += f"&tail={tail}"
    status, body, ctype = _docker("GET", path)
    if status != 200:
        raise DockerError(f"logs {container} -> HTTP {status}: {body[:200]!r}")
    return _demux(body, ctype)


def stop(container: str, timeout_s: int = 10) -> int:
    status, _, _ = _docker("POST", f"/containers/{container}/stop?t={timeout_s}")
    if status not in (204, 304):
        raise DockerError(f"stop {container} -> HTTP {status}")
    return status  # 204 stopped, 304 already stopped


def start(container: str) -> int:
    status, _, _ = _docker("POST", f"/containers/{container}/start")
    if status not in (204, 304):
        raise DockerError(f"start {container} -> HTTP {status}")
    return status  # 204 started, 304 already running


def restart(container: str, timeout_s: int = 10) -> int:
    status, _, _ = _docker("POST", f"/containers/{container}/restart?t={timeout_s}")
    if status != 204:
        raise DockerError(f"restart {container} -> HTTP {status}")
    return status


def prune_images() -> dict:
    """POST /images/prune — dangling images only (Docker's own default
    filter set; no `dangling=false` override, which would also remove
    tagged-but-unused images infractl has no business deleting)."""
    status, body, _ = _docker("POST", "/images/prune")
    if status != 200:
        raise DockerError(f"images prune -> HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def prune_volumes() -> dict:
    """POST /volumes/prune — anonymous/unreferenced volumes only (Docker's
    own default; never touches a named, still-referenced volume)."""
    status, body, _ = _docker("POST", "/volumes/prune")
    if status != 200:
        raise DockerError(f"volumes prune -> HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def exec_run(container: str, cmd: list[str], timeout_s: int = 60) -> tuple[int, str]:
    """docker exec equivalent: create + start(Detach=false) + inspect for
    the exit code. Used for the small number of in-container operations
    infractl needs that have no HTTP surface (e.g. bifrost-logs-pruner's
    `_mid_day_checkpoint()`), never for opening logs.db itself."""
    create_body = json.dumps({
        "AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": cmd,
    }).encode("utf-8")
    status, body, _ = _docker("POST", f"/containers/{container}/exec", body=create_body)
    if status != 201:
        raise DockerError(f"exec create {container} -> HTTP {status}: {body[:200]!r}")
    exec_id = json.loads(body)["Id"]

    start_body = json.dumps({"Detach": False, "Tty": False}).encode("utf-8")
    conn = _UnixHTTPConnection(get_settings().infractl_docker_socket, timeout=timeout_s)
    try:
        conn.request("POST", f"/exec/{exec_id}/start",
                      body=start_body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        ctype = resp.getheader("Content-Type", "") or ""
        out = _demux(resp.read(), ctype)
    finally:
        conn.close()

    status, body, _ = _docker("GET", f"/exec/{exec_id}/json")
    exit_code = json.loads(body).get("ExitCode", -1) if status == 200 else -1
    return exit_code, out
