#!/usr/bin/env python3
"""Render bifrost/config.snapshot.redacted.json -- the reviewable, committable
view of the live Bifrost routing config.

Sources: bifrost/config.json (declarative seed) + bifrost/config.db (runtime
mirror: virtual keys, per-VK provider allowlists, budgets, rate limits).
Output carries NO key material: provider keys are reported only as the env var
name they resolve from, virtual keys only by name/id/active/allowlists.

Why: config.db (28 MB SQLite) was tracked in git for four months and committed a
live virtual key to GitHub; untracking it removed the only history of the
routing config. This file restores that history without the secrets.

This gate would pass trivially if the exporter emitted an empty structure; the
`assert` at the bottom refuses to write a snapshot with zero providers or zero
virtual keys.

Usage: python scripts/export_config_snapshot.py [--check]
  --check  exit 1 if the on-disk snapshot differs from a fresh render.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIFROST = ROOT / "bifrost"
CONFIG_JSON = Path(os.environ.get("BIFROST_CONFIG_JSON", BIFROST / "config.json"))
CONFIG_DB = Path(os.environ.get("BIFROST_CONFIG_DB", BIFROST / "config.db"))
DISABLED_JSON = BIFROST / "disabled-providers.json"
OUT = BIFROST / "config.snapshot.redacted.json"


def _key_ref(value: object) -> str:
    v = str(value or "")
    if v.startswith("env."):
        return v
    return "<literal>" if v else "<empty>"


def render() -> dict:
    cfg = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    providers = {}
    for name, block in cfg.get("providers", {}).items():
        keys = block.get("keys", [])
        providers[name] = {
            "base_url": (block.get("network_config") or {}).get("base_url"),
            "list_models": block.get("list_models"),
            "keys": [
                {
                    "name": k.get("name"),
                    "value": _key_ref(k.get("value")),
                    "models": sorted(k.get("models", [])),
                    "aliases": dict(sorted((k.get("aliases") or {}).items())),
                    "weight": k.get("weight"),
                }
                for k in keys
            ],
            "concurrency_and_buffer_size": block.get("concurrency_and_buffer_size"),
        }
    disabled = sorted(json.loads(DISABLED_JSON.read_text(encoding="utf-8")).get("providers", {}).keys()) if DISABLED_JSON.exists() else []

    vks: list[dict] = []
    if CONFIG_DB.exists():
        con = sqlite3.connect(f"file:{CONFIG_DB}?mode=ro", uri=True)
        try:
            for vid, vname, active in con.execute(
                "SELECT id, name, is_active FROM governance_virtual_keys ORDER BY name"
            ):
                pcs = con.execute(
                    "SELECT provider, allow_all_keys, allowed_models FROM governance_virtual_key_provider_configs "
                    "WHERE virtual_key_id=? ORDER BY provider",
                    (vid,),
                ).fetchall()
                vks.append(
                    {
                        "name": vname,
                        "id": vid,
                        "is_active": bool(active),
                        "providers": [
                            {
                                "provider": p,
                                "allow_all_keys": bool(a),
                                "allowed_models": len(json.loads(m)) if m else 0,
                            }
                            for p, a, m in pcs
                        ],
                    }
                )
        finally:
            con.close()

    snap = {
        "_generated_by": "scripts/export_config_snapshot.py",
        "_note": "Redacted view of bifrost/config.json + config.db. No key material. Regenerate after any config change.",
        "providers": providers,
        "disabled_providers": disabled,
        "virtual_keys": vks,
        "client": cfg.get("client"),
    }
    assert snap["providers"], "snapshot would have zero providers -- refusing to write"
    assert not CONFIG_DB.exists() or snap["virtual_keys"], "config.db present but zero virtual keys read -- refusing"
    return snap


def main() -> int:
    snap = render()
    text = json.dumps(snap, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"DRIFT: {OUT} is stale; run scripts/export_config_snapshot.py")
            return 1
        print("snapshot up to date")
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(snap['providers'])} providers, {len(snap['virtual_keys'])} virtual keys, {len(snap['disabled_providers'])} parked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
