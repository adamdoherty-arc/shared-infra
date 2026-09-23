"""Live 2026-09-15 finding: execute()'s outer exception handler
unconditionally overwrote a T2 action's ledger status back to "failed"
even when `_execute_locked`'s own rollback branch had already set the more
specific "rolled_back" — a real rollback (config restored, gateway
re-verified) reported in the ledger as an indistinguishable generic
failure. Reproduced here by monkeypatching only the network-facing edges
(docker socket calls, the restart ladder, vk_sync) — the actions.py
orchestration logic itself (lock, snapshot, apply, exception handling,
ledger writes) runs for real, unmocked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from infractl.core import actions as actions_mod
from infractl.core.ledger import Ledger
from infractl.settings import Settings


@pytest.fixture
def bifrost_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bifrost"
    d.mkdir()
    cfg = {"providers": {"aion": {"keys": [{"name": "k1", "models": ["m1"]}]}}}
    (d / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    (d / "disabled-providers.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
    return d


@pytest.fixture
def settings(tmp_path: Path, bifrost_dir: Path) -> Settings:
    return Settings(
        INFRACTL_TOKEN="test-token", INFRACTL_BIFROST_DIR=str(bifrost_dir),
        INFRACTL_STATE_DIR=str(tmp_path / "state"), INFRACTL_WRITE_MODE="apply",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "infractl.db")


@pytest.mark.asyncio
async def test_rollback_status_survives_outer_exception_handler(ledger, settings, monkeypatch):
    monkeypatch.setattr(actions_mod.docker_client, "stop", lambda *a, **k: None)
    monkeypatch.setattr(actions_mod.docker_client, "start", lambda *a, **k: None)
    monkeypatch.setattr(actions_mod.vk_sync, "run_vk_sync", lambda *a, **k: "ok")
    monkeypatch.setattr(
        actions_mod.restart, "restart_bifrost",
        lambda *a, **k: {"healthy": True, "steps": [], "preflight": {}},
    )
    # verify_gateway is called twice in the failure path: once to decide
    # the action failed, once again after rollback to confirm recovery —
    # both return "gateway is fine" so the test isolates the STATUS BUG,
    # not gateway recovery itself.
    monkeypatch.setattr(actions_mod, "verify_gateway", lambda s: (False, {"health": "simulated failure"}))

    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.execute(
            ledger, settings, "models_add",
            {"provider": "aion", "key_name": "k1", "models": ["m2"]},
            requested_by="tester", reason="rollback status regression test",
        )
    assert exc_info.value.code == "rolled_back"

    action_id = ledger.list_actions()[0]["id"]
    row = ledger.get_action(action_id)
    assert row["status"] == "rolled_back", (
        f"expected 'rolled_back' to survive execute()'s outer handler, got '{row['status']}'"
    )
