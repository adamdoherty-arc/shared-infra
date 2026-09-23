"""Run bifrost/sync_vk_allowlists.py to refresh config.db's per-VK provider
allowlists from config.json. That script is already env-overridable
(`BIFROST_CONFIG_DB`/`BIFROST_CONFIG_JSON`, confirmed by reading it) and has
no `if __name__` guard — it performs the sync at import/exec time, so it MUST
be run as a subprocess (matching bifrost/auth_autoheal.py's `_run_sync()`
pattern), never imported."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class VkSyncError(RuntimeError):
    pass


def run_vk_sync(bifrost_dir: Path, timeout_s: int = 120) -> str:
    script = bifrost_dir / "sync_vk_allowlists.py"
    if not script.exists():
        raise VkSyncError(f"sync_vk_allowlists.py not found at {script}")
    env = {
        **os.environ,
        "BIFROST_CONFIG_DB": str(bifrost_dir / "config.db"),
        "BIFROST_CONFIG_JSON": str(bifrost_dir / "config.json"),
    }
    result = subprocess.run(
        [sys.executable, str(script)],
        env=env, capture_output=True, text=True, timeout=timeout_s,
    )
    if result.returncode != 0:
        raise VkSyncError(
            f"sync_vk_allowlists.py exit {result.returncode}: {result.stderr.strip()[:500]}"
        )
    return result.stdout
