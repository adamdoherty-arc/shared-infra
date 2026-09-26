"""Run bifrost/sync_vk_allowlists.py to refresh config.db from config.json
(provider deregistration, config_keys models/aliases, per-VK allowlists).
The script is env-overridable (`BIFROST_CONFIG_DB`/`BIFROST_CONFIG_JSON`)
and is always run as a subprocess against the bind-mounted copy, so the
sync infractl runs is byte-for-byte the one a human runs on the host. Only
`operator_violations()` is imported from it (bifrost/config.py), because
that function is pure."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class VkSyncError(RuntimeError):
    """`returncode == 2` is the sync REFUSING a config.json that carries an
    operator-disabled provider/model (bifrost/operator-disabled.json); the
    refusal text is on stdout, so both streams are kept."""

    def __init__(self, message: str, returncode: int | None = None, output: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.output = output

    @property
    def refused(self) -> bool:
        return self.returncode == 2


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
        detail = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        raise VkSyncError(
            f"sync_vk_allowlists.py exit {result.returncode}: {detail[-800:]}",
            returncode=result.returncode, output=detail,
        )
    return result.stdout
