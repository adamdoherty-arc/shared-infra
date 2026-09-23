"""NEVER PRINT SECRET VALUES — verifies the redacted snapshot and the VK
listing never leak a raw `sk-bf-...` virtual-key value, even though the
fixture config.db has one sitting right there in `governance_virtual_keys
.value`."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from infractl.bifrost import config as bifrost_config
from infractl.bifrost import configdb

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _vendored_scripts_dir(monkeypatch):
    # In the built image this is /app/vendor/scripts (Dockerfile COPY); for
    # a host/dev test run it's the real scripts/ dir at the repo root —
    # same file either way, never duplicated logic (see bifrost/config.py's
    # module docstring).
    monkeypatch.setenv("INFRACTL_VENDORED_SCRIPTS_DIR", str(REPO_ROOT / "scripts"))
    import sys
    for mod_name in list(sys.modules):
        if mod_name == "export_config_snapshot":
            del sys.modules[mod_name]
    yield


def test_redacted_snapshot_never_contains_raw_vk_value(tmp_bifrost_dir: Path, tmp_config_db: Path):
    # tmp_config_db's fixture VK value is 'sk-bf-testvalue000...' — copy it
    # alongside config.json so render_redacted_snapshot sees both.
    import shutil
    shutil.copy2(tmp_config_db, tmp_bifrost_dir / "config.db")

    snap = bifrost_config.render_redacted_snapshot(tmp_bifrost_dir)
    text = json.dumps(snap)
    assert "sk-bf-testvalue" not in text
    assert "sk-bf-" not in text
    # the VK is still represented, just by name/id, proving this isn't an
    # empty/gutted snapshot passing the assertion trivially
    vk_names = [v["name"] for v in snap["virtual_keys"]]
    assert "claude-code-local" in vk_names


def test_configdb_list_virtual_keys_never_returns_raw_value(tmp_config_db: Path):
    vks = configdb.list_virtual_keys(tmp_config_db)
    assert len(vks) == 1
    vk = vks[0]
    assert set(vk.keys()) == {"id", "name", "is_active", "sha256_prefix"}
    assert "sk-bf-" not in json.dumps(vk)
    assert len(vk["sha256_prefix"]) == 16
    # a real sha256 prefix, not a truncated raw value
    import hashlib
    expected = hashlib.sha256(b"sk-bf-testvalue000000000000000000000000000").hexdigest()[:16]
    assert vk["sha256_prefix"] == expected
