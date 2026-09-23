from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from infractl.settings import Settings


@pytest.fixture
def tmp_bifrost_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bifrost"
    d.mkdir()
    config = {
        "providers": {
            "vllm-local": {
                "base_url": "http://vllm-chat:8000",
                "keys": [{"name": "k1", "models": ["qwen3-chat"], "weight": 1}],
            },
            "mistral": {
                "base_url": "https://api.mistral.ai",
                "keys": [{"name": "k1", "models": ["mistral-large"], "weight": 1}],
            },
        }
    }
    (d / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (d / "disabled-providers.json").write_text(json.dumps({"providers": {}}, indent=2), encoding="utf-8")
    return d


@pytest.fixture
def tmp_config_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "config.db"
    con = sqlite3.connect(str(db_path))
    con.executescript(
        """
        CREATE TABLE config_providers (name TEXT PRIMARY KEY);
        CREATE TABLE config_keys (id INTEGER PRIMARY KEY, provider TEXT);
        CREATE TABLE governance_virtual_keys (id TEXT PRIMARY KEY, name TEXT, is_active INTEGER, value TEXT);
        CREATE TABLE governance_virtual_key_provider_configs (
            id INTEGER PRIMARY KEY, virtual_key_id TEXT, provider TEXT,
            allow_all_keys INTEGER, allowed_models TEXT
        );
        CREATE TABLE governance_virtual_key_provider_config_keys (
            id INTEGER PRIMARY KEY, table_virtual_key_provider_config_id INTEGER
        );
        CREATE TABLE governance_model_pricing (id INTEGER PRIMARY KEY, provider TEXT);
        INSERT INTO config_providers VALUES ('vllm-local'), ('mistral');
        INSERT INTO governance_virtual_keys VALUES
            ('vk-1', 'claude-code-local', 1, 'sk-bf-testvalue000000000000000000000000000');
        INSERT INTO governance_virtual_key_provider_configs (virtual_key_id, provider, allow_all_keys)
            VALUES ('vk-1', 'vllm-local', 1), ('vk-1', 'mistral', 1);
        """
    )
    con.commit()
    con.close()
    return db_path


@pytest.fixture
def settings(tmp_path: Path, tmp_bifrost_dir: Path) -> Settings:
    return Settings(
        INFRACTL_TOKEN="test-token-not-a-secret",
        INFRACTL_BIFROST_DIR=str(tmp_bifrost_dir),
        INFRACTL_STATE_DIR=str(tmp_path / "state"),
        INFRACTL_REPORTS_DIR=str(tmp_path / "reports"),
        INFRACTL_OBSERVABILITY_DIR=str(tmp_path / "observability"),
        INFRACTL_CONSUMERS_YAML=str(tmp_path / "consumers.yaml"),
        INFRACTL_WRITE_MODE="dry_run",
    )
