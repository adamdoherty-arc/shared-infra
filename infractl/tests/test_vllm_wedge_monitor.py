"""Replay tests for vllm_wedge_monitor's pure detectors.

This gate would pass trivially if the monitor module were imported with a
SATURATED_MIN_RUNNING_FRACTION env override in the test process, so the
tests reload the module with a scrubbed environment.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def monitor(monkeypatch):
    for k in ("VLLM_MAX_SEQS", "SATURATED_MIN_RUNNING_FRACTION", "SATURATED_MIN_TOKENS_PER_REQUEST_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.syspath_prepend(str(ROOT))
    sys.modules.pop("vllm_wedge_monitor", None)
    return importlib.import_module("vllm_wedge_monitor")


@pytest.mark.parametrize("running,gen_rate", [(20, 1.9), (24, 2.2), (23, 0.9), (27, 1.47)])
def test_2026_09_21_half_full_engine_at_two_tokens_per_second_is_saturated_starved(monitor, running, gen_rate):
    assert monitor.is_saturated_starved_sample(running, gen_rate) is True


@pytest.mark.parametrize("running,gen_rate", [(2, 2.0), (8, 4.0), (24, 80.0), (32, 75.0)])
def test_healthy_or_lightly_loaded_engine_is_not_saturated_starved(monitor, running, gen_rate):
    assert monitor.is_saturated_starved_sample(running, gen_rate) is False


def test_fraction_default_is_half_the_seq_slots(monitor):
    assert monitor.SATURATED_MIN_RUNNING_FRACTION == 0.5
    assert monitor.VLLM_MAX_SEQS == 32
