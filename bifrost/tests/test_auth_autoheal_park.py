"""bifrost-autoheal parks through infractl (the single config writer) and no
longer writes config.json / disabled-providers.json / config.db itself.

Run: python -m pytest bifrost/tests/test_auth_autoheal_park.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "auth_autoheal.py"


@pytest.fixture
def autoheal(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOHEAL_BIFROST_DIR", str(tmp_path))
    monkeypatch.setenv("INFRACTL_TOKEN", "test-token")
    monkeypatch.setenv("AUTOHEAL_INFRACTL_URL", "http://infractl.test:8095")
    spec = importlib.util.spec_from_file_location("auth_autoheal_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_request_park_posts_to_infractl_with_token_and_requester(autoheal, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        return _Resp(json.dumps({"ok": True, "data": {"action_id": "abc", "summary": "parked groq"}}).encode())

    monkeypatch.setattr(autoheal.urllib.request, "urlopen", fake_urlopen)
    data = autoheal.request_park("groq", 9)
    assert data["action_id"] == "abc"
    assert seen["url"] == "http://infractl.test:8095/api/config/providers/groq/park"
    assert seen["headers"]["x-infractl-token"] == "test-token"
    assert seen["body"]["requested_by"] == "bifrost-autoheal"
    assert seen["body"]["dry_run"] is False
    assert seen["timeout"] >= 600  # outlives the binding restart + a rollback restart


def test_refusal_is_reported_and_nothing_is_written(autoheal, monkeypatch, tmp_path):
    def fake_urlopen(req, timeout):
        body = json.dumps({"ok": False, "error": {"code": "cooldown", "message": "in cooldown"}}).encode()
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(body))

    monkeypatch.setattr(autoheal.urllib.request, "urlopen", fake_urlopen)
    assert autoheal.park("groq", 9) is False
    log = (tmp_path / "autoheal.log").read_text(encoding="utf-8")
    assert "could not park provider 'groq'" in log and "cooldown" in log
    assert not (tmp_path / "config.json").exists()
    assert not (tmp_path / "disabled-providers.json").exists()


def test_missing_token_fails_closed(autoheal, monkeypatch):
    monkeypatch.setattr(autoheal, "INFRACTL_TOKEN", "")
    with pytest.raises(autoheal.ParkRequestError, match="INFRACTL_TOKEN"):
        autoheal.request_park("groq", 9)


def test_no_local_config_write_path_remains(autoheal):
    for gone in ("_move_block_to_disabled", "_deregister", "_run_sync", "_snapshot"):
        assert not hasattr(autoheal, gone), f"{gone} is a second config writer; parks go through infractl"
