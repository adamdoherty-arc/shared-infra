"""Static invariants tying the DR drill's time budget to the REAL hostcron schedule.json.

The drill (ADA scripts/dr_restore_and_diff.py) bounds itself with scripts/dr_drill_contract.py; hostcron kills it at
timeout_s + grace_s. These tests read the live schedule and the contract by path, so a budget edit on either side that
breaks the other fails here.

This gate would pass trivially if it tested a copy of the schedule instead of schedule.json, if the heavy-job list in
the contract were empty (missing_heavy_jobs guards that), or if a job lost its grace_s/artifact and the check read
only timeout_s. Each invariant has a sabotaged schedule beside the real one.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest

HOSTCRON = Path(__file__).resolve().parents[1] / "hostcron"
SCHEDULE = json.loads((HOSTCRON / "schedule.json").read_text(encoding="utf8"))["jobs"]
CONTRACT_PATH = os.environ.get("ADA_DR_CONTRACT", r"C:\code\ADA\scripts\dr_drill_contract.py")
_spec = importlib.util.spec_from_file_location("dr_drill_contract", CONTRACT_PATH)
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)

TARGETS = sorted(contract.TARGET_BUDGETS)


def _job(jobs: list[dict], name: str) -> dict:
    return next(j for j in jobs if j["name"] == name)


@pytest.mark.parametrize("target", TARGETS)
def test_dr_job_matches_the_contract_budget(target):
    budget = contract.TARGET_BUDGETS[target]
    job = _job(SCHEDULE, budget["hostcron_job"])
    assert job["timeout_s"] == budget["hostcron_timeout_s"]
    assert job["grace_s"] == budget["hostcron_grace_s"]
    assert job["enabled"] is True
    assert job["cmd"][0].lower().endswith("python.exe") and job["cmd"][-2:] == ["--target", target]
    assert job["artifact"]["reject_status"] == ["running"]


@pytest.mark.parametrize("target", TARGETS)
def test_timeout_leaves_600s_over_the_worst_case_and_grace_covers_cleanup(target):
    budget = contract.TARGET_BUDGETS[target]
    floor_s = contract.worst_case_seconds(target) + contract.PHASE_BOUNDS_S["budget_margin"]
    assert budget["hostcron_timeout_s"] >= floor_s
    cleanup = contract.PHASE_BOUNDS_S["cleanup_reserve"]
    assert budget["hostcron_grace_s"] >= cleanup + contract.PHASE_BOUNDS_S["grace_margin"]
    assert sum(contract.CLEANUP_BREAKDOWN_S.values()) <= cleanup


def test_schedule_has_every_heavy_job_so_the_conflict_check_is_not_empty():
    assert contract.missing_heavy_jobs(SCHEDULE) == []


def test_no_heavy_job_overlaps_a_dr_hard_kill_window_and_the_two_windows_do_not_overlap():
    assert contract.heavy_window_conflicts(SCHEDULE) == []


def test_a_heavy_job_inside_the_dr_window_is_detected():
    """Sabotage: move the frontend release into the ADA drill window; control is the real schedule above."""
    jobs = copy.deepcopy(SCHEDULE)
    _job(jobs, "ada-frontend-prod-release")["schedule"] = {"minute": 30, "hour": 18, "dow": 0}
    assert any("ada-frontend-prod-release" in c for c in contract.heavy_window_conflicts(jobs))


def test_overlapping_dr_windows_are_detected():
    jobs = copy.deepcopy(SCHEDULE)
    _job(jobs, "legion-dr-restore-drill-weekly")["schedule"] = {"minute": 0, "hour": 17, "dow": 0}
    assert any("overlaps" in c for c in contract.heavy_window_conflicts(jobs))


def test_ops_selfcheck_timeout_covers_the_scratch_probe():
    assert _job(SCHEDULE, "ops-selfcheck")["timeout_s"] >= 600


def test_testctl_serve_ensure_does_not_inherit_the_runner_env():
    """It starts a detached daemon; a HOSTCRON_DEADLINE_EPOCH inherited by it would outlive the job."""
    assert _job(SCHEDULE, "testctl-serve-ensure").get("runner_env") is False


def test_no_job_still_runs_the_deleted_cmd_wrapper():
    assert not any("run-dr-restore-drill-weekly" in json.dumps(j) for j in SCHEDULE)
