from __future__ import annotations

import datetime as dt
import shutil
import time
import uuid
from pathlib import Path

from .profile import ARTIFACTS_ROOT

RETENTION_DAYS = 14


def new_artifact_dir(project: str, root: Path = ARTIFACTS_ROOT, now: dt.datetime | None = None) -> Path:
    now = now or dt.datetime.now()
    path = root / project / now.strftime("%Y-%m-%d") / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def prune(root: Path = ARTIFACTS_ROOT, days: int = RETENTION_DAYS, now: float | None = None) -> int:
    cutoff = (now or time.time()) - days * 86400
    removed = 0
    if not root.exists():
        return 0
    for project_dir in root.iterdir():
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        for day_dir in project_dir.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                stamp = dt.datetime.strptime(day_dir.name, "%Y-%m-%d").timestamp()
            except ValueError:
                continue
            if stamp < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)
                removed += 1
    return removed
