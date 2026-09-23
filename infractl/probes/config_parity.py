"""Config parity probe — every provider active in config.json must have at
least one VK allowlist row in config.db's
`governance_virtual_key_provider_configs` table, or callers 403 with no
explanation. Drift here means `sync_vk_allowlists.py` hasn't run since the
last config.json edit (a real, recurring failure mode — an operator hand-
edits config.json without also invoking vk_sync)."""
from __future__ import annotations

import time

from infractl.bifrost import config as bifrost_config
from infractl.bifrost import configdb
from infractl.probes.base import result
from infractl.settings import Settings


def probe(settings: Settings) -> dict:
    t0 = time.time()
    config_json = settings.config_json_path
    config_db = settings.config_db_path
    if not config_json.exists() or not config_db.exists():
        return result("config_parity", False, "config.json or config.db missing", t0)

    try:
        cfg = bifrost_config.read_config(config_json)
        allowlist_counts = configdb.provider_allowlist_counts(config_db)
    except Exception as exc:  # noqa: BLE001 — a broken read is a red probe, not a crash
        return result("config_parity", False, f"read failed: {exc}", t0)

    active_providers = set((cfg.get("providers") or {}).keys())
    missing = sorted(p for p in active_providers if allowlist_counts.get(p, 0) == 0)
    ok = not missing
    detail = "all active providers have VK allowlist rows" if ok else f"missing VK rows for: {missing}"
    return result("config_parity", ok, detail, t0)
