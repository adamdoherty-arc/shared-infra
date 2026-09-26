"""Cooldown / max-per-day / T3-approval gating in core/actions.execute().
These paths all short-circuit BEFORE any lock acquisition or real docker/
bifrost work, so they're testable with just a Ledger + Settings — no
running docker socket or bifrost stack required."""
from __future__ import annotations

from pathlib import Path

import pytest

from infractl.core import actions as actions_mod
from infractl.core.ledger import Ledger
from infractl.settings import Settings


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "infractl.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        INFRACTL_TOKEN="test-token", INFRACTL_BIFROST_DIR=str(tmp_path / "bifrost"),
        INFRACTL_STATE_DIR=str(tmp_path / "state"), INFRACTL_WRITE_MODE="dry_run",
    )


@pytest.mark.asyncio
async def test_unknown_kind_raises(ledger, settings):
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.execute(ledger, settings, "not_a_real_kind", {}, "tester", "why")
    assert exc_info.value.code == "unknown_kind"


@pytest.mark.asyncio
async def test_cooldown_blocks_repeat_within_window(ledger, settings):
    kind = "vk_resync"
    ledger.record_heal_event(kind, "test", "executed", "simulated prior run")
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.execute(ledger, settings, kind, {}, "tester", "why")
    assert exc_info.value.code == "cooldown"


@pytest.mark.asyncio
async def test_max_per_day_blocks_after_cap(ledger, settings):
    kind = "logsdb_quick_check"
    spec = actions_mod.REGISTRY[kind]
    # Insert max_per_day heal events spread far enough apart to each clear
    # cooldown individually, only the daily cap should trip.
    import time
    now = time.time()
    for i in range(spec.max_per_day):
        ledger.con.execute(
            "INSERT INTO heal_events (rule, probe_name, outcome, detail, ts) VALUES (?,?,?,?,?)",
            (kind, "test", "executed", "simulated", now - spec.cooldown_s * (i + 2)),
        )
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.execute(ledger, settings, kind, {}, "tester", "why")
    assert exc_info.value.code == "max_per_day"


@pytest.mark.asyncio
async def test_t3_without_confirm_creates_pending_approval_row(ledger, settings):
    result = await actions_mod.execute(
        ledger, settings, "restart_qwen38_chat", {}, "tester", "why", confirm=False,
    )
    assert result["status"] == "pending_approval"
    row = ledger.get_action(result["action_id"])
    assert row["status"] == "pending_approval"
    assert row["tier"] == "T3"


@pytest.mark.asyncio
async def test_approve_rejects_non_pending_action(ledger, settings):
    action_id = ledger.create_action("restart_qwen38_chat", "T3", {}, "tester", "why")
    ledger.update_action(action_id, status="succeeded")
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.approve(ledger, settings, action_id)
    assert exc_info.value.code == "invalid_state"


@pytest.mark.asyncio
async def test_approve_unknown_action_raises_not_found(ledger, settings):
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.approve(ledger, settings, "does-not-exist")
    assert exc_info.value.code == "not_found"


def test_t3_kinds_not_registered_have_no_run_handler():
    """NEVER SHIP STUBS: vk_budget_set/vk_rotate/compose_apply/model_swap
    are explicitly NOT in the registry until their mechanics exist."""
    for unimplemented in ("vk_budget_set", "vk_rotate", "compose_apply", "model_swap"):
        assert unimplemented not in actions_mod.REGISTRY


def test_api_dry_run_does_not_start_cooldown(ledger):
    """An operator's API preview must not block the real run right after it."""
    ledger.record_heal_event("alias_set", "api", "dry_run", "preview")
    assert ledger.last_heal_event_ts("alias_set") is None
    assert ledger.heal_events_today("alias_set") == 0


def test_heal_rule_dry_run_and_real_runs_still_count(ledger):
    """A heal rule in dry-run mode paces like the live rule; real runs always count."""
    ledger.record_heal_event("vk_resync", "lanes", "dry_run", "would resync")
    ledger.record_heal_event("vk_resync", "api", "executed", "ran")
    assert ledger.last_heal_event_ts("vk_resync") is not None
    assert ledger.heal_events_today("vk_resync") == 2
