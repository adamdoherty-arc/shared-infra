from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_FILE = PLATFORM_ROOT / "projects.yml"
ARTIFACTS_ROOT = PLATFORM_ROOT / "artifacts"
PROFILE_NAME = ".testplatform.yml"
FRAMEWORKS = frozenset({"pytest", "vitest", "playwright", "schemathesis", "commands"})


class ProfileError(Exception):
    pass


@dataclass
class Project:
    name: str
    root: Path
    legion_project_id: int
    profile: dict[str, Any] = field(default_factory=dict)

    def tier(self, name: str) -> dict[str, Any] | None:
        return (self.profile.get("tiers") or {}).get(name)

    @property
    def tier_names(self) -> list[str]:
        return list((self.profile.get("tiers") or {}).keys())

    @property
    def default_target(self) -> str:
        return self.profile.get("default_target", "fast")


def load_registry(path: Path = PROJECTS_FILE) -> dict[str, Any]:
    if not path.exists():
        raise ProfileError(f"projects registry missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "projects" not in data or not isinstance(data["projects"], dict):
        raise ProfileError("projects.yml must contain a 'projects' mapping")
    return data


def validate_profile(profile: dict[str, Any], where: str) -> None:
    if not isinstance(profile, dict):
        raise ProfileError(f"{where}: profile must be a mapping")
    tiers = profile.get("tiers")
    if not isinstance(tiers, dict) or not tiers:
        raise ProfileError(f"{where}: 'tiers' mapping is required")
    for name, tier in tiers.items():
        if not isinstance(tier, dict):
            raise ProfileError(f"{where}: tier {name!r} must be a mapping")
        fw = tier.get("framework", profile.get("framework"))
        if fw not in FRAMEWORKS:
            raise ProfileError(f"{where}: tier {name!r} framework {fw!r} not in {sorted(FRAMEWORKS)}")
        if int(tier.get("timeout_s", 0)) <= 0:
            raise ProfileError(f"{where}: tier {name!r} needs a positive timeout_s")
    default = profile.get("default_target", "fast")
    if default not in tiers:
        raise ProfileError(f"{where}: default_target {default!r} is not a tier")
    if profile.get("path_tier") not in (None, *tiers.keys()):
        raise ProfileError(f"{where}: path_tier must name a tier")
    vpt = profile.get("vitest_path_tier")
    if vpt is not None:
        if vpt not in tiers:
            raise ProfileError(f"{where}: vitest_path_tier must name a tier")
        if tiers[vpt].get("framework", profile.get("framework")) != "vitest":
            raise ProfileError(f"{where}: vitest_path_tier {vpt!r} must be a vitest tier")


def load_project(name: str, registry_path: Path = PROJECTS_FILE) -> Project:
    registry = load_registry(registry_path)
    entry = registry["projects"].get(name)
    if entry is None:
        raise ProfileError(f"unknown project {name!r}; registered: {', '.join(sorted(registry['projects']))}")
    root = Path(str(entry["root"]))
    pfile = root / PROFILE_NAME
    if not pfile.exists():
        raise ProfileError(f"no {PROFILE_NAME} in {root}")
    profile = yaml.safe_load(pfile.read_text(encoding="utf-8")) or {}
    validate_profile(profile, str(pfile))
    return Project(name=name, root=root, legion_project_id=int(entry["legion_project_id"]), profile=profile)


def legion_url(registry_path: Path = PROJECTS_FILE) -> str:
    env = os.environ.get("TESTCTL_LEGION_URL")
    if env:
        return env.rstrip("/")
    return str(load_registry(registry_path).get("legion_url", "http://127.0.0.1:8005")).rstrip("/")


def parse_target_spec(spec: str) -> tuple[str, str | None]:
    project, sep, target = spec.partition(":")
    return project, (target if sep and target else None)
