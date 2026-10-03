"""The registered projects' shipped profiles, as testctl's own test container can read them.

shared-infra's tests run in a throwaway container that mounts only testplatform/ and scripts/, so the checks of
ADA's shipped .testplatform.yml looked for C:/code/ADA there, found nothing and skipped on every testctl run (ADA
sprint 15335's verifier, 2026-10-03): no run had ever evaluated them. shared-infra's .testplatform.yml now mounts
ADA's profile read-only under $TESTPLATFORM_SHIPPED_PROFILES. With that variable set a missing profile fails the
test; only a run with neither the mount nor a checkout skips.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import profile  # noqa: E402

ENV = "TESTPLATFORM_SHIPPED_PROFILES"


def shipped_profile_file(name: str, registry_path: Path = profile.PROJECTS_FILE) -> Path:
    mounted = os.environ.get(ENV)
    if mounted:
        path = Path(mounted) / name / profile.PROFILE_NAME
        if not path.is_file():
            pytest.fail(f"{ENV}={mounted} but {path} is missing: testctl's mount of {name}'s profile is broken")
        return path
    root = Path(str(profile.load_registry(registry_path)["projects"][name]["root"]))
    if not (root / profile.PROFILE_NAME).is_file():
        pytest.skip(f"no {name} checkout at {root} and no {ENV} mount")
    return root / profile.PROFILE_NAME


def load_shipped_project(name: str, tmp_path: Path, registry_path: Path = profile.PROJECTS_FILE) -> profile.Project:
    """profile.load_project on the shipped profile, through a registry whose root holds a copy of it, so every check
    load_project makes (budgets, the fast timeout cap, the serial phase keys) runs against it."""
    source = shipped_profile_file(name, registry_path)
    entry = dict(profile.load_registry(registry_path)["projects"][name])
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, root / profile.PROFILE_NAME)
    entry["root"] = root.as_posix()
    registry = tmp_path / "shipped-projects.yml"
    registry.write_text(yaml.safe_dump({"projects": {name: entry}}), encoding="utf-8")
    return profile.load_project(name, registry)
