"""Bifrost restart ladder + WAL preflight — ported from
C:\\code\\ADA\\scripts\\bifrost_model_sync.py's `restart_bifrost()` /
`preflight_check_wal()` / `_logs_wal_size_mb()`, adapted from Windows-host
`docker` CLI subprocess calls to the in-container Docker Unix socket
(infractl/core/docker.py) since this runs inside the shared-infra-control
container, not on the host. ADA-specific steps (stopping
`ada-backend-autoheal`/`ada-scheduler-autoheal`, which don't exist in this
repo) are dropped — shared-infra's restart ladder only ever touches
`shared-bifrost` + `bifrost-logs-pruner`.

WAL preflight NEVER opens logs.db directly, from the host or from this
container: Docker Desktop bind mounts are 9p, and a host/foreign-container
open of the live WAL database has been measured to delete Bifrost's
-wal/-shm sidecars on close (bifrost-logs-pruner/pruner.py docstring,
2026-09-15 incident). The size check is `os.stat` only; the checkpoint
itself runs INSIDE bifrost-logs-pruner via `docker exec` (core.docker.exec_run),
calling that container's own `pruner._mid_day_checkpoint()`.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from infractl.bifrost import vk_sync
from infractl.core import docker as docker_client

WAL_SIZE_WARN_MB = 512.0


def logs_wal_size_mb(bifrost_dir: Path) -> float:
    wal_path = bifrost_dir / "logs.db-wal"
    try:
        return wal_path.stat().st_size / 1_048_576
    except OSError:
        return 0.0


def logs_db_size_mb(bifrost_dir: Path) -> float:
    db_path = bifrost_dir / "logs.db"
    try:
        return db_path.stat().st_size / 1_048_576
    except OSError:
        return 0.0


def preflight_check_wal(bifrost_dir: Path, warn_mb: float = WAL_SIZE_WARN_MB,
                         pruner_container: str = "bifrost-logs-pruner") -> dict:
    """Returns {checked, wal_mb, checkpointed, note}. Never raises — a
    checkpoint failure degrades to 'restart will checkpoint on Bifrost's own
    shutdown', matching the ADA script's behavior."""
    size_mb = logs_wal_size_mb(bifrost_dir)
    if size_mb <= warn_mb:
        return {"checked": True, "wal_mb": size_mb, "checkpointed": False, "note": "under threshold"}
    try:
        exit_code, out = docker_client.exec_run(
            pruner_container,
            ["python", "-c", "import pruner; pruner._mid_day_checkpoint()"],
            timeout_s=300,
        )
        after_mb = logs_wal_size_mb(bifrost_dir)
        if exit_code == 0:
            return {
                "checked": True, "wal_mb": size_mb, "wal_mb_after": after_mb,
                "checkpointed": True, "note": out[-300:],
            }
        return {
            "checked": True, "wal_mb": size_mb, "checkpointed": False,
            "note": f"in-container checkpoint exit={exit_code}: {out[-300:]}",
        }
    except docker_client.DockerError as exc:
        return {
            "checked": True, "wal_mb": size_mb, "checkpointed": False,
            "note": f"pruner container unavailable: {exc}; restart will checkpoint on shutdown",
        }


def restart_bifrost(bifrost_dir: Path, bifrost_container: str = "shared-bifrost",
                     probe_base: str = "http://shared-bifrost:8080",
                     settle_s: int = 8, poll_attempts: int = 6, poll_interval_s: int = 5) -> dict:
    """The binding restart sequence, in-container form:
    stop -> start -> sleep -> vk_sync -> restart -> sleep -> poll /v1/models.
    Returns {healthy, steps: [...]} — every step logged, nothing swallowed."""
    steps: list[str] = []

    preflight = preflight_check_wal(bifrost_dir)
    steps.append(f"preflight_wal: {preflight}")

    docker_client.stop(bifrost_container)
    steps.append(f"stopped {bifrost_container}")
    docker_client.start(bifrost_container)
    steps.append(f"started {bifrost_container}")
    time.sleep(settle_s)

    try:
        sync_out = vk_sync.run_vk_sync(bifrost_dir)
        steps.append(f"vk_sync ok: {sync_out.strip().splitlines()[-1] if sync_out.strip() else '(no output)'}")
    except vk_sync.VkSyncError as exc:
        steps.append(f"vk_sync FAILED: {exc}")

    docker_client.restart(bifrost_container)
    steps.append(f"restarted {bifrost_container}")
    time.sleep(settle_s)

    healthy = False
    for attempt in range(poll_attempts):
        try:
            resp = httpx.get(f"{probe_base}/v1/models", timeout=5.0)
            if resp.status_code < 500:
                healthy = True
                steps.append(f"poll {attempt + 1}/{poll_attempts}: HTTP {resp.status_code} -> healthy")
                break
            steps.append(f"poll {attempt + 1}/{poll_attempts}: HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            steps.append(f"poll {attempt + 1}/{poll_attempts}: {exc}")
        time.sleep(poll_interval_s)

    return {"healthy": healthy, "steps": steps, "preflight": preflight}
