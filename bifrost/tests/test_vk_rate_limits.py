"""Per-consumer VK rate caps (bifrost/vk-rate-limits.json), 2026-09-25."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("avrl", ROOT / "scripts" / "apply_vk_rate_limits.py")
avrl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(avrl)  # type: ignore[union-attr]


def test_every_cap_is_positive_and_ada_is_above_its_observed_peak():
    limits, reset = avrl.load_limits()
    assert reset == "1m"
    assert all(v > 0 for v in limits.values())
    assert limits["ada-prod"] > 548  # observed 24h max/min on 2026-09-25


def test_plan_updates_missing_or_wrong_caps_and_flags_unknown_vks():
    vks = [
        {"id": "1", "name": "ada-prod", "rate_limit": {"request_max_limit": 900, "request_reset_duration": "1m"}},
        {"id": "2", "name": "legion-prod", "rate_limit": None},
        {"id": "3", "name": "new-vk", "rate_limit": None},
    ]
    updates, problems = avrl.plan(vks, {"ada-prod": 900, "legion-prod": 120}, "1m")
    assert updates == [("2", "legion-prod", 120)]
    assert problems == ["new-vk: no cap in bifrost/vk-rate-limits.json"]


@pytest.mark.live
def test_live_gateway_matches_the_file():
    updates, problems = avrl.plan(avrl._vks(), *avrl.load_limits())
    assert not updates and not problems
