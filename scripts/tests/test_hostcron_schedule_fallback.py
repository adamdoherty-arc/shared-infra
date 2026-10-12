"""A schedule.json that stops parsing must not stop every hostcron job.

2026-10-11 20:21 ET an invalid \\escape in schedule.json made _load_schedule
return [] every minute; no job ran until the file was repaired at 03:16 ET
2026-10-12 (416 error lines), so the ADA restart-window runner skipped seven
hourly windows and queued restart intents sat undrained.

This gate would pass trivially if it only checked that a good file loads: it
also corrupts the file after a good load and requires the previous jobs back,
and a control with no prior good load must still return [].
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HOSTCRON_PY = Path(__file__).resolve().parents[1] / "hostcron" / "hostcron.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hostcron_under_test", HOSTCRON_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_corrupt_schedule_keeps_last_good_jobs(tmp_path, monkeypatch):
    mod = _load_module()
    sched = tmp_path / "schedule.json"
    sched.write_text(
        json.dumps({"jobs": [{"name": "a"}, {"name": "b", "enabled": False}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "SCHEDULE_PATH", sched)
    assert [j["name"] for j in mod._load_schedule()] == ["a"]
    sched.write_text('{"jobs": [{"cmd": "C:\\code"}]}', encoding="utf-8")
    assert [j["name"] for j in mod._load_schedule()] == ["a"]


def test_corrupt_schedule_without_prior_load_is_empty(tmp_path, monkeypatch):
    mod = _load_module()
    sched = tmp_path / "schedule.json"
    sched.write_text('{"jobs": [', encoding="utf-8")
    monkeypatch.setattr(mod, "SCHEDULE_PATH", sched)
    assert mod._load_schedule() == []
