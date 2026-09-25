"""Operator disable list (bifrost/operator-disabled.json), 2026-09-25.

Operator order: Kimi and Mistral are removed everywhere, and a provider
without a working key stays off until the operator re-enables it. These tests
pin the three enforcement points: the live config is compliant, the VK sync
refuses a non-compliant config and removes absent providers from config.db,
and the freellmapi enforcer disables matching keys and fallback entries.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OD = json.loads((ROOT / "bifrost" / "operator-disabled.json").read_text(encoding="utf-8"))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


sync = _load("sync_vk_allowlists", "bifrost/sync_vk_allowlists.py")
fla = _load("apply_operator_disabled_freellmapi", "scripts/apply_operator_disabled_freellmapi.py")


def test_live_config_has_no_operator_disabled_entries():
    cfg = json.loads((ROOT / "bifrost" / "config.json").read_text(encoding="utf-8"))
    assert sync.operator_violations(cfg) == []


def test_operator_list_covers_the_ordered_removals():
    pats = {p.lower() for p in OD["model_patterns"]}
    assert {"kimi", "mistral"} <= pats
    assert {"mistral", "moonshot", "aion"} <= set(OD["providers"])


def test_violations_flag_models_aliases_and_providers():
    cfg = {
        "providers": {
            "nvidia-nim": {"keys": [{"name": "k", "models": ["moonshotai/kimi-k3", "openai/gpt-oss-20b"]}]},
            "hf-router": {"keys": [{"name": "h", "models": ["x"], "aliases": {"code": "mistralai/Codestral-22B"}}]},
            "mistral": {"keys": [{"name": "m", "models": ["mistral-large-latest"]}]},
        }
    }
    v = sync.operator_violations(cfg)
    assert any("kimi-k3" in x for x in v)
    assert any("Codestral" in x for x in v)
    assert any("provider mistral" in x for x in v)
    assert not any("gpt-oss-20b" in x for x in v)


def test_deregister_absent_providers_removes_rows():
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE config_providers (name TEXT);
        CREATE TABLE config_keys (provider TEXT, name TEXT);
        CREATE TABLE governance_virtual_key_provider_configs (id INTEGER PRIMARY KEY, provider TEXT);
        CREATE TABLE governance_virtual_key_provider_config_keys (table_virtual_key_provider_config_id INTEGER);
        INSERT INTO config_providers VALUES ('groq'), ('aion');
        INSERT INTO config_keys VALUES ('groq','g'), ('aion','a');
        INSERT INTO governance_virtual_key_provider_configs VALUES (1,'groq'), (2,'aion');
        INSERT INTO governance_virtual_key_provider_config_keys VALUES (1), (2);
        """
    )
    counts = sync.deregister_absent_providers(db, {"providers": {"groq": {}}})
    assert counts == {"aion": 1}
    assert [r[0] for r in db.execute("SELECT name FROM config_providers")] == ["groq"]
    assert [r[0] for r in db.execute("SELECT provider FROM config_keys")] == ["groq"]
    assert [
        r[0]
        for r in db.execute(
            "SELECT table_virtual_key_provider_config_id FROM governance_virtual_key_provider_config_keys"
        )
    ] == [1]


def test_freellmapi_plan_disables_matches_and_keeps_live_entries():
    od = {
        "model_patterns": {"kimi": ""},
        "freellmapi_platforms": {"mistral": ""},
        "freellmapi_models": {"sambanova/DeepSeek-V3.1-cb": ""},
    }
    keys = [
        {"id": 1, "platform": "mistral", "enabled": True},
        {"id": 2, "platform": "sambanova", "enabled": True},
        {"id": 3, "platform": "nvidia", "enabled": True},
    ]
    chain = [
        {"modelDbId": 10, "platform": "mistral", "modelId": "mistral-large-latest", "enabled": True, "priority": 1},
        {"modelDbId": 11, "platform": "sambanova", "modelId": "DeepSeek-V3.1-cb", "enabled": True, "priority": 2},
        {"modelDbId": 12, "platform": "nvidia", "modelId": "moonshotai/kimi-k2.6", "enabled": True, "priority": 3},
        {"modelDbId": 13, "platform": "nvidia", "modelId": "openai/gpt-oss-120b", "enabled": True, "priority": 4},
        {"modelDbId": 14, "platform": "cloudflare", "modelId": "@cf/x", "enabled": True, "priority": 5},
        {"modelDbId": 15, "platform": "sambanova", "modelId": "DeepSeek-V3.2", "enabled": True, "priority": 6},
    ]
    kill_keys, kill_entries = fla.plan(od, keys, chain)
    assert [k["id"] for k in kill_keys] == [1]
    assert sorted(e["modelDbId"] for e in kill_entries) == [10, 11, 12, 14]
