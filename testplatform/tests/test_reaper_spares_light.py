"""The heavy-run orphan reaper kills whole-suite pytest left by a dead runner and never a live light run.

ada:changed run 14684 (2026-10-14) selected enough tests that testctl handed pytest the `backend/tests` directory plus an
allowlist, so its command line carried the same words as a whole-suite run. The memory_heavy run that started four
seconds after `ada:full` timed out reaped it (exit 143, no report). A whole-suite run always roots at the snapshot.

This would pass trivially if the pattern were tested only against the heavy command line (the light allowlist command
line below must not match), if the pattern ignored the snapshot root (the first case reads the pattern the reaper
really sends), or if a profile without a snapshot lost reaping altogether (the control keeps the old pattern).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import runner  # noqa: E402

HEAVY = ("timeout -s TERM -k 20 5400 python -m pytest backend/tests --rootdir=/snap -c backend/tests/pytest.ini "
         "--timeout=120 -n 2 --dist loadfile -m not live_db")
LIGHT_ALLOWLIST = ("timeout -s TERM -k 20 1800 python -m pytest backend/tests --rootdir=/app -c backend/tests/pytest.ini "
                   "--timeout=120 -p no:testmon")


def _pattern_sent(monkeypatch, runtime):
    sent = []

    def fake_run(cmd, timeout, *args, **kwargs):
        sent.append(cmd)
        return 1, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    runner.reap_orphaned_heavy_pytest(runtime)
    return sent[0][-1]


def test_the_pattern_matches_a_snapshot_rooted_whole_suite_run_and_spares_a_light_allowlist_run(monkeypatch):
    pattern = _pattern_sent(monkeypatch, {"container": "c", "snapshot": {"dest": "/snap"}})
    assert re.search(pattern, HEAVY)
    assert not re.search(pattern, LIGHT_ALLOWLIST)


def test_control_a_runtime_without_a_snapshot_keeps_the_original_pattern(monkeypatch):
    pattern = _pattern_sent(monkeypatch, {"container": "c"})
    assert re.search(pattern, LIGHT_ALLOWLIST)
    assert re.search(pattern, HEAVY)
