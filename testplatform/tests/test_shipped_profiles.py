"""The shipped-profile checks run under testctl instead of skipping (ADA sprint 15335's verifier, 2026-10-03).

This gate would pass trivially if a set but empty mount skipped like a missing checkout (the checks would go back to
never running), or if load_shipped_project bypassed load_project's budget checks; each has a sabotage case here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shipped_profiles import ENV, load_shipped_project, shipped_profile_file  # noqa: E402
from tplib import profile  # noqa: E402


def test_sabotage_a_set_but_empty_mount_fails_instead_of_skipping(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path))
    with pytest.raises(pytest.fail.Exception, match="mount of ada's profile is broken"):
        shipped_profile_file("ada")


def test_control_a_mounted_profile_is_the_one_read(tmp_path, monkeypatch):
    (tmp_path / "ada").mkdir()
    mounted = tmp_path / "ada" / profile.PROFILE_NAME
    mounted.write_text("tiers: {}\n", encoding="utf-8")
    monkeypatch.setenv(ENV, str(tmp_path))
    assert shipped_profile_file("ada") == mounted


def test_sabotage_a_shipped_profile_over_its_memory_budget_is_refused_at_load(tmp_path, monkeypatch):
    """The verifier's margin point as an absolute check: four 2,100 MB workers, the 1,350 MB parent and the 500 MB
    headroom are 10,250 MB against the 10,240 MB budget, and load_project refuses the whole profile."""
    shipped = yaml.safe_load(shipped_profile_file("ada").read_text(encoding="utf-8"))
    shipped["lanes"]["worker_rss_mb"] = 2100
    mount = tmp_path / "mount"
    (mount / "ada").mkdir(parents=True)
    (mount / "ada" / profile.PROFILE_NAME).write_text(yaml.safe_dump(shipped), encoding="utf-8")
    monkeypatch.setenv(ENV, str(mount))
    with pytest.raises(profile.ProfileError, match="exceeds mem_budget_mb"):
        load_shipped_project("ada", tmp_path / "work")


def test_control_the_shipped_ada_profile_loads_with_every_load_time_check(tmp_path):
    project = load_shipped_project("ada", tmp_path)
    assert project.tier("bitcoin").get("serial_marker"), "the live-money release tier runs its memory_heavy tests"
    assert profile.memory_budget_problem(project.profile, project.light_concurrency) is None
