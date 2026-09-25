"""Synthetic probes must skip providers the probe VK cannot reach, and probe
embedding lanes with /v1/embeddings (2026-09-25: a 403 on openrouter and a
4xx on embed-local were both counted as "responsive")."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from infractl.bifrost import admin_api
from infractl.probes.lanes import load_probe_lanes, vk_allowed_providers


def _cfg(tmp: Path) -> Path:
    p = tmp / "config.json"
    p.write_text(
        json.dumps(
            {
                "providers": {
                    "groq": {"keys": [{"name": "g", "models": ["openai/gpt-oss-20b"]}]},
                    "openrouter": {"keys": [{"name": "o", "models": ["openrouter/free"]}]},
                    "embed-local": {"keys": [{"name": "e", "models": ["Qwen/Qwen3-Embedding-0.6B"]}]},
                }
            }
        ),
        encoding="utf-8",
    )
    return p


def _db(tmp: Path) -> Path:
    p = tmp / "config.db"
    db = sqlite3.connect(p)
    db.executescript(
        """
        CREATE TABLE governance_virtual_keys (id TEXT, value TEXT);
        CREATE TABLE governance_virtual_key_provider_configs (virtual_key_id TEXT, provider TEXT);
        INSERT INTO governance_virtual_keys VALUES ('vk1', 'sk-bf-probe');
        INSERT INTO governance_virtual_key_provider_configs VALUES ('vk1','groq'), ('vk1','embed-local');
        """
    )
    db.commit()
    db.close()
    return p


def test_vk_allowed_providers_reads_governance(tmp_path):
    assert vk_allowed_providers(_db(tmp_path), "sk-bf-probe") == {"groq", "embed-local"}


def test_unknown_vk_or_missing_db_means_probe_everything(tmp_path):
    assert vk_allowed_providers(_db(tmp_path), "sk-bf-other") is None
    assert vk_allowed_providers(tmp_path / "nope.db", "sk-bf-probe") is None


def test_disallowed_provider_is_skipped_not_probed(tmp_path):
    lanes, skipped = load_probe_lanes(_cfg(tmp_path), {"groq", "embed-local"})
    assert {ln["model"].split("/", 1)[0] for ln in lanes} == {"groq", "embed-local"}
    assert skipped == {"openrouter": "vk_not_allowed"}
    embed = next(ln for ln in lanes if ln["model"].startswith("embed-local/"))
    assert embed["kind"] == "embed"


def test_no_filter_keeps_every_provider(tmp_path):
    lanes, skipped = load_probe_lanes(_cfg(tmp_path))
    assert len(lanes) == 3 and skipped == {}


def test_synthetic_embedding_requires_a_vector(monkeypatch):
    class _Resp:
        status = 200

        def __init__(self, body: dict):
            self._b = json.dumps(body).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        admin_api.urllib.request, "urlopen", lambda req, timeout: _Resp({"data": [{"embedding": [0.1] * 4}]})
    )
    ok, detail = admin_api.synthetic_embedding("http://x", "vk", "embed-local/m")
    assert ok and "dim=4" in detail
    monkeypatch.setattr(admin_api.urllib.request, "urlopen", lambda req, timeout: _Resp({"data": []}))
    ok, _ = admin_api.synthetic_embedding("http://x", "vk", "embed-local/m")
    assert not ok
