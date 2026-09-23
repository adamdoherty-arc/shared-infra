"""logs.db size probe — file size + WAL size via `os.stat` ONLY. Never opens
logs.db itself, even read-only, even with `immutable=1` — the 9p bind-mount
lock-sharing hazard documented in bifrost/restart.py's module docstring
applies to ANY foreign process opening the live WAL, not just a host
process, and infractl's own `/data/bifrost` mount is exactly such a foreign
process relative to shared-bifrost's own open handle. The costlier
`PRAGMA quick_check` integrity probe is intentionally NOT run here on every
cycle (bifrost-logs-pruner/pruner.py's own `_integrity_check()` takes ~110s
of one core over a 3.1GB file) — it is exposed only as the standalone
`logsdb_quick_check` T1 action (core/actions.py), run on demand or on a
much slower cadence than this probe."""
from __future__ import annotations

import time

from infractl.bifrost import restart as bifrost_restart
from infractl.probes.base import result
from infractl.settings import Settings


def probe(settings: Settings) -> dict:
    t0 = time.time()
    wal_mb = bifrost_restart.logs_wal_size_mb(settings.infractl_bifrost_dir)
    db_mb = bifrost_restart.logs_db_size_mb(settings.infractl_bifrost_dir)
    ok = wal_mb <= settings.infractl_wal_warn_mb
    detail = f"logs.db={db_mb:.1f}MB logs.db-wal={wal_mb:.1f}MB (warn>{settings.infractl_wal_warn_mb:.0f}MB)"
    return result("logsdb", ok, detail, t0)
