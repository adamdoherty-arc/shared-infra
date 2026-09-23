"""T3 `disk_prune` action mechanics: dangling docker images/volumes via the
Engine API's own `/prune` endpoints (never a raw `rm -rf` — Docker's own
prune logic already refuses anything referenced), plus `bifrost/*.bak*`
files beyond the 10 newest (auth_autoheal.py's `_snapshot()` and ADA's
`bifrost_model_sync.py`'s `_backup_config()` both write timestamped `.bak*`
files here and neither ever cleans up after itself — this is the one place
that does, and only past the newest-10 retention floor)."""
from __future__ import annotations

import time
from pathlib import Path

from infractl.core import docker as docker_client
from infractl.settings import Settings

BAK_RETAIN_COUNT = 10


def _prune_old_bak_files(bifrost_dir: Path, retain: int = BAK_RETAIN_COUNT) -> dict:
    candidates = sorted(
        (p for p in bifrost_dir.glob("*.bak*") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    to_delete = candidates[retain:]
    deleted = []
    freed_bytes = 0
    for p in to_delete:
        try:
            freed_bytes += p.stat().st_size
            p.unlink()
            deleted.append(p.name)
        except OSError as exc:
            deleted.append(f"{p.name} (failed: {exc})")
    return {
        "retained": min(len(candidates), retain), "deleted": deleted,
        "freed_bytes": freed_bytes,
    }


def prune(settings: Settings) -> dict:
    t0 = time.time()
    images_result = docker_client.prune_images()
    volumes_result = docker_client.prune_volumes()
    bak_result = _prune_old_bak_files(settings.infractl_bifrost_dir)
    return {
        "images_deleted": len(images_result.get("ImagesDeleted") or []),
        "images_space_reclaimed": images_result.get("SpaceReclaimed", 0),
        "volumes_deleted": volumes_result.get("VolumesDeleted") or [],
        "volumes_space_reclaimed": volumes_result.get("SpaceReclaimed", 0),
        "bak_files": bak_result,
        "duration_s": round(time.time() - t0, 2),
    }
