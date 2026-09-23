"""Live tests against a running shared-infra-control container. Requires
INFRACTL_URL (default http://127.0.0.1:8095) + INFRACTL_TOKEN in the
environment. Excluded from the default run (`pytest -m "not live"`); run
explicitly with `pytest -m live`."""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("INFRACTL_URL", "http://127.0.0.1:8095")
TOKEN = os.environ.get("INFRACTL_TOKEN", "")


def _headers():
    return {"X-Infractl-Token": TOKEN}


def test_healthz_open_no_auth():
    resp = httpx.get(f"{BASE_URL}/healthz", timeout=10)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_metrics_open_no_auth():
    resp = httpx.get(f"{BASE_URL}/metrics", timeout=10)
    assert resp.status_code == 200
    assert "infractl_probe_ok" in resp.text


def test_api_requires_token():
    resp = httpx.get(f"{BASE_URL}/api/status", timeout=10)
    assert resp.status_code == 401


def test_api_status_with_token():
    assert TOKEN, "INFRACTL_TOKEN must be set for live tests"
    resp = httpx.get(f"{BASE_URL}/api/status", headers=_headers(), timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["write_mode"] in ("dry_run", "apply")


def test_api_health_runs_probes():
    resp = httpx.get(f"{BASE_URL}/api/health", headers=_headers(), timeout=30)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["data"]["probes"]) >= 10


def test_api_consumers_matches_yaml():
    resp = httpx.get(f"{BASE_URL}/api/consumers", headers=_headers(), timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    names = {c["name"] for c in body["data"]}
    assert "ada" in names
    assert "claude-code-local" in names


def test_api_config_snapshot_never_leaks_secret():
    resp = httpx.get(f"{BASE_URL}/api/config/snapshot", headers=_headers(), timeout=10)
    assert resp.status_code == 200
    assert "sk-bf-" not in resp.text
