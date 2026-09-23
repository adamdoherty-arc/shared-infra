"""Disk-space probe — pure `os.statvfs` on the container's rootfs mount
(reflects the shared Docker Desktop VM disk, see settings.py's
`infractl_disk_path` docstring). Red below 10% free, amber below 20%."""
from __future__ import annotations

import os
import time

from infractl.probes.base import result
from infractl.settings import Settings

WARN_FREE_PCT = 20.0
CRIT_FREE_PCT = 10.0


def probe(settings: Settings) -> dict:
    t0 = time.time()
    try:
        st = os.statvfs(settings.infractl_disk_path)
    except OSError as exc:
        return result("disk", False, f"statvfs({settings.infractl_disk_path}) failed: {exc}", t0)

    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    free_pct = (free / total * 100) if total else 0.0
    ok = free_pct >= CRIT_FREE_PCT
    detail = (
        f"{free_pct:.1f}% free ({free / 1_073_741_824:.1f} GiB of "
        f"{total / 1_073_741_824:.1f} GiB) at {settings.infractl_disk_path}"
    )
    if free_pct < WARN_FREE_PCT:
        detail += " -- below warn threshold" if free_pct >= CRIT_FREE_PCT else " -- CRITICAL"
    return result("disk", ok, detail, t0)
