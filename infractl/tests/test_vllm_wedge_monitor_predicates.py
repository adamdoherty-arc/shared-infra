"""Unit tests for vllm_wedge_monitor's pure predicates.

Fix-1100000608 (2026-09-21): the "saturated-starved" detector added
alongside `startup_suppression` -- Running near MAX_SEQS with per-request
throughput below a floor, replayed against the actual incident numbers
(Running 29-32, generation oscillating ~75/0.0 tok/s) instead of a live
engine, per the owner directive not to restart qwen38-chat as part of this
change.

This gate would pass trivially if it asserted only on the TOTAL gen_rate
(exactly the OLD decode-starved detector's blind spot, per the module's own
2026-09-21 comment) -- every assertion below divides by `running` and checks
`Running >= 0.9*MAX_SEQS` is itself required, not incidental.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent / "vllm_wedge_monitor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("vllm_wedge_monitor", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vllm_wedge_monitor"] = module
    spec.loader.exec_module(module)
    return module


m = _load_module()


def test_control_low_concurrency_low_throughput_is_not_starved() -> None:
    """Two requests at 2 tok/s each is healthy -- nothing is queued behind
    a lightly-loaded engine, so this must NOT trip even though the raw
    per-request math (2/2=1.0) sits right at the floor."""
    assert m.is_saturated_starved_sample(running=2, gen_rate=2.0) is False


def test_control_high_throughput_at_full_concurrency_is_not_starved() -> None:
    assert m.is_saturated_starved_sample(running=32, gen_rate=1600.0) is False


def test_sabotage_incident_replay_running_30_gen_rate_75_is_starved() -> None:
    """The measured 2026-09-21 incident shape: Running 29-32 (>= 0.9*32),
    total generation oscillating to ~75 tok/s -- OLD decode-starved detector
    (floor 1.0 tok/s on the TOTAL) read this as healthy and reset its
    counter. Divided by Running (30), per-request throughput is 2.5 tok/s
    -- also above the 1.0 floor here, so a single 75 tok/s SAMPLE is
    correctly NOT flagged (matches production: no single good poll should
    trip a false positive)."""
    assert m.is_saturated_starved_sample(running=30, gen_rate=75.0) is False


def test_sabotage_incident_replay_running_30_gen_rate_near_zero_is_starved() -> None:
    """The OTHER half of the oscillation -- total generation near 0.0 while
    Running stays pinned near MAX_SEQS. This is the actual overload sample:
    per-request throughput 0.5/30 ~ 0.017 tok/s, far below the 1.0 floor."""
    assert m.is_saturated_starved_sample(running=30, gen_rate=0.5) is True


def test_control_running_below_saturation_threshold_never_trips() -> None:
    """Even zero generation is not "saturated-starved" when Running is well
    below MAX_SEQS -- that shape belongs to the existing hard-stall/
    decode-starved detectors, not this one."""
    assert m.is_saturated_starved_sample(running=5, gen_rate=0.0) is False


def test_running_zero_never_trips() -> None:
    assert m.is_saturated_starved_sample(running=0, gen_rate=0.0) is False


def test_boundary_at_exactly_ninety_percent_of_max_seqs() -> None:
    # 0.9 * 32 = 28.8 -- 29 is the first integer Running that qualifies.
    assert m.is_saturated_starved_sample(running=29, gen_rate=0.1) is True
    assert m.is_saturated_starved_sample(running=28, gen_rate=0.1) is False
