from __future__ import annotations

from pathlib import Path

from infractl.core.ledger import Ledger


def test_schema_creates_all_tables(tmp_path: Path):
    ledger = Ledger(tmp_path / "infractl.db")
    tables = {
        r[0] for r in ledger.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "actions", "probe_runs", "heal_events", "lane_health", "alerts_seen",
        "brain_usage", "improvement_ledger", "snapshots",
    }
    assert expected <= tables
    ledger.close()


def test_schema_init_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "infractl.db"
    Ledger(db_path).close()
    Ledger(db_path).close()  # re-opening + re-running init_schema must not raise


def test_create_action_and_get_action_round_trip(tmp_path: Path):
    ledger = Ledger(tmp_path / "infractl.db")
    action_id = ledger.create_action(
        "vk_resync", "T1", {"x": 1}, "tester", "why", dry_run=True,
    )
    row = ledger.get_action(action_id)
    assert row is not None
    assert row["kind"] == "vk_resync"
    assert row["status"] == "pending"
    assert row["dry_run"] == 1
    ledger.close()


def test_update_action_and_list_actions(tmp_path: Path):
    ledger = Ledger(tmp_path / "infractl.db")
    action_id = ledger.create_action("wal_checkpoint", "T1", {}, "tester", "why")
    ledger.update_action(action_id, status="succeeded")
    rows = ledger.list_actions(status="succeeded")
    assert any(r["id"] == action_id for r in rows)
    ledger.close()


def test_probe_streak_red(tmp_path: Path):
    ledger = Ledger(tmp_path / "infractl.db")
    for _ in range(3):
        ledger.record_probe("bifrost", False, "down", 5.0)
    assert ledger.probe_streak_red("bifrost", 3) is True
    ledger.record_probe("bifrost", True, "up", 5.0)
    assert ledger.probe_streak_red("bifrost", 3) is False
    ledger.close()


def test_alert_dedupe(tmp_path: Path):
    ledger = Ledger(tmp_path / "infractl.db")
    assert ledger.alert_recently_sent("fp1", 3600) is False
    ledger.record_alert_sent("fp1", "bifrost")
    assert ledger.alert_recently_sent("fp1", 3600) is True
    ledger.close()


def test_heal_events_today_and_cooldown(tmp_path: Path):
    ledger = Ledger(tmp_path / "infractl.db")
    assert ledger.heal_events_today("vk_resync") == 0
    assert ledger.last_heal_event_ts("vk_resync") is None
    ledger.record_heal_event("vk_resync", "config_parity", "executed", "ok")
    assert ledger.heal_events_today("vk_resync") == 1
    assert ledger.last_heal_event_ts("vk_resync") is not None
    ledger.close()
