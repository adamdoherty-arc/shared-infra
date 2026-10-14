"""The heavy lane runs a request that names files before a queued whole tier, and never starves the whole tier.

ADA's single heavy lane held `ada:full` for over 90 minutes on 2026-10-14 and every `live_db --paths <file>` request an
agent made waited behind it, then behind memory_heavy and the whole live_db tier queued ahead of it, one of them until
"ada test lock still held by another run after 5400s". The run that is live cannot be preempted, but the order of the
queue behind it can be: a request naming files takes minutes, a whole tier takes twenty minutes to over an hour.

These tests would pass trivially if the ranking were never read (a short ticket queued last would still wait for the
older whole tier: the first case), if it ignored age (a whole tier that has waited past SHORT_FIRST_AGING_S must go
ahead of a newer short request: the second case, with a young whole tier as its control), or if the light lane were
ranked too (the last case reads the same tickets in a light lane and demands plain first come first served).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib.lock import HEAVY, LIGHT, SHORT_FIRST_AGING_S, ProjectLock  # noqa: E402

WHOLE_TIER = json.dumps(["memory_heavy", []])
SHORT_REQUEST = json.dumps(["live_db", ["backend/tests/test_x.py"]])


def _queue(tmp_path: Path, lane: str, tickets: list[tuple[str, str, float]]) -> tuple[ProjectLock, Path, list[Path]]:
    lock = ProjectLock("p", tmp_path, lane=lane, slots=1)
    queue = tmp_path / ("p.queue" if lane == HEAVY else "p.light.queue")
    queue.mkdir()
    paths = []
    for index, (group, key, waited_s) in enumerate(tickets):
        path = queue / f"{index:020d}-{group}"
        path.write_text(json.dumps({"pid": os.getpid(), "group": group, "key": key,
                                    "queued_at": time.time() - waited_s}), encoding="utf-8")
        paths.append(path)
    return lock, queue, paths


def test_a_short_request_queued_last_runs_before_a_younger_whole_tier(tmp_path):
    lock, queue, (whole, short) = _queue(tmp_path, HEAVY, [("sched", WHOLE_TIER, 60.0), ("agent", SHORT_REQUEST, 5.0)])
    assert lock._is_next(queue, short)
    assert not lock._is_next(queue, whole)
    assert lock.queue_position(short) == 1
    assert lock.queue_position(whole) == 2


def test_a_whole_tier_that_has_waited_past_the_aging_limit_goes_ahead_of_a_newer_short_request(tmp_path):
    lock, queue, (whole, short) = _queue(
        tmp_path, HEAVY, [("sched", WHOLE_TIER, SHORT_FIRST_AGING_S + 30.0), ("agent", SHORT_REQUEST, 5.0)])
    assert lock._is_next(queue, whole)
    assert not lock._is_next(queue, short)


def test_control_two_whole_tiers_stay_first_come_first_served(tmp_path):
    lock, queue, (older, newer) = _queue(tmp_path, HEAVY, [("a", WHOLE_TIER, 60.0), ("b", json.dumps(["live_db", []]), 30.0)])
    assert lock._is_next(queue, older)
    assert not lock._is_next(queue, newer)


def test_control_the_light_lane_is_not_ranked(tmp_path):
    lock, queue, (first, second) = _queue(tmp_path, LIGHT, [("a", WHOLE_TIER, 60.0), ("b", SHORT_REQUEST, 5.0)])
    assert lock._is_next(queue, first)
    assert not lock._is_next(queue, second)


def test_a_ticket_with_an_unreadable_key_ranks_as_a_whole_tier(tmp_path):
    lock, queue, (odd, short) = _queue(tmp_path, HEAVY, [("a", "not json", 10.0), ("b", SHORT_REQUEST, 5.0)])
    assert lock._is_next(queue, short)
    assert not lock._is_next(queue, odd)
