"""Read-only + targeted-write access to bifrost/config.db.

`immutable=1` is REQUIRED (not optional) on every read-only open across this
Windows 9p bind mount — a plain `mode=ro` connect() succeeds (lazy open) but
every subsequent query then throws `sqlite3.OperationalError: unable to open
database file`, because the 9p mount doesn't share locks for the `-shm`
sidecar the way a native filesystem does. This is the exact pattern
bifrost/auth_autoheal.py's `key_provider_map()` already carries; lifted
verbatim rather than rediscovered.

infractl performs no direct writes here: provider deregistration happens in
bifrost/sync_vk_allowlists.py (`deregister_absent_providers()`), which the
binding restart ladder runs with shared-bifrost stopped."""
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


def providers_with_keys(db_path: Path) -> set[str]:
    """Providers that have at least one `config_keys` row. A provider in
    config.json with no row here is one Bifrost has not imported yet (an
    unpark, or a rollback of a park): the VK sync can only grant allowlists
    for providers it finds in config_keys, so the restart ladder runs a
    second stop -> sync -> start cycle after Bifrost's own import creates
    the rows (infractl/bifrost/restart.py)."""
    if not db_path.exists():
        return set()
    con = _ro_connect(db_path)
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT provider FROM config_keys")}
    finally:
        con.close()
