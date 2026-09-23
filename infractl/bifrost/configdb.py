"""Read-only + targeted-write access to bifrost/config.db.

`immutable=1` is REQUIRED (not optional) on every read-only open across this
Windows 9p bind mount — a plain `mode=ro` connect() succeeds (lazy open) but
every subsequent query then throws `sqlite3.OperationalError: unable to open
database file`, because the 9p mount doesn't share locks for the `-shm`
sidecar the way a native filesystem does. This is the exact pattern
bifrost/auth_autoheal.py's `key_provider_map()` already carries; lifted
verbatim rather than rediscovered.

Writes (deregister on park, re-register on unpark) use a plain read-write
connect() — config.db tolerates being opened read-write from a second
process while Bifrost itself is stopped (see auth_autoheal.py's `park()`,
which does exactly this under the lock's `docker stop`/`docker start`
bracket), so infractl's action ladder stops shared-bifrost first."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=10)


def list_providers(db_path: Path) -> list[dict]:
    con = _ro_connect(db_path)
    try:
        rows = con.execute("SELECT name FROM config_providers ORDER BY name").fetchall()
        return [{"name": r[0]} for r in rows]
    finally:
        con.close()


def list_virtual_keys(db_path: Path) -> list[dict]:
    """Redacted: name, id, is_active, sha256 prefix — never the raw value."""
    import hashlib

    con = _ro_connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, name, is_active, value FROM governance_virtual_keys ORDER BY name"
        ).fetchall()
        out = []
        for vid, name, active, value in rows:
            digest = hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16] if value else ""
            out.append({"id": vid, "name": name, "is_active": bool(active), "sha256_prefix": digest})
        return out
    finally:
        con.close()


def provider_allowlist_counts(db_path: Path) -> dict[str, int]:
    """provider -> number of VK provider-config rows referencing it (used by
    the config_parity probe to detect a provider active in config.json with
    zero VK allowlist rows in config.db)."""
    con = _ro_connect(db_path)
    try:
        rows = con.execute(
            "SELECT provider, COUNT(*) FROM governance_virtual_key_provider_configs "
            "GROUP BY provider"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        con.close()


def deregister_provider(db_path: Path, provider: str, purge_pricing: bool = False) -> dict:
    """Mirrors bifrost/auth_autoheal.py's `_deregister()` exactly — same
    table order (child join rows before parent PC rows), same
    governance_model_pricing policy default (left alone unless
    purge_pricing=True). Caller MUST have already stopped shared-bifrost."""
    db = sqlite3.connect(str(db_path), timeout=30)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        counts: dict = {}
        counts["vk_pc_keys"] = db.execute(
            "DELETE FROM governance_virtual_key_provider_config_keys "
            "WHERE table_virtual_key_provider_config_id IN "
            "(SELECT id FROM governance_virtual_key_provider_configs WHERE provider=?)",
            (provider,),
        ).rowcount
        counts["vk_pc"] = db.execute(
            "DELETE FROM governance_virtual_key_provider_configs WHERE provider=?", (provider,)
        ).rowcount
        if purge_pricing:
            counts["model_pricing"] = db.execute(
                "DELETE FROM governance_model_pricing WHERE provider=?", (provider,)
            ).rowcount
        else:
            counts["model_pricing"] = "left-alone (repo policy)"
        counts["config_keys"] = db.execute(
            "DELETE FROM config_keys WHERE provider=?", (provider,)
        ).rowcount
        counts["config_providers"] = db.execute(
            "DELETE FROM config_providers WHERE name=?", (provider,)
        ).rowcount
        db.commit()
        return counts
    finally:
        db.close()
