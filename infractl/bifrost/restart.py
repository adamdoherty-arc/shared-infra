"""The binding Bifrost restart sequence, in-container form, plus the logs WAL
preflight.

`restart_bifrost()` is scripts/bifrost_restart.sh run through the Docker
socket instead of the docker CLI, step for step:

  stop bifrost-autoheal -> stop shared-bifrost -> sync_vk_allowlists.py
  (config.json -> config.db, Bifrost STOPPED) -> start shared-bifrost ->
  poll /health up to 120 s -> authenticated 1-token completion against
  vllm-local (must be HTTP 200) -> start bifrost-autoheal

and, like the script's EXIT trap, a `finally` that always starts both
containers again, so a failed step never leaves the gateway or autoheal
down. Until 2026-09-25 this module ran an older ladder ported from ADA's
bifrost_model_sync.py (stop -> start -> sync against the RUNNING gateway ->
restart, never touching bifrost-autoheal), which is the sequence
30-docker.md forbids; every T2 action now goes through the binding one.

One addition the shell script does not need: a provider that is in
config.json but has no `config_keys` row yet (an unpark, or the rollback of
a park) only gets its rows when Bifrost imports config.json on boot, and the
VK sync can only grant allowlists for providers it finds in config_keys. In
that case the ladder runs a second stop -> sync -> start -> /health cycle
once Bifrost has imported the new key, so virtual keys can reach it.

`stop_autoheal=False` exists for exactly one caller: bifrost-autoheal itself
asking infractl to park a provider. That sidecar is single-threaded and is
blocked on the HTTP call for the whole ladder, so it cannot race the
restart, and stopping it would kill the request that started the action.

WAL preflight NEVER opens logs.db directly: the size check is `os.stat`
only and the checkpoint runs INSIDE bifrost-logs-pruner via `docker exec`
(bifrost-logs-pruner/pruner.py docstring, 2026-09-15 incident). Since the
2026-09-22 move of the request log store to Postgres there is normally no
logs.db-wal, and the preflight reports 0 MB.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx

from infractl.bifrost import config as bifrost_config
from infractl.bifrost import configdb, vk_sync
from infractl.core import docker as docker_client

WAL_SIZE_WARN_MB = 512.0
BIFROST_CONTAINER = "shared-bifrost"
AUTOHEAL_CONTAINER = "bifrost-autoheal"
PROBE_MODEL = "vllm-local/qwen3-chat"


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
    """Returns {checked, wal_mb, checkpointed, note}. Never raises -- a
    checkpoint failure degrades to 'restart will checkpoint on Bifrost's own
    shutdown'."""
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


def completion_probe(probe_base: str, vk: str, model: str = PROBE_MODEL,
                     timeout_s: float = 60.0) -> tuple[bool, str]:
    """Strict form of the script's step 6: only HTTP 200 passes. (The
    synthetic probe in admin_api counts a 4xx as 'responsive'; after a
    config change a 403 means the VK sync did not land, which is a failure.)"""
    body = json.dumps({
        "model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        f"{probe_base}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json", "x-bf-vk": vk},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            r.read()
            return r.status == 200, f"HTTP {r.status} in {time.time() - t0:.1f}s"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} in {time.time() - t0:.1f}s"
    except Exception as e:  # noqa: BLE001 -- timeout, refused, reset
        return False, f"{type(e).__name__} after {time.time() - t0:.1f}s"


def _poll_health(probe_base: str, timeout_s: int, interval_s: int, steps: list[str]) -> bool:
    waited = 0
    while waited < timeout_s:
        try:
            # /health, not /v1/models: an unauthenticated models listing makes
            # Bifrost log a "virtual key is required" error per provider.
            resp = httpx.get(f"{probe_base}/health", timeout=5.0)
            if resp.status_code == 200:
                steps.append(f"/health 200 after {waited}s")
                return True
        except httpx.HTTPError:
            pass
        time.sleep(interval_s)
        waited += interval_s
    steps.append(f"/health not 200 within {timeout_s}s")
    return False


def _unimported_providers(bifrost_dir: Path) -> list[str]:
    """Providers active in config.json with no config_keys row (read with
    Bifrost stopped, read-only + immutable)."""
    if not (bifrost_dir / "config.db").exists():
        return []
    try:
        active = set(bifrost_config.read_config(bifrost_dir / "config.json").get("providers", {}))
        return sorted(active - configdb.providers_with_keys(bifrost_dir / "config.db"))
    except Exception:  # noqa: BLE001 -- unreadable db: the sync below reports the real error
        return []


def restart_bifrost(bifrost_dir: Path, probe_base: str = "http://shared-bifrost:8080",
                     probe_vk: str = "", stop_autoheal: bool = True,
                     health_timeout_s: int = 120, poll_interval_s: int = 3) -> dict:
    """Run the binding sequence. Never raises; returns
    {ok, healthy, sync_ok, sync_refused, sync_error, probe_ok, probe, steps,
    preflight}. `ok` is the script's exit-0 condition."""
    steps: list[str] = []
    result: dict = {
        "ok": False, "healthy": False, "sync_ok": False, "sync_refused": False,
        "sync_error": None, "probe_ok": False, "probe": None, "steps": steps,
    }
    result["preflight"] = preflight_check_wal(bifrost_dir)
    bifrost_down = False
    autoheal_stopped = False

    def _sync() -> bool:
        try:
            out = vk_sync.run_vk_sync(bifrost_dir)
            tail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
            steps.append(f"sync_vk_allowlists ok: {tail}")
            return True
        except vk_sync.VkSyncError as exc:
            result["sync_refused"] = exc.refused
            result["sync_error"] = str(exc)[:1500]
            steps.append(f"sync_vk_allowlists {'REFUSED' if exc.refused else 'FAILED'}: {str(exc)[:300]}")
            return False

    try:
        if stop_autoheal:
            try:
                docker_client.stop(AUTOHEAL_CONTAINER)
                autoheal_stopped = True
                steps.append(f"stopped {AUTOHEAL_CONTAINER}")
            except docker_client.DockerError as exc:
                steps.append(f"{AUTOHEAL_CONTAINER} not stopped ({exc}); continuing")
        else:
            steps.append(f"{AUTOHEAL_CONTAINER} left running (it requested this action)")

        docker_client.stop(BIFROST_CONTAINER, timeout_s=60)
        bifrost_down = True
        steps.append(f"stopped {BIFROST_CONTAINER}")

        unimported = _unimported_providers(bifrost_dir)
        result["sync_ok"] = _sync()
        if not result["sync_ok"]:
            return result

        docker_client.start(BIFROST_CONTAINER)
        bifrost_down = False
        steps.append(f"started {BIFROST_CONTAINER}")
        result["healthy"] = _poll_health(probe_base, health_timeout_s, poll_interval_s, steps)
        if not result["healthy"]:
            return result

        if unimported:
            steps.append(f"second sync cycle: {unimported} had no config_keys rows before boot")
            docker_client.stop(BIFROST_CONTAINER, timeout_s=60)
            bifrost_down = True
            result["sync_ok"] = _sync()
            docker_client.start(BIFROST_CONTAINER)
            bifrost_down = False
            steps.append(f"started {BIFROST_CONTAINER} (second cycle)")
            if not result["sync_ok"]:
                return result
            result["healthy"] = _poll_health(probe_base, health_timeout_s, poll_interval_s, steps)
            if not result["healthy"]:
                return result

        if not probe_vk:
            result["probe"] = "INFRA_PROBE_VK not set -- cannot smoke test"
            steps.append(result["probe"])
            return result
        result["probe_ok"], result["probe"] = completion_probe(probe_base, probe_vk)
        steps.append(f"vllm-local 1-token probe: {result['probe']}")
        result["ok"] = result["probe_ok"]
        return result
    except docker_client.DockerError as exc:
        steps.append(f"docker error: {exc}")
        return result
    finally:
        if bifrost_down:
            try:
                docker_client.start(BIFROST_CONTAINER)
                steps.append(f"finally: started {BIFROST_CONTAINER}")
            except docker_client.DockerError as exc:
                steps.append(f"finally: CRITICAL could not start {BIFROST_CONTAINER}: {exc}")
        if autoheal_stopped:
            try:
                docker_client.start(AUTOHEAL_CONTAINER)
                steps.append(f"finally: started {AUTOHEAL_CONTAINER}")
            except docker_client.DockerError as exc:
                steps.append(f"finally: could not start {AUTOHEAL_CONTAINER}: {exc}")
