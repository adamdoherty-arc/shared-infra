from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_FILE = PLATFORM_ROOT / "projects.yml"
ARTIFACTS_ROOT = PLATFORM_ROOT / "artifacts"
PROFILE_NAME = ".testplatform.yml"
DEFAULT_LIGHT_CONCURRENCY = 1
MAX_LIGHT_CONCURRENCY = 8
FRAMEWORKS = frozenset({"pytest", "vitest", "playwright", "schemathesis", "commands"})


class ProfileError(Exception):
    pass


@dataclass
class Project:
    name: str
    root: Path
    legion_project_id: int
    profile: dict[str, Any] = field(default_factory=dict)
    light_concurrency: int = DEFAULT_LIGHT_CONCURRENCY
    light_concurrency_max: int | None = None

    @property
    def light_slots_max(self) -> int:
        return max(self.light_concurrency, self.light_concurrency_max or 0)

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


def check_light_concurrency(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIGHT_CONCURRENCY:
        raise ProfileError(f"{where}: light_concurrency must be an integer 1..{MAX_LIGHT_CONCURRENCY}, got {value!r}")
    return value


def measured_rss_file(project: str) -> Path:
    return PLATFORM_ROOT / "state" / f"measured_worker_rss_{project}.json"


def measured_worker_rss_mb(project: str) -> float | None:
    try:
        return float(json.loads(measured_rss_file(project).read_text(encoding="utf-8"))["worker_rss_mb"])
    except (OSError, ValueError, KeyError):
        return None


def memory_budget_problem(profile: dict[str, Any], light_concurrency: int, measured_mb: float | None = None) -> str | None:
    """None when the heavy run's workers fit under the container memory limit, else why not.

    Workers are sized by RAM, not CPU: every xdist worker collects the whole suite and grows to 1.3-2.2 GB, so
    the budget is `workers * worker_rss + parent + headroom <= mem_budget`; light runs are admitted around
    that at run time (`light_slots_while_heavy`) and queue when nothing is left. A recorded
    measurement from the last heavy run (the sampled peak worker RSS) overrides the configured estimate when it
    is larger, so the config can never claim a fit the machine has already disproved.
    """
    lanes = profile.get("lanes") or {}
    budget = lanes.get("mem_budget_mb")
    if budget is None:
        return None
    tiers = profile.get("tiers") or {}
    path_tier = profile.get("path_tier")
    heavy = max([int(t.get("workers", 0)) for n, t in tiers.items()
                 if n != path_tier and t.get("framework", profile.get("framework")) == "pytest"] or [0])
    worker = max(float(lanes.get("worker_rss_mb", 0)), measured_mb or 0.0)
    need = heavy * worker + float(lanes.get("parent_rss_mb", 450)) + float(lanes.get("headroom_mb", 0))
    if need > float(budget):
        return (f"heavy workers {heavy} x {worker:.0f} MB + parent {lanes.get('parent_rss_mb', 450)} MB + headroom "
                f"{lanes.get('headroom_mb', 0)} MB = {need:.0f} MB exceeds mem_budget_mb {budget}")
    return None


def light_slots_while_heavy(profile: dict[str, Any], light_concurrency: int, measured_mb: float | None = None) -> int:
    """How many light runs fit beside a live heavy run under the memory budget (all of them when no budget)."""
    lanes = profile.get("lanes") or {}
    budget = lanes.get("mem_budget_mb")
    if budget is None:
        return light_concurrency
    tiers = profile.get("tiers") or {}
    path_tier = profile.get("path_tier")
    heavy = max([int(t.get("workers", 0)) for n, t in tiers.items()
                 if n != path_tier and t.get("framework", profile.get("framework")) == "pytest"] or [0])
    worker = max(float(lanes.get("worker_rss_mb", 0)), measured_mb or 0.0)
    spare = float(budget) - heavy * worker - float(lanes.get("parent_rss_mb", 450))
    light = max(1.0, float(lanes.get("light_rss_mb", 1)))
    return max(0, min(light_concurrency, int(spare // light)))


DEFAULT_HOST_RESERVE_MB = 1024.0


def host_available_mb() -> float | None:
    """Physical RAM the host can still hand out, or None when it cannot be read."""
    if sys.platform == "win32":
        import ctypes

        class _Mem(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong),
                        ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("ve", ctypes.c_ulonglong)]

        mem = _Mem()
        mem.length = ctypes.sizeof(_Mem)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return None
        return mem.avail / (1024 * 1024)
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def light_slots_idle(profile: dict[str, Any], base: int, maximum: int, available_mb: float | None = None) -> int:
    """How many light runs may run at once while NO heavy run is live.

    A profile without a `light_concurrency_max` above `base` keeps exactly `base`. Otherwise the answer starts at
    `maximum` and is cut by the container memory budget (`light_rss_mb` each beside the parent), the cpu budget
    (a light run is one in-process interpreter) and the host's free RAM minus `host_reserve_mb`; a tight machine
    degrades toward 1, never to 0, so the lane always makes progress.
    """
    if maximum <= base:
        return base
    lanes = profile.get("lanes") or {}
    light = max(1.0, float(lanes.get("light_rss_mb", 1)))
    allowed = float(maximum)
    budget = lanes.get("mem_budget_mb")
    if budget is not None:
        allowed = min(allowed, (float(budget) - float(lanes.get("parent_rss_mb", 450))) // light)
    cpu = lanes.get("cpu_budget")
    if cpu is not None:
        allowed = min(allowed, float(cpu))
    if available_mb is not None:
        reserve = float(lanes.get("host_reserve_mb", DEFAULT_HOST_RESERVE_MB))
        allowed = min(allowed, max(0.0, available_mb - reserve) // light)
    return max(1, min(int(maximum), int(allowed)))


def worker_budget_problem(profile: dict[str, Any], light_concurrency: int) -> str | None:
    """None when the xdist workers of every run that can overlap fit the container's CPUs, else why not.

    A whole-tier (heavy) run owns `workers` processes; every light-lane run is forced in-process (one process)
    while a heavy run is live, so the worst overlap is heavy workers + one process per light slot. With no heavy
    run the light slots share the budget between them.
    """
    lanes = profile.get("lanes") or {}
    budget = lanes.get("cpu_budget")
    if budget is None:
        return None
    tiers = profile.get("tiers") or {}
    path_tier = profile.get("path_tier")
    heavy = max([int(t.get("workers", 0)) for n, t in tiers.items()
                 if n != path_tier and t.get("framework", profile.get("framework")) == "pytest"] or [0])
    if heavy + light_concurrency > budget:
        return f"heavy workers {heavy} + light slots {light_concurrency} exceed cpu_budget {budget}"
    light = max([int(t.get("workers", 0)) for n, t in tiers.items()
                 if t.get("mode") == "changed" or n == path_tier] or [0])
    if light * light_concurrency > budget:
        return f"light workers {light} x light slots {light_concurrency} exceed cpu_budget {budget}"
    return None


MAX_FAST_PER_TEST_TIMEOUT_S = 60


def fast_timeout_problem(profile: dict[str, Any]) -> str | None:
    """A fast tier whose per-test pytest-timeout exceeds the cap lets one hung test burn minutes of a worker."""
    fast = (profile.get("tiers") or {}).get("fast")
    if not fast or fast.get("framework", profile.get("framework")) != "pytest":
        return None
    args = [str(a) for a in list((profile.get("pytest") or {}).get("common_args", [])) + list(fast.get("args", []))]
    values = [int(a.split("=", 1)[1]) for a in args if a.startswith("--timeout=") and a.split("=", 1)[1].isdigit()]
    if values and values[-1] > MAX_FAST_PER_TEST_TIMEOUT_S:
        return f"fast tier per-test --timeout={values[-1]} exceeds the {MAX_FAST_PER_TEST_TIMEOUT_S}s cap"
    return None


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
    lanes = profile.get("lanes")
    if lanes is not None:
        if not isinstance(lanes, dict):
            raise ProfileError(f"{where}: 'lanes' must be a mapping")
        check_light_concurrency(lanes.get("light_concurrency"), where)
        check_light_concurrency(lanes.get("light_concurrency_max"), where)
        budget = lanes.get("cpu_budget")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
            raise ProfileError(f"{where}: lanes.cpu_budget must be a positive integer, got {budget!r}")
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
    light = check_light_concurrency((profile.get("lanes") or {}).get("light_concurrency"), str(pfile))
    if light is None:
        light = check_light_concurrency(entry.get("light_concurrency"), str(registry_path))
    light = light or DEFAULT_LIGHT_CONCURRENCY
    light_max = check_light_concurrency((profile.get("lanes") or {}).get("light_concurrency_max"), str(pfile))
    problem = worker_budget_problem(profile, light) or fast_timeout_problem(profile) or memory_budget_problem(profile, light, None)
    if problem:
        raise ProfileError(f"{pfile}: {problem}")
    return Project(name=name, root=root, legion_project_id=int(entry["legion_project_id"]), profile=profile,
                   light_concurrency=light, light_concurrency_max=light_max)


def legion_url(registry_path: Path = PROJECTS_FILE) -> str:
    env = os.environ.get("TESTCTL_LEGION_URL")
    if env:
        return env.rstrip("/")
    return str(load_registry(registry_path).get("legion_url", "http://127.0.0.1:8005")).rstrip("/")


def parse_target_spec(spec: str) -> tuple[str, str | None]:
    project, sep, target = spec.partition(":")
    return project, (target if sep and target else None)
