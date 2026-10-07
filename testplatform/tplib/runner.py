from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from . import parsers
from . import selection as sel
from . import snapshot as snapmod
from .artifacts import new_artifact_dir
from .legion import LegionClient, LegionError, retryable
from .lock import HEAVY, LIGHT, Held, ProjectLock
from .profile import (
    ARTIFACTS_ROOT,
    Project,
    host_available_mb,
    light_slots_idle,
    light_slots_while_heavy,
    measured_rss_file,
    measured_worker_rss_mb,
    memory_budget_problem,
)

TRIGGERS = ("claude", "schedule", "hook", "manual")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
OUTPUT_TAIL_LINES = 12
SCHEDULE_RERUNS = 2
RERUN_EXCEPT = ("Timeout >", "crashed while running")
FAILURE_STREAM_ENV = "TESTCTL_FAILURE_STREAM"
MAX_DESELECT = 400
DEFAULT_STALL_S = 600


class RunError(Exception):
    pass


@dataclass
class Execution:
    status: str
    cases: list[dict[str, Any]] = field(default_factory=list)
    error_summary: str | None = None
    quarantined_deselected: int = 0
    rc: int | None = None
    reason: str | None = None


@dataclass
class Outcome:
    run_id: int | None
    status: str
    text: str
    artifact_dir: Path | None = None
    attached: bool = False

    @property
    def exit_code(self) -> int:
        return {"passed": 0, "failed": 1}.get(self.status, 2)


def git_sha(root: Path) -> str:
    base = ["git", "-c", "safe.directory=*", "-C", str(root)]
    try:
        head = subprocess.run(base + ["rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=20, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if not head:
        return "unknown"
    try:
        diff = subprocess.run(base + ["diff", "HEAD", "--no-ext-diff", "--binary"], capture_output=True,
                              timeout=30, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return head
    if not diff:
        return head
    return f"{head[:32]}+{hashlib.sha1(diff).hexdigest()[:7]}"


VITEST_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"})


def resolve_target(project: Project, target: str | None) -> tuple[str, dict[str, Any], list[str] | None]:
    target = target or project.default_target
    tier = project.tier(target)
    if tier is not None:
        return target, tier, None
    path_tier = project.profile.get("path_tier")
    if Path(target.split("::")[0]).suffix in VITEST_SUFFIXES and project.profile.get("vitest_path_tier"):
        path_tier = project.profile["vitest_path_tier"]
    if not path_tier:
        raise RunError(f"target {target!r} is not a tier ({', '.join(project.tier_names)}) and the profile has no path_tier")
    return path_tier, project.tier(path_tier), [target]


def lane_of(project: Project, target: str | None, changed_paths: list[str] | None = None) -> str:
    """`light` for a request scoped to named files (a path target or `changed --paths`), `heavy` for any whole tier."""
    _, tier, paths = resolve_target(project, target)
    return LIGHT if (paths or (changed_paths and tier.get("mode") == "changed")) else HEAVY


def lock_for(project: Project, target: str | None, changed_paths: list[str] | None = None,
             lock_dir: Path | None = None) -> ProjectLock:
    """The lock a request must hold: the project's single heavy slot, or one of its light-lane slots."""
    kwargs: dict[str, Any] = {} if lock_dir is None else {"lock_dir": lock_dir}
    if lane_of(project, target, changed_paths) == LIGHT:
        return ProjectLock(project.name, lane=LIGHT, slots=project.light_slots_max,
                           admit=lambda n: light_blocker(project, n, lock_dir), **kwargs)
    tier_name = resolve_target(project, target)[0]
    return ProjectLock(project.name, admit=lambda _n: heavy_blocker(project, lock_dir, tier=tier_name), **kwargs)


def request_key(target: str | None, paths: list[str] | None) -> str:
    """Identity of a run request: a second request with the same key attaches, a different one queues."""
    return json.dumps([target or "", sorted(paths or [])])


def preflight(project: Project, target: str | None, changed_paths: list[str] | None) -> None:
    """Refuse before a Legion run row exists when the request cannot be scoped."""
    _, tier, named = resolve_target(project, target)
    for node in named or []:
        if not (project.root / node.split("::", 1)[0]).exists():
            raise RunError(f"target {node!r} is not a tier ({', '.join(project.tier_names)}) and names no existing "
                           f"file or directory under {project.root.as_posix()}")
    if lane_of(project, target, changed_paths) == HEAVY:
        problem = memory_budget_problem(project.profile, project.light_concurrency, measured_worker_rss_mb(project.name))
        if problem:
            raise RunError(f"memory budget: {problem}; lower the heavy workers or raise the container limit")
    if changed_paths and tier.get("mode") != "changed":
        raise RunError("--paths only applies to the changed tier (testctl run <project>:changed --paths <files>)")
    if tier.get("mode") == "changed" and project.profile.get("runtime"):
        problem = sel.preflight_changed(project.profile["runtime"], tier, bool(changed_paths))
        if problem:
            raise RunError(problem)


def _run(cmd: list[str], timeout: float, out_file: Path | None = None, cwd: Path | None = None,
         env: dict[str, str] | None = None) -> tuple[int, str]:
    handle = open(out_file, "ab") if out_file else None
    try:
        proc = subprocess.run(cmd, stdout=handle or subprocess.PIPE, stderr=subprocess.STDOUT,
                              cwd=str(cwd) if cwd else None, timeout=timeout,
                              env={**os.environ, **(env or {})}, creationflags=NO_WINDOW)
        text = "" if handle else (proc.stdout or b"").decode("utf-8", "replace")
        return proc.returncode, text
    except subprocess.TimeoutExpired:
        return 124, "host-side timeout"
    except OSError as exc:
        return 127, f"could not start {cmd[0]}: {exc}"
    finally:
        if handle:
            handle.close()


class StallWatchdog:
    """Ends a pytest run whose output stops growing, so one stuck worker cannot spend the tier's whole budget.

    Scheduled fast runs 3048 and 3175 (2026-10-02) printed their last progress character about twelve minutes in
    and then sat on the final 4 and 7 tests until the 1,800 s kill, a run that normally takes 11 to 13 minutes.
    The per-test timeout cannot help when the controller itself is waiting on a worker that will never report.
    The longest legitimate silence is one test's timeout plus the hang guard's grace, far under `stall_s`.
    """

    def __init__(self, container: str, out_log: Path, run_marker: str, stall_s: int, poll_s: float = 15.0):
        self.container, self.out_log, self.run_marker = container, out_log, run_marker
        self.stall_s, self.poll_s = stall_s, poll_s
        self.fired = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="stall-watchdog")

    def _size(self) -> int:
        try:
            return self.out_log.stat().st_size
        except OSError:
            return 0

    def _loop(self) -> None:
        last_size, last_change = self._size(), time.time()
        while not self._stop.wait(self.poll_s):
            size = self._size()
            if size != last_size:
                last_size, last_change = size, time.time()
                continue
            if time.time() - last_change >= self.stall_s:
                self.fired = True
                _run(["docker", "exec", self.container, "pkill", "-TERM", "-f", self.run_marker], 30)
                self._stop.wait(10)
                _run(["docker", "exec", self.container, "pkill", "-KILL", "-f", self.run_marker], 30)
                return

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=60)


ARGFILE_CMDLINE_LIMIT = 24000


def spill_paths_to_argfile(cmd_prefix: list[str], pytest_args: list[str], path_count: int, command_len: int,
                           write: Callable[[str, str], int], remote: str) -> list[str]:
    """Windows CreateProcess refuses a command line over 32,767 characters, so a changed-tier run that selects
    thousands of test files failed to start (exit 127, no report). Past ARGFILE_CMDLINE_LIMIT the selected paths
    move into a pytest @argfile written inside the container; below it the arguments are returned unchanged."""
    if path_count <= 0 or len(subprocess.list2cmdline(cmd_prefix + pytest_args)) <= ARGFILE_CMDLINE_LIMIT:
        return pytest_args
    path_args = pytest_args[command_len:command_len + path_count]
    if write(remote, "\n".join(path_args) + "\n") != 0:
        return pytest_args
    return pytest_args[:command_len] + [f"@{remote}"] + pytest_args[command_len + path_count:]


ALLOWLIST_MIN_FILES = 25


def allowlist_plan(project: Project, paths: list[str] | None, min_files: int = ALLOWLIST_MIN_FILES
                   ) -> tuple[list[str], str] | None:
    """For a large named-file pytest selection, the directories to hand pytest ONCE plus the allowlist text.

    pytest 9 re-lists and stats a test directory once per positional file argument, so N files over a ~2,500-entry
    directory on a 9p bind mount cost N x 2,500 stats per worker (run #7367: 1,474 files never left collection).
    When the profile declares `pytest.allowlist_env` and the selection holds at least `min_files` distinct
    `test_*.py` files, the positional arguments become the minimal set of containing directories and the selected
    files go into an allowlist (one path or path::node id per line) the project's collection plugin reads from that
    env var. None keeps the one-argument-per-file behaviour."""
    cfg = project.profile.get("pytest", {})
    if not cfg.get("allowlist_env") or not paths:
        return None
    prefix = cfg.get("path_prefix", "")
    entries: list[str] = []
    files: set[str] = set()
    for raw in paths:
        value = raw[len(prefix):] if prefix and raw.startswith(prefix) else raw
        value = value.replace("\\", "/")
        base = value.split("::", 1)[0]
        name = base.rsplit("/", 1)[-1]
        if not base.endswith(".py") or not name.startswith("test_") or "/" not in base or base.startswith(("/", "-")):
            return None
        entries.append(value)
        files.add(base)
    if len(files) < min_files:
        return None
    parents = sorted({f.rsplit("/", 1)[0] for f in files})
    dirs = [d for d in parents if not any(d != o and d.startswith(o + "/") for o in parents)]
    return dirs, "\n".join(dict.fromkeys(entries)) + "\n"


def push_text(container: str, remote: str, text: str) -> int:
    try:
        proc = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"',
                               "sh", remote], input=text.encode("utf-8"), capture_output=True, timeout=60,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return 1
    return proc.returncode


def pull_file(container: str, remote: str, dest: Path) -> int:
    rc, _ = _run(["docker", "exec", container, "cat", remote], 120, dest)
    if rc != 0 and dest.exists():
        dest.unlink()
    return rc


def _tail(path: Path, n: int = OUTPUT_TAIL_LINES) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(ln for ln in lines[-n:] if ln.strip())


_PROGRESS_LINE = re.compile(r"^([.FEsxXR]+)\s*(?:\[\s*(\d+)%\]\s*)?(?:\[gw\d+\].*)?$")
_ITEMS_HEADER = re.compile(r"\[(\d+) items\]")


def partial_progress(out_log: Path) -> dict[str, int] | None:
    """Counts pytest's progress characters from a run killed before it wrote a report; None if nothing ran."""
    try:
        text = out_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0, "percent": 0}
    header = _ITEMS_HEADER.search(text)
    if header:
        counts["total"] = int(header.group(1))
    for line in text.splitlines():
        match = _PROGRESS_LINE.match(line.strip())
        if not match:
            continue
        chars = match.group(1)
        counts["passed"] += chars.count(".") + chars.count("X")
        counts["failed"] += chars.count("F")
        counts["errors"] += chars.count("E")
        counts["skipped"] += chars.count("s") + chars.count("x")
        if match.group(2):
            counts["percent"] = int(match.group(2))
    done = counts["passed"] + counts["failed"] + counts["errors"] + counts["skipped"]
    if done == 0:
        return None
    if counts["total"] and not counts["percent"]:
        counts["percent"] = min(99, done * 100 // counts["total"])
    return counts


def timeout_summary(out_log: Path, timeout_s: int) -> str:
    progress = partial_progress(out_log)
    tail = _summary(_tail(out_log))
    if progress is None:
        return f"exceeded {timeout_s}s with no test progress recorded; {tail}"[:500]
    head = (f"exceeded {timeout_s}s at {progress['percent']}% (partial: {progress['passed']} passed, "
            f"{progress['failed']} failed, {progress['errors']} errors, {progress['skipped']} skipped "
            f"of {progress['total']}); ")
    return (head + tail)[:500]


def _summary(text: str, limit: int = 500) -> str:
    return " | ".join(ln.strip() for ln in text.splitlines() if ln.strip())[-limit:]


def reap_orphaned_heavy_pytest(runtime: dict[str, Any], workdir_marker: str = "pytest backend/tests --rootdir") -> int:
    """Kill whole-suite pytest processes left in the container by a runner that died. The caller holds the project's
    single heavy-lane lock, so any whole-suite pytest still running there has no owner."""
    container = runtime["container"]
    pattern = f"timeout -s TERM .*{workdir_marker}"
    rc, out = _run(["docker", "exec", container, "pgrep", "-f", pattern], 30)
    pids = [p for p in out.split() if p.isdigit()] if rc == 0 else []
    if not pids:
        return 0
    _run(["docker", "exec", container, "pkill", "-TERM", "-f", pattern], 30)
    time.sleep(25)
    _run(["docker", "exec", container, "pkill", "-KILL", "-f", pattern], 30)
    return len(pids)


def ensure_container(runtime: dict[str, Any], root: Path) -> str | None:
    container = runtime["container"]
    rc, out = _run(["docker", "inspect", "-f", "{{.State.Running}}", container], 30)
    if rc == 0 and out.strip() == "true":
        return None
    up = runtime.get("ensure_up")
    if up:
        _run(list(up), 300, cwd=root)
        rc, out = _run(["docker", "inspect", "-f", "{{.State.Running}}", container], 30)
        if rc == 0 and out.strip() == "true":
            return None
    return f"container {container} is not running and could not be started"


DEFAULT_PARALLEL_MIN_FILES = 150


HEAVY_PRIORITY_AFTER_S = 600.0


def light_blocker(project: Project, held_light: int, lock_dir: Path | None = None,
                  available_mb: float | None = None, now: float | None = None) -> Held | None:
    """The run that stops a further light run from starting, else None.

    While a heavy run is live that is the heavy run when no memory is left beside it. A heavy request that has
    waited HEAVY_PRIORITY_AFTER_S blocks further light runs the same way until what is live fits beside it: on
    2026-10-03 ADA's fast, e2e_bitcoin, e2e and bitcoin requests waited over an hour while one targeted run after
    another kept the light lane busy, because a heavy run starts only once the lane has drained. With no heavy run
    the lane is capped by `light_slots_idle` (container memory, cpu, host free RAM) and the blocker is a live light
    holder. This would pass trivially if a waiter never aged (light runs keep the lane forever) or aged at once
    (every queued heavy request stalls targeted runs); test_admission.py pins both edges.
    """
    kwargs: dict[str, Any] = {} if lock_dir is None else {"lock_dir": lock_dir}
    heavy = ProjectLock(project.name, **kwargs).read()
    if heavy is not None and not heavy.stale:
        allowed = light_slots_while_heavy(project.profile, project.light_concurrency,
                                          measured_worker_rss_mb(project.name), heavy_tier=held_tier(heavy))
        return heavy if held_light >= allowed else None
    waiting = ProjectLock(project.name, **kwargs).oldest_waiter()
    if waiting is not None and (time.time() if now is None else now) - waiting[1] >= HEAVY_PRIORITY_AFTER_S:
        allowed = light_slots_while_heavy(project.profile, project.light_concurrency,
                                          measured_worker_rss_mb(project.name), heavy_tier=held_tier(waiting[0]))
        if held_light >= allowed:
            return waiting[0]
    if project.light_slots_max <= project.light_concurrency:
        return None
    if available_mb is None:
        available_mb = host_available_mb()
    allowed = light_slots_idle(project.profile, project.light_concurrency, project.light_slots_max, available_mb)
    if held_light < allowed:
        return None
    live = ProjectLock(project.name, lane=LIGHT, slots=project.light_slots_max, **kwargs).holders()
    return live[0] if live else None


def held_tier(held: Held) -> str | None:
    """The tier a heavy lock holder is running, read from its request key (`[target, paths]`); None when unreadable."""
    try:
        target = json.loads(held.key or "")[0]
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    return str(target) or None


def heavy_blocker(project: Project, lock_dir: Path | None = None, tier: str | None = None) -> Held | None:
    """A live light holder when more light runs are live than fit beside the requested heavy tier, else None.

    The light lane may widen to `light_concurrency_max` while nothing heavy runs; a heavy run starting then would add
    its workers on top and push the container past its memory limit, so it waits for the lane to drain to the
    level that fits beside its own tier (a tier declaring `rss_mb`, such as one that runs on the host, waits for
    nothing it does not need).
    """
    if project.light_slots_max <= project.light_concurrency:
        return None
    kwargs: dict[str, Any] = {} if lock_dir is None else {"lock_dir": lock_dir}
    live = ProjectLock(project.name, lane=LIGHT, slots=project.light_slots_max, **kwargs).holders()
    allowed = light_slots_while_heavy(project.profile, project.light_concurrency, measured_worker_rss_mb(project.name),
                                      heavy_tier=tier)
    return live[0] if len(live) > max(allowed, 0) else None


def light_worker_cap(project: Project, lock_dir: Path | None = None) -> int | None:
    """xdist worker ceiling for a light-lane run: in-process while a heavy run is live, else its share of cpu_budget."""
    budget = (project.profile.get("lanes") or {}).get("cpu_budget")
    if budget is None:
        return None
    kwargs: dict[str, Any] = {} if lock_dir is None else {"lock_dir": lock_dir}
    heavy = ProjectLock(project.name, **kwargs).read()
    if heavy is not None and not heavy.stale:
        return 0
    return int(budget) // max(1, project.light_concurrency)


def effective_workers(workers: int, paths: list[str] | None, parallel_min_files: int) -> int:
    """xdist only pays for itself on a large file set: each worker re-imports the whole backend (~1 GB, tens of
    seconds), and under lane contention the per-test thread timeout kills starved workers in a replace loop.
    A named-file selection below `parallel_min_files` therefore runs in-process (-n0)."""
    if workers <= 0 or parallel_min_files <= 0 or not paths:
        return workers
    files = {p.split("::", 1)[0] for p in paths if p.split("::", 1)[0].endswith(".py")}
    return workers if len(files) >= parallel_min_files else 0


def _pytest_args(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str,
                 report_file: str, deselect: list[str], extra: list[str],
                 positional: list[str] | None = None) -> list[str]:
    cfg = project.profile.get("pytest", {})
    prefix = cfg.get("path_prefix", "")

    def strip(value: str) -> str:
        return value[len(prefix):] if prefix and value.startswith(prefix) else value

    args = list(cfg.get("command", ["python", "-m", "pytest"]))
    args += positional if positional is not None else [strip(p) for p in (paths or tier.get("paths") or cfg.get("paths") or [])]
    deselect = [strip(d) for d in deselect]
    args += list(cfg.get("common_args", []))
    workers = int(tier.get("workers", cfg.get("workers", 0)))
    workers = effective_workers(workers, paths, int(tier.get("parallel_min_files", DEFAULT_PARALLEL_MIN_FILES)))
    args += ["-n", str(workers), "--dist", "loadfile"] if workers > 0 else ["-n0"]
    node_id_run = bool(tier.get("marker_unless_node_id")) and any("::" in p for p in (paths or []))
    if tier.get("marker") and not node_id_run:
        args += ["-m", tier["marker"]]
    args += list(tier.get("args", []))
    args += extra
    if trigger == "schedule":
        args += ["--reruns", str(SCHEDULE_RERUNS)]
        for pattern in RERUN_EXCEPT:
            args += ["--rerun-except", pattern]
    else:
        args += ["-p", "no:randomly"]
    for node in deselect[:MAX_DESELECT]:
        args += ["--deselect", node]
    args += ["--json-report", f"--json-report-file={report_file}",
             "--json-report-omit=keywords", "--json-report-omit=streams",
             "--json-report-omit=warnings", "--json-report-omit=log", "--json-report-omit=traceback"]
    return args


def map_changed_to_tests(root: Path, files: list[str], test_globs: list[str]) -> list[str]:
    import fnmatch
    tests: set[str] = set()
    test_dirs = sorted({g.replace("\\", "/").split("*", 1)[0].rstrip("/") or "." for g in test_globs})
    all_tests: list[str] = []
    for d in test_dirs:
        base = root / d
        if base.exists():
            all_tests += [str(p.relative_to(root)).replace("\\", "/") for p in base.rglob("test_*.py")]
    for f in files:
        f = f.replace("\\", "/")
        if not f.endswith(".py"):
            continue
        if any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(f, g.replace("/**/", "/")) for g in test_globs):
            if (root / f).exists():
                tests.add(f)
            continue
        stem = Path(f).stem
        if stem == "__init__" or len(stem) < 3:
            continue
        for t in all_tests:
            tstem = Path(t).stem
            if tstem == f"test_{stem}" or tstem.startswith(f"test_{stem}_"):
                tests.add(t)
    return sorted(tests)


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(env: dict[str, str], root: Path) -> dict[str, str]:
    dotenv: dict[str, str] = {}
    try:
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            key, sep, val = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                dotenv[key.strip()] = val.strip().strip("\"'")
    except OSError:
        pass

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        found = dotenv.get(name) or os.environ.get(name)
        if found is None:
            raise RunError(f"runtime env references ${{{name}}} but it is set in neither the environment nor {root / '.env'}")
        return found

    return {k: _ENV_REF.sub(sub, str(v)) for k, v in env.items()}


class RssSampler:
    """Records the peak combined xdist worker RSS seen while a heavy run executes (the sum at one instant, since
    workers peak at different times), so the memory gate rests on a measurement and not on an estimate."""

    def __init__(self, container: str, project: str, interval_s: float = 20.0):
        self.container, self.project, self.interval_s = container, project, interval_s
        self.peak_total_kb = 0
        self.workers = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="rss-sampler")

    def _sample(self) -> None:
        rc, text = _run(["docker", "exec", self.container, "sh", "-c",
                         "ps -eo rss=,args= | grep 'exec(eval' | grep -v grep | sort -rn"], 30)
        if rc != 0:
            return
        rows = [int(line.split()[0]) for line in text.splitlines() if line.strip() and line.split()[0].isdigit()]
        if rows and sum(rows) > self.peak_total_kb:
            self.peak_total_kb = sum(rows)
            self.workers = len(rows)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._sample()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=35)
        if self.peak_total_kb:
            target = measured_rss_file(self.project)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"worker_rss_mb": round(self.peak_total_kb / 1024 / self.workers, 1),
                                              "workers_total_mb": round(self.peak_total_kb / 1024, 1),
                                              "workers": self.workers, "at": _iso()}), encoding="utf-8")
            except OSError:
                pass


def sync_snapshot(project: Project, runtime: dict[str, Any], out_log: Path) -> str | None:
    """Mirror the committed tree into the container's own filesystem and return its path, or None on failure.

    The repo reaches the container over a 9p bind mount where a whole-backend read costs ~20s and every
    repo-scanning test pays it; the snapshot is local disk (0.3s). Committed content only, so another
    session's uncommitted edits cannot fail an official run. Untracked runtime paths are symlinked back.
    """
    cfg = runtime["snapshot"]
    dest, rev, container = cfg["dest"], cfg.get("rev", "HEAD"), runtime["container"]
    include = list(cfg["include"])
    app = runtime.get("workdir", "/app")
    started = time.time()
    staging = f"{dest}.new"
    prep = f"rm -rf {staging} && mkdir -p {staging}"
    rc, text = _run(["docker", "exec", container, "sh", "-c", prep], 120)
    if rc != 0:
        out_log.write_text(f"snapshot prepare failed: {text[:300]}\n", encoding="utf-8")
        return None
    git = ["git", "-c", "safe.directory=*", "-C", str(project.root), "archive", rev, "--", *include,
           *[f":(exclude){x}" for x in cfg.get("exclude", [])]]
    try:
        producer = subprocess.Popen(git, stdout=subprocess.PIPE, creationflags=NO_WINDOW)
        consumer = subprocess.run(["docker", "exec", "-i", container, "tar", "-x", "-C", staging],
                                  stdin=producer.stdout, capture_output=True, timeout=300, creationflags=NO_WINDOW)
        producer.stdout.close()
        producer.wait(timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        out_log.write_text(f"snapshot stream failed: {exc}\n", encoding="utf-8")
        return None
    if producer.returncode != 0 or consumer.returncode != 0:
        detail = consumer.stderr.decode("utf-8", "replace")[:300]
        out_log.write_text(f"snapshot extract failed: git rc={producer.returncode} tar rc={consumer.returncode} {detail}\n",
                           encoding="utf-8")
        return None
    link = (f'cd {staging} && for base in {" ".join(cfg.get("link_children_of", ["."]))}; do '
            f'mkdir -p "{staging}/$base"; '
            f'for f in {app}/$base/* {app}/$base/.[!.]*; do b=$(basename "$f"); '
            f'[ -e "{staging}/$base/$b" ] || [ -L "{staging}/$base/$b" ] || ln -s "$f" "{staging}/$base/$b"; done; done; '
            f'rm -rf {dest} && mv {staging} {dest}')
    rc, text = _run(["docker", "exec", container, "sh", "-c", link], 120)
    if rc != 0:
        out_log.write_text(f"snapshot link failed: {text[:300]}\n", encoding="utf-8")
        return None
    out_log.write_text(f"snapshot {rev} -> {container}:{dest} in {time.time() - started:.1f}s\n", encoding="utf-8")
    return dest


def quarantine_in_scope(quarantined: list[str], paths: list[str] | None) -> list[str]:
    """The quarantined node ids a run of `paths` can collect; all of them for a whole-tier run.

    A targeted run used to pass, and count as quarantined-skipped, the project's whole quarantine list, so a run of
    one file holding no quarantined test still reported "quarantined-skipped 1" (ADA run 3669, 2026-10-03).
    """
    if not paths:
        return list(quarantined)
    scopes = [p.replace("\\", "/").rstrip("/") for p in paths]
    return [node for node in quarantined
            if any(node == scope or node.startswith((scope + "::", scope + "[", scope + "/")) for scope in scopes)]


def run_pytest(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, art: Path,
               legion: LegionClient, target: str, changed_paths: list[str] | None = None,
               snap: snapmod.Snapshot | None = None, reuse_snapshot: bool = False) -> Execution:
    """One pytest invocation. `reuse_snapshot` is a whole-tier run's serial phase: it runs on the container snapshot
    the parallel phase synced, so both phases test the same commit, and leaves the orphan reap to that phase."""
    runtime = project.profile["runtime"]
    timeout_s = int(tier["timeout_s"])
    quarantined: list[str] = []
    if trigger in ("claude", "hook"):
        try:
            quarantined = list(legion.quarantine(project.legion_project_id))
        except LegionError:
            quarantined = []
    extra: list[str] = []
    selection: dict[str, Any] = {"mode": "tier"}
    env: dict[str, str] = expand_env(dict(runtime.get("env", {})), project.root)
    if tier.get("mode") == "changed":
        data_file = tier.get("testmon_datafile", "/tmp/testplatform/.testmondata")
        env["TESTMON_DATAFILE"] = data_file
        if changed_paths:
            mapped, selection = sel.select_for_paths(project.root, runtime, data_file, changed_paths,
                                                     tier.get("test_globs", []))
            if not mapped:
                (art / "selection.json").write_text(json.dumps(selection, indent=1), encoding="utf-8")
                return Execution("passed", [], None, 0, 0, reason=(
                    f"no test names or imports any of them via {selection.get('source', 'selection')}"))
            paths = mapped
        else:
            problem = sel.preflight_changed(runtime, tier, False)
            if problem:
                raise RunError(problem)
            extra += ["--testmon"]
            selection = {"mode": "testmon", "datafile": data_file}
    elif tier.get("testmon_build"):
        env["TESTMON_DATAFILE"] = tier.get("testmon_datafile", "/tmp/testplatform/.testmondata")
        extra += ["--testmon-noselect"]
        selection = {"mode": "testmon-build"}
    if paths or changed_paths:
        extra += ["-p", "no:testmon"]
    quarantined = quarantine_in_scope(quarantined, paths)
    (art / "selection.json").write_text(json.dumps(selection, indent=1), encoding="utf-8")

    report_name = f"{uuid.uuid4().hex}.json"
    out_log = art / "output.log"
    report_host = art / "report.json"
    failure_stream: Path | None = art / "failures.jsonl"
    stalled_s = 0
    timeout_prefix = ["timeout", "-s", "TERM", "-k", "20", str(timeout_s)]
    started = time.time()
    if runtime["kind"] == "exec":
        problem = ensure_container(runtime, project.root)
        if problem:
            return Execution("error", error_summary=problem)
        snapshot_dir: str | None = None
        if not paths and not changed_paths:
            if not reuse_snapshot:
                reap_orphaned_heavy_pytest(runtime)
            if runtime.get("snapshot"):
                snapshot_dir = (str(runtime["snapshot"]["dest"]) if reuse_snapshot
                                else sync_snapshot(project, runtime, art / "snapshot.log"))
                if snapshot_dir is None:
                    return Execution("error", error_summary="could not build the clean-tree snapshot for the heavy run, "
                                                            f"see {(art / 'snapshot.log').as_posix()}")
                env["PYTHONPATH"] = snapshot_dir
                if runtime["snapshot"].get("pycache"):
                    env["PYTHONPYCACHEPREFIX"] = runtime["snapshot"]["pycache"]
        tmp_dir = runtime.get("tmp_dir", "/tmp/testplatform")
        report_in = f"{tmp_dir}/{report_name}"
        stream_in = f"{tmp_dir}/{report_name}.failures"
        env[FAILURE_STREAM_ENV] = stream_in
        if paths or changed_paths:
            cap = light_worker_cap(project)
            if cap is not None:
                tier = {**tier, "workers": min(int(tier.get("workers", 0)), cap)}
        workdir = runtime.get("workdir", "/app")
        if snapshot_dir:
            workdir = snapshot_dir
        heavy = not paths and not changed_paths
        argfile_in = f"{tmp_dir}/{report_name}.args"
        allow_in = f"{tmp_dir}/{report_name}.allow"
        positional: list[str] | None = None
        plan = allowlist_plan(project, paths) if not snapshot_dir else None
        if plan is not None:
            if push_text(runtime["container"], allow_in, plan[1]) == 0:
                positional = plan[0]
                env[project.profile["pytest"]["allowlist_env"]] = allow_in
        pytest_args = _pytest_args(project, tier, paths, trigger, report_in, quarantined, extra, positional)
        if snapshot_dir:
            pytest_args = [f"--rootdir={snapshot_dir}" if a == f"--rootdir={runtime.get('workdir', '/app')}" else a
                           for a in pytest_args]
        cmd = ["docker", "exec", "-w", workdir]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [runtime["container"], "sh", "-c", f'mkdir -p {tmp_dir}; exec "$@"', "sh"] + timeout_prefix
        command_len = len(project.profile.get("pytest", {}).get("command", ["python", "-m", "pytest"]))
        spilled = spill_paths_to_argfile(cmd, pytest_args, len(positional) if positional is not None else len(paths or []),
                                         command_len,
                                         lambda remote, text: push_text(runtime["container"], remote, text), argfile_in)
        cmd += spilled
        sampler = RssSampler(runtime["container"], project.name) if heavy else None
        if sampler:
            sampler.start()
        stall_s = int(tier.get("stall_s", DEFAULT_STALL_S)) if heavy else 0
        watchdog = StallWatchdog(runtime["container"], out_log, report_name, stall_s) if stall_s > 0 else None
        if watchdog:
            watchdog.start()
        try:
            rc, _ = _run(cmd, timeout_s + 120, out_log)
        finally:
            if sampler:
                sampler.stop()
            if watchdog:
                watchdog.stop()
        stalled_s = stall_s if watchdog and watchdog.fired else 0
        cp_rc = pull_file(runtime["container"], report_in, report_host)
        if pull_file(runtime["container"], stream_in, failure_stream) != 0:
            failure_stream = None
        _run(["docker", "exec", runtime["container"], "rm", "-f", report_in, argfile_in, allow_in, stream_in], 30)
    else:
        report_in = f"/out/{report_name}"
        pytest_args = _pytest_args(project, tier, paths, trigger, report_in, quarantined, extra)
        cmd = ["docker", "run", "--rm", "--init", "--entrypoint", "sh", "-w", runtime.get("workdir", "/app")]
        if runtime.get("network"):
            cmd += ["--network", runtime["network"]]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        mount_root = snap.root if snap else project.root  # a snapshot run mounts the committed tree, never the live one
        for mount in runtime.get("mounts", []):
            cmd += ["-v", f"{(mount_root / mount['host']).resolve().as_posix()}:{mount['container']}:{mount.get('mode', 'ro')}"]
        for masked in tmpfs_masks(mount_root, runtime):
            cmd += ["--tmpfs", masked]  # an empty scratch dir over a path of a mounted tree (live data a test must never see)
        cmd += ["-v", f"{art.as_posix()}:/out", runtime["image"], "-c"]
        setup = runtime.get("setup", "true")
        cmd += [f'{setup} >/dev/null 2>&1; exec "$@"', "sh"] + timeout_prefix + pytest_args
        rc, _ = _run(cmd, timeout_s + 180, out_log)
        produced = art / report_name
        if produced.exists():
            produced.replace(report_host)
        cp_rc = 0 if report_host.exists() else 1
    elapsed = time.time() - started
    return interpret_pytest(project, tier, paths, trigger, rc, report_host if cp_rc == 0 else None, out_log,
                            elapsed, timeout_s, len(quarantined[:MAX_DESELECT]), failure_stream, stalled_s)


def tmpfs_masks(root: Path, runtime: dict[str, Any]) -> list[str]:
    """`runtime.tmpfs` entries that exist on the host: Docker cannot mount over a path a read-only bind mount
    does not contain, and a data dir that is absent on this machine holds no live data to hide."""
    mounts = [(m["container"].rstrip("/"), (root / m["host"]).resolve()) for m in runtime.get("mounts", [])]
    out: list[str] = []
    for masked in runtime.get("tmpfs", []):
        covering = [(c, h) for c, h in mounts if masked == c or masked.startswith(c + "/")]
        if not covering:
            out.append(masked)
            continue
        c, h = max(covering, key=lambda m: len(m[0]))
        if (h / masked[len(c):].lstrip("/")).exists():
            out.append(masked)
    return out


def check_floors(path: Path, cases: list[dict[str, Any]]) -> str | None:
    """Per-critical-service executed-test floors keyed on test module stem; a prefix matching nothing is an error."""
    try:
        floors = json.loads(path.read_text(encoding="utf-8")).get("floors")
    except (OSError, ValueError):
        return None
    if not isinstance(floors, dict):
        return None
    executed: dict[str, int] = {}
    for c in cases:
        if c["status"] == "skipped":
            continue
        stem = Path(str(c["node_id"]).split("::", 1)[0]).stem
        executed[stem] = executed.get(stem, 0) + 1
    problems: list[str] = []
    for service, cfg in floors.items():
        prefixes = cfg.get("classname_prefixes", []) or []
        zero = [p for p in prefixes if executed.get(p, 0) == 0]
        total = sum(executed.get(p, 0) for p in prefixes)
        floor = int(cfg.get("min_executed", 0))
        if zero:
            problems.append(f"{service}: prefixes matched zero tests {zero}")
        elif total < floor:
            problems.append(f"{service}: executed={total} floor={floor}")
    return ("service floor breach: " + "; ".join(problems)) if problems else None


_CRASHED_WORKER = re.compile(r"worker '(gw\d+)' crashed while running '([^']+)'")


def truncation_of(report: dict[str, Any], rc: int, out_log: Path) -> str | None:
    """Why a pytest run stopped before running what it collected, or None when it ran to the end.

    pytest exit 2 (interrupted) and 3 (internal error) end a session early whatever the cases that did report
    say. Nightly ADA full run 3208: xdist worker gw3 was OOM-killed, the scheduler raised KeyError on its
    replacement and pytest exited 3 at 93%, with 34,332 of 36,687 collected tests reported. The verdict named only
    the paper_trading floor breach those 2,355 missing tests caused, and without a floors file it read as an
    ordinary FAILED: exit 3 counted as an error only when no case had failed, and a crashed worker always leaves
    one failed case behind. A crashed worker whose replacement finished the run (exit 1, nothing missing) is not
    truncation; its crashed test is an ordinary failure. pytest-json-report's summary.collected counts deselected
    items back in (an in-process run reports them as summary.deselected; an xdist controller deselects nothing),
    so the tests that should have reported are collected minus deselected.
    """
    tests = report.get("tests") or []
    summary = report.get("summary") or {}
    selected = int(summary.get("collected") or 0) - int(summary.get("deselected") or 0)
    missing = max(selected - len(tests), 0)
    crashes = []
    for test in tests:
        for phase in test.values():
            found = _CRASHED_WORKER.search(str(phase.get("longrepr") or "")) if isinstance(phase, dict) else None
            if found:
                crashes.append(f"worker {found.group(1)} crashed running {found.group(2)}")
    if rc not in (2, 3) and not (crashes and missing):
        return None
    parts = [f"RUN TRUNCATED: pytest exit {rc}" + {2: " (interrupted)", 3: " (internal error)"}.get(rc, "")]
    if missing:
        parts.append(f"{len(tests)} of {selected} selected tests reported, {missing} never ran")
    parts.extend(crashes[:2])
    internal = [ln[len("INTERNALERROR> "):].strip() for ln in _tail(out_log, 400).splitlines()
                if ln.startswith("INTERNALERROR> ") and "Error" in ln]
    if internal:
        parts.append(internal[-1])
    return "; ".join(parts)


def interpret_pytest(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, rc: int,
                     report_path: Path | None, out_log: Path, elapsed: float, timeout_s: int,
                     quarantined_count: int, failure_stream: Path | None = None, stalled_s: int = 0) -> Execution:
    tail = _summary(_tail(out_log))
    if stalled_s or (rc in (124, 137) and elapsed >= timeout_s - 5):
        streamed = parsers.cases_from_failure_stream(failure_stream, project.root,
                                                     project.profile.get("pytest", {}).get("path_prefix", ""))
        summary = timeout_summary(out_log, timeout_s)
        if stalled_s:
            stalled = f"STALLED, no test progress for {stalled_s}s (killed at {int(elapsed)}s)"
            summary = summary.replace(f"exceeded {timeout_s}s", stalled, 1)
        if streamed:
            names = ", ".join(c["node_id"].rsplit("/", 1)[-1] for c in streamed[:3])
            more = f" +{len(streamed) - 3} more" if len(streamed) > 3 else ""
            summary = f"{summary.split('; ', 1)[0]}; FAILED SO FAR ({len(streamed)}): {names}{more}"[:500]
        return Execution("timeout", streamed, summary, quarantined_count, rc)
    report: dict[str, Any] | None = None
    if report_path and report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except ValueError:
            report = None
    if report is None:
        why = "killed (SIGKILL, likely OOM)" if rc == 137 else f"pytest exit {rc} and no report produced"
        return Execution("error", error_summary=f"{why}: {tail}"[:500], rc=rc,
                         quarantined_deselected=quarantined_count)
    reruns = SCHEDULE_RERUNS if trigger == "schedule" else 0
    cases = parsers.parse_pytest_json(report, project.root, reruns=reruns,
                                      path_prefix=project.profile.get("pytest", {}).get("path_prefix", ""))
    breach = check_floors(project.root / tier["floors_file"], cases) \
        if tier.get("floors_file") and paths is None else None
    truncated = truncation_of(report, rc, out_log)
    if truncated:
        consequence = f"; consequence: {breach}" if breach else ""
        return Execution("error", cases, (truncated + consequence)[:500], quarantined_count, rc)
    if breach:
        return Execution("error", cases, breach[:500], quarantined_count, rc)
    executed = sum(1 for c in cases if c["status"] not in ("skipped",))
    min_executed = int(tier.get("min_executed", 0))
    totals = parsers.totals_of(cases)
    if rc in (2, 3, 4) and totals["errors"] == 0 and totals["failed"] == 0:
        return Execution("error", cases, f"pytest exit {rc}: {tail}"[:500], quarantined_count, rc)
    if rc == 5 and min_executed == 0:
        return Execution("passed", cases, None, quarantined_count, rc)
    if executed < min_executed:
        hint = (f" (marker {tier['marker']!r} deselects slow tests from file runs; name a node id to force one)"
                if tier.get("marker_unless_node_id") else "")
        return Execution("error", cases, f"executed {executed} tests, tier requires at least {min_executed}{hint}",
                         quarantined_count, rc)
    if totals["errors"] and totals["failed"] == 0 and rc != 1:
        status = "error"
    else:
        status = "failed" if (totals["failed"] or totals["errors"] or rc == 1) else "passed"
    summary = None
    if status == "error":
        summary = f"collection/setup errors in {totals['errors']} items: {tail}"[:500]
    return Execution(status, cases, summary, quarantined_count, rc)


def vitest_container_paths(paths: list[str] | None, repo_subdir: str) -> list[str]:
    """Repo-relative paths -> paths relative to the vitest container workdir (which is `repo_subdir`)."""
    subdir = repo_subdir.strip("/")
    out = []
    for raw in paths or []:
        norm = raw.replace("\\", "/")
        out.append(norm[len(subdir) + 1:] if subdir and norm.startswith(subdir + "/") else norm)
    return out


VITEST_TEST_FILE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$|(^|/)__tests__/")


def split_frontend_paths(paths: list[str], repo_subdir: str) -> tuple[list[str], list[str]]:
    """Split caller paths into (frontend files vitest can act on, everything else).

    Frontend means a vitest-suffixed file under the repo's frontend subdir; files are normalised to `/`.
    """
    subdir = repo_subdir.strip("/")
    fe: list[str] = []
    rest: list[str] = []
    for raw in paths:
        norm = raw.replace("\\", "/").removeprefix("./")
        under = not subdir or norm.startswith(subdir + "/")
        (fe if under and Path(norm).suffix in VITEST_SUFFIXES else rest).append(norm)
    return fe, rest


def _vitest_host_call(project: Project, tier: dict[str, Any], argv: list[str], art: Path, tag: str,
                      snap: snapmod.Snapshot | None = None) -> Execution:
    """`npx vitest ...` on the host, in `vitest.repo_subdir` (a project whose node_modules live on the host and
    which runs no frontend container). Same report parsing as the container form; node ids are repo-relative."""
    import shutil
    vt = project.profile.get("vitest", {})
    timeout_s = int(tier["timeout_s"])
    npx = shutil.which("npx")
    if not npx:
        return Execution("error", error_summary="npx is not on PATH for the host vitest run")
    cwd = project.root / str(vt.get("repo_subdir", ""))
    if not (cwd / "node_modules").is_dir():
        return Execution("error", error_summary=f"{cwd.as_posix()}/node_modules is missing: run npm ci there first")
    report_host = art / f"report{tag}.json"
    cmd = [npx, "vitest", *argv, "--reporter=json", f"--outputFile={report_host.as_posix()}", *list(tier.get("args", []))]
    out_log = art / "output.log"
    started = time.time()
    # Snapshot run: the committed tree lives under <repo>/<location>/.tp-snapshot/<id>/, so Node still resolves the
    # real node_modules by walking up from there (the node_modules check above is on the real dir).
    run_cwd = (snap.root / str(vt.get("repo_subdir", ""))) if snap else cwd
    rc, _ = _run(cmd, timeout_s, out_log, cwd=run_cwd)
    tail = _summary(_tail(out_log))
    if rc == 124 and time.time() - started >= timeout_s - 5:
        return Execution("timeout", error_summary=f"exceeded {timeout_s}s; {tail}"[:500], rc=rc)
    if not report_host.exists():
        if rc == 0 and "related" in argv:
            return Execution("passed", [], None, 0, rc)
        return Execution("error", error_summary=f"vitest exit {rc}, no report: {tail}"[:500], rc=rc)
    report = json.loads(report_host.read_text(encoding="utf-8"))
    cases = parsers.parse_vitest_json(report, project.root,
                                      vt.get("container_root", (snap.root if snap else project.root).as_posix()))
    totals = parsers.totals_of(cases)
    status = "failed" if (totals["failed"] or totals["errors"] or rc == 1) else "passed"
    return Execution(status, cases, None, 0, rc)


def _vitest_call(project: Project, tier: dict[str, Any], argv: list[str], art: Path, tag: str,
                 snap: snapmod.Snapshot | None = None) -> Execution:
    """One `npx vitest ...` invocation in the vitest container; `argv` is everything after `npx vitest`."""
    vt = project.profile.get("vitest", {})
    if vt.get("kind") == "host":
        return _vitest_host_call(project, tier, argv, art, tag, snap)
    runtime = project.profile["runtime"]
    container = vt.get("container", runtime["container"])
    timeout_s = int(tier["timeout_s"])
    rt = {**runtime, "container": container, "ensure_up": vt.get("ensure_up")}
    problem = ensure_container(rt, project.root)
    if problem:
        return Execution("error", error_summary=problem)
    report_in = f"/tmp/{uuid.uuid4().hex}.json"
    cmd = ["docker", "exec", "-w", vt.get("workdir", "/app"), container, "timeout", "-s", "TERM", "-k", "20",
           str(timeout_s), "npx", "vitest", *argv, "--reporter=json", f"--outputFile={report_in}"]
    cmd += list(tier.get("args", []))
    out_log = art / "output.log"
    started = time.time()
    rc, _ = _run(cmd, timeout_s + 120, out_log)
    report_host = art / f"report{tag}.json"
    cp_rc = pull_file(container, report_in, report_host)
    _run(["docker", "exec", container, "rm", "-f", report_in], 30)
    tail = _summary(_tail(out_log))
    if rc in (124, 137) and time.time() - started >= timeout_s - 5:
        return Execution("timeout", error_summary=f"exceeded {timeout_s}s; {tail}"[:500], rc=rc)
    if cp_rc != 0 or not report_host.exists():
        if rc == 0 and "related" in argv:
            return Execution("passed", [], None, 0, rc)
        return Execution("error", error_summary=f"vitest exit {rc}, no report: {tail}"[:500], rc=rc)
    report = json.loads(report_host.read_text(encoding="utf-8"))
    cases = parsers.parse_vitest_json(report, project.root, vt.get("container_root", "/app"))
    totals = parsers.totals_of(cases)
    status = "failed" if (totals["failed"] or totals["errors"] or rc == 1) else "passed"
    return Execution(status, cases, None, 0, rc)


def run_vitest(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, art: Path,
               legion: LegionClient, target: str, snap: snapmod.Snapshot | None = None) -> Execution:
    vt = project.profile.get("vitest", {})
    vt_paths = vitest_container_paths(paths, str(vt.get("repo_subdir", "")))
    return _vitest_call(project, tier, ["run", *(vt_paths or list(tier.get("paths", [])))], art, "", snap)


def merge_executions(parts: list[Execution]) -> Execution:
    """Combine the pytest and vitest halves of one mixed `--paths` run into one verdict (worst status wins)."""
    order = {"passed": 0, "failed": 1, "error": 2, "timeout": 3}
    seen: dict[str, dict[str, Any]] = {}
    for part in parts:
        for case in part.cases:
            seen[case["node_id"]] = case
    worst = max(parts, key=lambda e: order.get(e.status, 2))
    summaries = [p.error_summary for p in parts if p.error_summary]
    reasons = [p.reason for p in parts if p.reason]
    return Execution(worst.status, list(seen.values()), "; ".join(summaries)[:500] or None,
                     sum(p.quarantined_deselected for p in parts), worst.rc, "; ".join(reasons) or None)


SERIAL_PHASE_DROPPED = ("serial_marker", "serial_min_executed", "serial_timeout_s", "floors_file", "testmon_build",
                        "testmon_datafile", "marker_unless_node_id", "mode")
PHASE_ORDER = {"passed": 0, "failed": 1, "error": 2, "timeout": 3}


def serial_phase_tier(tier: dict[str, Any]) -> dict[str, Any] | None:
    """The serial second phase of a whole pytest tier, or None when the tier declares no `serial_marker`.

    A test that grows one process by gigabytes (ADA's memory_heavy marker: TestMonteCarloOracle's 200,000-path draw)
    cannot share the container with a tier's xdist workers, so the tier's `marker` deselects it and this phase runs
    it after the workers have exited: one process (-n0), `-m serial_marker`, the tier's own paths and args (its -k).
    Floors and testmon data belong to the whole run, so this phase carries neither; `serial_min_executed` (default
    0) is its own executed-test floor and `serial_timeout_s` (default the tier's) its own budget.
    """
    marker = tier.get("serial_marker")
    if not marker:
        return None
    serial = {k: v for k, v in tier.items() if k not in SERIAL_PHASE_DROPPED}
    serial.update(marker=str(marker), workers=0, min_executed=int(tier.get("serial_min_executed", 0)),
                  timeout_s=int(tier.get("serial_timeout_s", tier["timeout_s"])))
    return serial


def merge_phases(project: Project, tier: dict[str, Any], parallel: Execution, serial: Execution) -> Execution:
    """One verdict from a tier's two phases.

    The worst status wins, the serial phase's own problem is labelled as such, the quarantine list (both phases
    deselect it) counts once, and the tier's floors are judged on every case the run executed: judged per phase, a
    floor whose tests all run in the serial phase reads as matching zero tests.
    """
    cases = list({c["node_id"]: c for c in [*parallel.cases, *serial.cases]}.values())
    worst = max((parallel, serial), key=lambda e: PHASE_ORDER.get(e.status, 2))
    status = worst.status
    summaries = [s for s in (parallel.error_summary,
                             f"serial phase: {serial.error_summary}" if serial.error_summary else None) if s]
    breach = check_floors(project.root / tier["floors_file"], cases) if tier.get("floors_file") else None
    if breach:
        summaries.append(f"consequence: {breach}" if status in ("error", "timeout") else breach)
        if status != "timeout":
            status = "error"
    reasons = [r for r in (parallel.reason, serial.reason) if r]
    return Execution(status, cases, "; ".join(summaries)[:500] or None,
                     max(parallel.quarantined_deselected, serial.quarantined_deselected), worst.rc,
                     "; ".join(reasons) or None)


def run_pytest_with_serial_phase(project: Project, tier: dict[str, Any], trigger: str, art: Path,
                                 legion: LegionClient, target: str,
                                 snap: snapmod.Snapshot | None = None) -> Execution:
    """A whole pytest tier that declares `serial_marker`: its xdist workers first, then the serial phase.

    The serial phase writes its own artifacts under `serial/` and runs on the snapshot the parallel phase built,
    so one verdict and one @sha cover both. A parallel phase that timed out (its budget is spent) or produced no
    cases at all (no container, no snapshot) ends the run there.

    This would pass trivially if the serial phase re-synced the snapshot (a commit landing between the phases makes
    them test different code under one @sha), if floors were judged per phase, or if a serial phase that selected
    nothing passed while the tier requires it to run something (a renamed class would drop out of the release gate
    silently); `serial_min_executed` turns that into an error, and testplatform/tests/test_serial_phase.py has a
    sabotage case for each beside its control.
    """
    serial = serial_phase_tier(tier)
    if serial is None:
        raise RunError("run_pytest_with_serial_phase needs a tier with a serial_marker")
    kw: dict[str, Any] = {"snap": snap} if snap else {}
    parallel = {k: v for k, v in tier.items() if k != "floors_file"}
    first = run_pytest(project, parallel, None, trigger, art, legion, target, **kw)
    if first.status == "timeout" or (first.status == "error" and not first.cases):
        return first
    serial_art = art / "serial"
    serial_art.mkdir(parents=True, exist_ok=True)
    second = run_pytest(project, serial, None, trigger, serial_art, legion, target, reuse_snapshot=True, **kw)
    return merge_phases(project, tier, first, second)


_SHA = re.compile(r"[0-9a-f]{7,40}")


def pin_snapshot_rev(project: Project, sha: str) -> Project:
    """`project` with its snapshot built from the commit the run reports instead of whatever HEAD is when the
    snapshot is taken.

    start_and_run records git_sha first and the snapshot read `rev: HEAD` later, after the container check and the
    orphan reap; a commit landing in between made the verdict's @sha name code the run never tested, and
    scripts/bitcoin_prod_release.py releases exactly that sha. Only the default HEAD rev is pinned, and only to a
    real sha: a dirty tree reports head+diffhash, whose head part is the committed tree a snapshot holds.
    """
    runtime = project.profile.get("runtime") or {}
    cfg = runtime.get("snapshot")
    head = sha.split("+", 1)[0]
    if not cfg or str(cfg.get("rev", "HEAD")) != "HEAD" or not _SHA.fullmatch(head):
        return project
    pinned = {**runtime, "snapshot": {**cfg, "rev": head}}
    return replace(project, profile={**project.profile, "runtime": pinned})


def run_vitest_for_changed(project: Project, tier: dict[str, Any], fe_paths: list[str], art: Path) -> Execution:
    """Vitest for frontend `--paths`: test files by path, source files through vitest's own `related` import graph."""
    vt = project.profile.get("vitest", {})
    container_paths = vitest_container_paths(fe_paths, str(vt.get("repo_subdir", "")))
    direct = [p for p in container_paths if VITEST_TEST_FILE.search(p)]
    sources = [p for p in container_paths if p not in direct]
    parts: list[Execution] = []
    if direct:
        parts.append(_vitest_call(project, tier, ["run", *direct], art, "-direct"))
    if sources:
        parts.append(_vitest_call(project, tier, ["related", *sources, "--run", "--passWithNoTests"], art, "-related"))
    merged = merge_executions(parts)
    if not merged.cases and merged.status == "passed":
        merged.reason = f"vitest found no test that is or imports {len(fe_paths)} frontend file(s)"
    return merged


def fuzz_exclusions(tier: dict[str, Any], root: Path | None = None) -> list[str]:
    """The tier's excluded operation path templates, in order and without duplicates: `exclude_paths` (a list, or a
    mapping of reason -> list; the reason keys document why each group is out of the run, schemathesis only sees the
    paths) plus the same shape read from `exclude_paths_file`, relative to the project root, so a long reason-grouped
    list can live next to the project's own gate that checks it. A named file that cannot be read or is not a list or
    mapping is a RunError: a fuzz run that silently drops its exclusions is the run that hangs on an event stream."""
    sources: list[Any] = [tier.get("exclude_paths") or []]
    if tier.get("exclude_paths_file"):
        path = (root or Path(".")) / str(tier["exclude_paths_file"])
        try:
            sources.append(yaml.safe_load(path.read_text(encoding="utf-8")) or [])
        except (OSError, yaml.YAMLError) as exc:
            raise RunError(f"fuzz exclude_paths_file {path}: {exc}") from exc
    out: list[str] = []
    for raw in sources:
        if not isinstance(raw, dict | list):
            raise RunError(f"fuzz exclusions must be a list or a reason -> list mapping, got {type(raw).__name__}")
        for group in list(raw.values()) if isinstance(raw, dict) else [raw]:
            for path in group or []:
                if str(path) not in out:
                    out.append(str(path))
    return out


def schemathesis_args(tier: dict[str, Any], report_in: str, root: Path | None = None) -> list[str]:
    """`schemathesis run` for a fuzz tier. The report is the NDJSON event stream, flushed one event per line, so a run
    killed at its budget still says which operations finished; the JUnit report is only written at exit."""
    base_url = tier.get("base_url", tier["schema_url"].rsplit("/", 1)[0])
    args = ["schemathesis", "run", tier["schema_url"], "--url", base_url,
            "--seed", str(tier.get("seed", 20260929)), "--max-examples", str(tier.get("max_examples", 5)),
            "--checks", str(tier.get("checks", "not_a_server_error")),
            "--report", "ndjson", "--report-ndjson-path", report_in, "--no-color"]
    for method in tier.get("methods", ["GET"]):
        args += ["--include-method", str(method)]
    for path in fuzz_exclusions(tier, root):
        args += ["--exclude-path", path]
    return args + [str(a) for a in tier.get("args", [])]


def interpret_schemathesis(rc: int, report: dict[str, Any] | None, tail: str, elapsed: float,
                           timeout_s: int) -> Execution:
    """Verdict of one fuzz run from its exit code and its parsed NDJSON report (None when none could be read).

    A run killed at its budget is `timeout` and still carries every operation that finished, so a run that spent
    its budget on a hung request reports what it did test instead of nothing (runs 3188 and 3242, 2026-10-03:
    0 cases after 3600 s). A run that exits before the engine's final event is an `error` with its partial cases.
    """
    cases = list(report["cases"]) if report else []
    done = sum(1 for c in cases if c["status"] != "skipped")
    if rc in (124, 137) and elapsed >= timeout_s - 5:
        if report is None:
            return Execution("timeout", error_summary=f"exceeded {timeout_s}s and left no event stream; {tail}"[:500],
                             rc=rc)
        return Execution("timeout", cases, (f"exceeded {timeout_s}s: {done} operations finished and are reported, "
                                            f"{report['unfinished']} scenarios still running at the kill; {tail}")[:500],
                         0, rc)
    if report is None:
        return Execution("error", error_summary=f"schemathesis exit {rc} and no event stream: {tail}"[:500], rc=rc)
    if not cases:
        return Execution("error", error_summary=f"schemathesis produced no test cases: {tail}"[:500], rc=rc)
    if not report["engine_finished"]:
        return Execution("error", cases, (f"schemathesis exit {rc} before the run finished: {done} operations "
                                          f"reported, {report['unfinished']} unfinished; {tail}")[:500], 0, rc)
    totals = parsers.totals_of(cases)
    status = "failed" if (totals["failed"] or totals["errors"]) else "passed"
    summary = None
    if report["stop_reason"] not in (None, "completed"):
        summary = f"schemathesis stopped early ({report['stop_reason']}) after {done} operations; {tail}"[:500]
    return Execution(status, cases, summary, 0, rc)


def gzip_in_place(path: Path) -> Path:
    """`path` -> `path.gz` (the raw NDJSON keeps every response body, tens of MB a night); the original on failure."""
    import gzip
    import shutil
    target = path.with_name(path.name + ".gz")
    try:
        with open(path, "rb") as src, gzip.open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
    except OSError:
        return path
    return target


def run_schemathesis(project: Project, tier: dict[str, Any], trigger: str, art: Path) -> Execution:
    runtime = project.profile["runtime"]
    timeout_s = int(tier["timeout_s"])
    problem = ensure_container(runtime, project.root)
    if problem:
        return Execution("error", error_summary=problem)
    tmp_dir = runtime.get("tmp_dir", "/tmp/testplatform")
    report_in = f"{tmp_dir}/{uuid.uuid4().hex}.ndjson"
    try:
        st_args = schemathesis_args(tier, report_in, project.root)
    except RunError as exc:
        return Execution("error", error_summary=str(exc)[:500])
    cmd = ["docker", "exec", "-w", runtime.get("workdir", "/app")]
    for key, value in (tier.get("env") or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [runtime["container"], "sh", "-c", f'mkdir -p {tmp_dir}; exec "$@"', "sh",
            "timeout", "-s", "TERM", "-k", "20", str(timeout_s)] + st_args
    out_log = art / "output.log"
    started = time.time()
    rc, _ = _run(cmd, timeout_s + 120, out_log)
    elapsed = time.time() - started
    report_host = art / "report.ndjson"
    cp_rc = pull_file(runtime["container"], report_in, report_host)
    _run(["docker", "exec", runtime["container"], "rm", "-f", report_in], 30)
    report: dict[str, Any] | None = None
    if cp_rc == 0 and report_host.exists():
        with open(report_host, encoding="utf-8", errors="replace") as fh:
            report = parsers.parse_schemathesis_ndjson(fh)
        gzip_in_place(report_host)
    return interpret_schemathesis(rc, report, _summary(_tail(out_log)), elapsed, timeout_s)


PLAYWRIGHT_PAGE_KEYS = ("path", "admin_mode", "assert", "assert_testid", "assert_absent_testid")
PLAYWRIGHT_FLOW_KEYS = ("name", "admin_mode")
PLAYWRIGHT_FLOW_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
"""The flow-name rule, written twice on purpose: this copy rejects a bad tier entry before anything is spawned, and the
ADA runner (`C:/code/ADA/.claude/skills/playwright-testing/runner.py`, `FLOW_NAME_PATTERN`) rejects it again before the import.
The two must stay the same string. Each repo pins the literal in its own test
(`test_the_flow_name_pattern_is_pinned_to_the_ada_runner_copy` in `tests/test_playwright_tier.py`, and
`test_the_flow_name_pattern_is_pinned_to_the_testctl_copy` in ADA's `playwright-testing/tests/test_runner_contract.py`), so
changing one side fails its own repo's test and names the other side."""
PLAYWRIGHT_BUDGET_ENV = "TESTCTL_ITEM_TIMEOUT_S"
"""The environment variable that tells a playwright runner its kill timeout in seconds, the same string as `ITEM_TIMEOUT_ENV` in the
ADA runner (`C:/code/ADA/.claude/skills/playwright-testing/runner.py`). `_run` throws away the output of a process it had to kill, so a
runner that outlives its timeout leaves its case with no verdict; told the budget, it bounds its own waits and prints first. Each repo pins
the literal in its own test (`test_the_budget_variable_is_pinned_to_the_ada_runner_copy` here,
`test_the_item_timeout_variable_is_pinned_to_the_testctl_copy` in ADA's `playwright-testing/tests/test_runner_contract.py`)."""
ADMIN_SUFFIX = " [admin]"


def _playwright_entry(where: str, entry: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise RunError(f"{where} must be a mapping with keys {', '.join(allowed)}, got {type(entry).__name__}")
    unknown = sorted(set(entry) - set(allowed))
    if unknown:
        raise RunError(f"{where} has unknown key(s) {', '.join(map(str, unknown))} (allowed: {', '.join(allowed)}); "
                       "a typo here would run a weaker check than the one written")
    admin = entry.get("admin_mode", False)
    if not isinstance(admin, bool):
        raise RunError(f"{where} admin_mode must be true or false, got {admin!r}")
    return entry


def _playwright_text(where: str, entry: dict[str, Any], key: str) -> str | None:
    if key not in entry:
        return None
    value = entry[key]
    if not isinstance(value, str) or not value:
        raise RunError(f"{where} {key} must be a non-empty string, got {value!r}")
    return value


def playwright_items(tier: dict[str, Any], base: str) -> list[dict[str, Any]]:
    """A playwright tier's `pages` and `flows` as the ordered list of things to run.

    A page is a string (a plain smoke, unchanged since the tier type was written) or a mapping
    `{path, admin_mode?, assert?, assert_testid?, assert_absent_testid?}` turned into the matching `runner.py smoke` flags
    (`assert_absent_testid` is the negative twin of `assert_testid`: the page must NOT render a visible element with that
    `data-testid`; the same page without `admin_mode` is the read-only smoke). A flow is a
    mapping `{name, admin_mode?}` run as `runner.py flow <name> <base_url> [--admin-mode]`. Every item carries a
    `label`, the case name (`e2e::<label>`), which says which page or flow and which mode: `/clients?tab=rules
    [admin]`, `flow:admin_banner_probe [admin]`. A string page keeps its path-only name; when two items would
    share a name (the 18 `/labs/bitcoin?tab=...` pages all reduced to `/labs/bitcoin`, so one failing tab shared
    a case with seventeen passing ones) a later string page is named by its full path and query, and any
    remaining twin gets ` #2`, ` #3`.

    Unknown keys, a non-boolean `admin_mode`, an empty assertion and one testid both required and forbidden are errors,
    never ignored: a mistyped `admin-mode: true` would otherwise run the page without Admin Mode and report a pass for a
    check that was never made.
    """
    pages = tier.get("pages") or []
    flows = tier.get("flows") or []
    if not isinstance(pages, list) or not isinstance(flows, list):
        raise RunError("a playwright tier's pages and flows must be lists")
    items: list[dict[str, Any]] = []
    for i, page in enumerate(pages):
        where = f"playwright tier pages[{i}]"
        if isinstance(page, str):
            url = f"{base}{page}"
            items.append({"kind": "page", "label": urlparse(url).path or "/", "full_label": page, "url": url,
                          "argv": ["smoke", url], "admin_mode": False, "title": url, "legacy": True})
            continue
        entry = _playwright_entry(where, page, PLAYWRIGHT_PAGE_KEYS)
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise RunError(f"{where} needs a string path starting with '/', got {path!r}")
        admin = bool(entry.get("admin_mode", False))
        url = f"{base}{path}"
        argv = ["smoke", url] + (["--admin-mode"] if admin else [])
        text = _playwright_text(where, entry, "assert")
        testid = _playwright_text(where, entry, "assert_testid")
        absent = _playwright_text(where, entry, "assert_absent_testid")
        if testid is not None and testid == absent:
            raise RunError(f"{where} requires and forbids the same data-testid {testid!r}: nothing can satisfy both")
        if text is not None:
            argv.append(f"--assert={text}")
        if testid is not None:
            argv.append(f"--assert-testid={testid}")
        if absent is not None:
            argv.append(f"--assert-absent-testid={absent}")
        suffix = ADMIN_SUFFIX if admin else ""
        items.append({"kind": "page", "label": path + suffix, "full_label": path + suffix, "url": url,
                      "argv": argv, "admin_mode": admin, "title": url + suffix, "legacy": False})
    for i, flow in enumerate(flows):
        where = f"playwright tier flows[{i}]"
        entry = _playwright_entry(where, flow, PLAYWRIGHT_FLOW_KEYS)
        name = entry.get("name")
        if not isinstance(name, str) or not PLAYWRIGHT_FLOW_NAME.fullmatch(name):
            raise RunError(f"{where} needs a name that is a plain identifier, got {name!r}")
        admin = bool(entry.get("admin_mode", False))
        suffix = ADMIN_SUFFIX if admin else ""
        argv = ["flow", name, base] + (["--admin-mode"] if admin else [])
        items.append({"kind": "flow", "label": f"flow:{name}{suffix}", "full_label": f"flow:{name}{suffix}",
                      "url": base, "argv": argv, "admin_mode": admin, "title": f"flow:{name}{suffix}",
                      "legacy": False})
    if not items:
        raise RunError("playwright tier lists no pages and no flows")
    seen: set[str] = set()
    for item in items:
        label = item["label"]
        if label in seen and item["legacy"]:
            label = item["full_label"]
        base_label, n = label, 2
        while label in seen:
            label = f"{base_label} #{n}"
            n += 1
        seen.add(label)
        item["label"] = label
    return items


def last_json_object(text: str) -> dict[str, Any]:
    """The last line of runner output that parses as a JSON object; `{}` when none does."""
    for line in reversed(text.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def run_playwright(project: Project, tier: dict[str, Any], art: Path) -> Execution:
    """Run every page and flow of a playwright tier through the project's runner, one process each.

    An item gets `per_item` seconds, its kill timeout, and the same number in `PLAYWRIGHT_BUDGET_ENV` so the runner can print its
    verdict before the kill (a runner that ignores the variable behaves as before: killed at the timeout, case failed with no verdict).
    """
    runner = project.root / tier.get("runner", ".claude/skills/playwright-testing/runner.py")
    base = tier["base_url"].rstrip("/")
    timeout_s = int(tier["timeout_s"])
    try:
        items = playwright_items(tier, base)
    except RunError as exc:
        return Execution("error", error_summary=str(exc)[:500])
    per_item = max(30, timeout_s // len(items))
    results: list[dict[str, Any]] = []
    log = art / "output.log"
    deadline = time.time() + timeout_s
    for item in items:
        record = {"url": item["url"], "label": item["label"], "kind": item["kind"], "argv": item["argv"],
                  "admin_mode": item["admin_mode"], "budget_s": per_item}
        if time.time() > deadline:
            results.append({**record, "returncode": 124, "stderr": "", "duration_ms": 0,
                            "output": {"status": "failure", "reason": "tier deadline reached before this item ran"}})
            continue
        started = time.time()
        proc_rc, text = _run([sys.executable, str(runner), *item["argv"]], per_item,
                             env={PLAYWRIGHT_BUDGET_ENV: str(per_item)})
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"== {item['title']} rc={proc_rc}\n{text}\n")
        results.append({**record, "returncode": proc_rc, "output": last_json_object(text), "stderr": text[-300:],
                        "duration_ms": (time.time() - started) * 1000})
    cases = parsers.parse_playwright_smoke(results)
    (art / "report.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    if not cases:
        return Execution("error", error_summary="no e2e pages ran")
    totals = parsers.totals_of(cases)
    return Execution("failed" if totals["failed"] else "passed", cases, None, 0, 0)


def run_commands(project: Project, tier: dict[str, Any], art: Path, snap: snapmod.Snapshot | None = None) -> Execution:
    """Static gates: each configured command is one case; exit 0 passes, 1 fails, 2 or a timeout errors."""
    import hashlib
    import shutil
    specs = tier.get("commands") or []
    if not specs:
        return Execution("error", error_summary="commands tier has no commands")
    deadline = time.time() + int(tier["timeout_s"])
    cases: list[dict[str, Any]] = []
    log = art / "output.log"
    live = project.root.as_posix()
    for spec in specs:
        name = spec["name"]
        # `{repo}` is the LIVE repo root, for the untracked runtime files a snapshot does not carry (the .venv python).
        argv = [sys.executable if a == "python" and i == 0 else a.replace("{repo}", live)
                for i, a in enumerate(spec["run"])]
        if argv[0] != sys.executable and not os.path.isabs(argv[0]):
            argv[0] = shutil.which(argv[0]) or argv[0]  # Windows: `npm` is npm.cmd, which CreateProcess will not find bare
        left = deadline - time.time()
        node_id = f"{tier.get('case_prefix', 'gates')}::{name}"
        started = time.time()
        if left <= 0:
            rc, text = 124, "tier deadline reached before this gate ran"
        else:
            env = {k: str(v).replace("{repo}", live) for k, v in (spec.get("env") or {}).items()}
            if snap:
                env.update({"TP_LIVE_ROOT": live, "TP_SNAPSHOT_ROOT": snap.root.as_posix()})
            rc, text = _run(argv, min(float(spec.get("timeout_s", 900)), left), cwd=snap.root if snap else project.root,
                            env=env)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"== {name} rc={rc}\n{text[-6000:]}\n")
        status = "passed" if rc == 0 else ("failed" if rc == 1 else "error")
        case: dict[str, Any] = {
            "node_id": node_id, "file": spec.get("file", node_id.split("::")[0]), "status": status,
            "duration_ms": int((time.time() - started) * 1000), "attempts": 1,
            "body_hash": hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
            "feature_slug": None, "requirement_ids": []}
        if rc != 0:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            message = (lines[-1] if lines else f"exit {rc}")
            case["failure"] = parsers.make_failure("GateFailed" if rc == 1 else "GateError",
                                                   f"{name}: {message}", "\n".join(lines))
        cases.append(case)
    totals = parsers.totals_of(cases)
    status = "error" if totals["errors"] and not totals["failed"] else ("failed" if totals["failed"] or totals["errors"] else "passed")
    summary = f"{totals['errors']} gates errored" if status == "error" else None
    return Execution(status, cases, summary, 0, 0)


def run_changed_paths(project: Project, tier: dict[str, Any], trigger: str, art: Path, legion: LegionClient,
                      target: str, changed_paths: list[str]) -> Execution:
    """`changed --paths`: python files select pytest tests, frontend files run vitest; a mixed set runs both.

    This path would pass trivially if a path set no leg of it can act on (docs, yaml, a frontend file in a
    project with no vitest_path_tier) returned an empty passed run; start_and_run refuses that as NO TESTS SELECTED.
    """
    vt_tier_name = project.profile.get("vitest_path_tier")
    vt_tier = project.tier(vt_tier_name) if vt_tier_name else None
    subdir = str(project.profile.get("vitest", {}).get("repo_subdir", ""))
    fe, rest = split_frontend_paths(changed_paths, subdir) if vt_tier else ([], list(changed_paths))
    parts: list[Execution] = []
    if fe and vt_tier:
        parts.append(run_vitest_for_changed(project, vt_tier, fe, art))
    if rest or not fe:
        parts.append(run_pytest(project, tier, None, trigger, art, legion, target, rest or list(changed_paths)))
    return parts[0] if len(parts) == 1 else merge_executions(parts)


def zero_test_refusal(framework: str | None, tier: dict[str, Any], changed_paths: list[str] | None,
                      exe: Execution, totals: dict[str, int]) -> str | None:
    """One-line refusal when a run that must execute tests executed none, else None.

    This gate would pass trivially if it only inspected `exe.status`: a zero-test run reports `passed`, which is
    exactly the defect (testctl runs #227 and #248, 2026-09-29). It therefore reads the executed count itself and
    only stands down for tiers that do not run tests (commands, playwright, schemathesis), a non-passed run, and a
    testmon `changed` run without paths, where zero selected tests legitimately means nothing depends on the edit.
    """
    if exe.status != "passed" or framework not in ("pytest", "vitest"):
        return None
    if totals["total"] - totals["skipped"] > 0:
        return None
    if tier.get("mode") == "changed" and not changed_paths:
        return None
    if changed_paths:
        return f"NO TESTS SELECTED for {len(changed_paths)} paths ({exe.reason or 'no test depends on them'})"
    return f"NO TESTS EXECUTED by tier ({exe.reason or 'zero tests ran'})"


def execute_framework(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, art: Path,
                      legion: LegionClient, target: str, changed_paths: list[str] | None = None) -> Execution:
    """Run one tier. A whole tier on a host-read runtime with `runtime.snapshot` runs against a per-run extract of the
    committed tree (see snapshot.py); path and `--paths` targets stay on the live tree."""
    framework = tier.get("framework", project.profile.get("framework"))
    runtime = project.profile.get("runtime", {})
    vt_kind = project.profile.get("vitest", {}).get("kind")
    if not snapmod.wanted(runtime, tier, framework, vt_kind, paths, changed_paths):
        return _execute_framework(project, tier, paths, trigger, art, legion, target, changed_paths, None)
    try:
        snap = snapmod.create(project.root, runtime["snapshot"], art)
    except snapmod.SnapshotError as exc:
        return Execution("error", error_summary=f"could not build the committed-tree snapshot: {exc}"[:500])
    (art / "snapshot.log").write_text(snap.describe() + "\n", encoding="utf-8")
    try:
        return _execute_framework(project, tier, paths, trigger, art, legion, target, changed_paths, snap)
    finally:
        try:
            snap.cleanup()
        except (OSError, snapmod.SnapshotError) as exc:
            with open(art / "snapshot.log", "a", encoding="utf-8") as fh:
                fh.write(f"snapshot cleanup failed: {exc}\n")


def _execute_framework(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, art: Path,
                       legion: LegionClient, target: str, changed_paths: list[str] | None,
                       snap: snapmod.Snapshot | None) -> Execution:
    framework = tier.get("framework", project.profile.get("framework"))
    kw: dict[str, Any] = {"snap": snap} if snap else {}
    if framework == "pytest" and changed_paths and tier.get("mode") == "changed":
        return run_changed_paths(project, tier, trigger, art, legion, target, changed_paths)
    if framework == "pytest" and tier.get("serial_marker") and not paths and not changed_paths:
        return run_pytest_with_serial_phase(project, tier, trigger, art, legion, target, snap)
    if framework == "pytest":
        return run_pytest(project, tier, paths, trigger, art, legion, target, changed_paths, **kw)
    if framework == "vitest":
        return run_vitest(project, tier, paths, trigger, art, legion, target, **kw)
    if framework == "schemathesis":
        return run_schemathesis(project, tier, trigger, art)
    if framework == "playwright":
        return run_playwright(project, tier, art)
    if framework == "commands":
        return run_commands(project, tier, art, **kw)
    raise RunError(f"unsupported framework {framework}")


def local_verdict(status: str, totals: dict[str, int], cases: list[dict[str, Any]], error_summary: str | None,
                  note: str) -> str:
    lines = [f"{status.upper()} {totals['passed']}/{totals['total']} passed, {totals['failed']} failed, "
             f"{totals['errors']} errors, {totals['skipped']} skipped, {totals['rerun_passed']} rerun_passed ({note})"]
    if error_summary:
        lines.append(f"error: {error_summary}"[:220])
    bad = [c for c in cases if c["status"] in ("failed", "error")]
    for c in bad[:8]:
        f = c.get("failure") or {}
        lines.append(f"FAIL {c['node_id']}: {f.get('type', '')}: {f.get('message', '')}".replace("\n", " ")[:220])
    if len(bad) > 8:
        lines.append(f"+{len(bad) - 8} more")
    return "\n".join(lines[:15])


def _iso(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(ts or time.time(), tz=dt.UTC).isoformat()


def _finish_batch(lock: Any, exe: Execution, own_targets: list[str], peers: list[dict[str, Any]],
                  run_id: int | None, art: Path, elapsed: float) -> Execution:
    """Split the shared run, hand every claimed waiter its own slice (or a retry), return the leader's own slice."""
    parts = split_execution(exe, [own_targets] + [list(p["spec"]["targets"]) for p in peers])
    for peer, part in zip(peers, parts[1:], strict=False):
        if part is None:
            lock.deliver(peer["ticket"], {"retry": True, "reason": f"shared run {exe.status} without per-test results"})
            continue
        lock.deliver(peer["ticket"], {"execution": _execution_payload(part), "leader_run_id": run_id,
                                      "leader_artifact": art.as_posix(), "elapsed": round(elapsed, 2),
                                      "batch_size": len(peers) + 1})
    (art / "batch.json").write_text(json.dumps({"leader": True, "members": len(peers) + 1,
                                                "targets": [t for p in peers for t in p["spec"]["targets"]]}),
                                    encoding="utf-8")
    return parts[0] if parts[0] is not None else exe


DEFAULT_BATCH_MAX_PEERS = 5
DEFAULT_BATCH_MAX_FILES = 8


class NullLock:
    """Stands in for a lane slot when a request runs inside another process's pytest invocation."""

    lane = LIGHT
    stale_taken = None
    mine = False

    def set_run_id(self, run_id: int) -> None:
        return None

    def release(self) -> None:
        return None


def batch_spec(project: Project, target: str | None, trigger: str, changed_paths: list[str] | None,
               key: str | None = None) -> dict[str, Any] | None:
    """What a queued light request publishes so another process may run it in the same pytest invocation, or None
    when it must run alone (a whole tier, a `changed --paths` mapping, any non-pytest tier).

    Requests are only combined when they select tests the same way: the path tier's marker filter is dropped as soon
    as one named target carries a node id, so node-id requests and plain file requests never share a process.
    """
    if os.environ.get("TESTCTL_BATCH") == "0":
        return None
    tier_name, tier, paths = resolve_target(project, target)
    framework = tier.get("framework", project.profile.get("framework"))
    if not paths or changed_paths or framework != "pytest" or tier_name != project.profile.get("path_tier"):
        return None
    node = any("::" in p for p in paths)
    return {"batch_class": f"{tier_name}|{trigger}|{'node' if node else 'file'}", "targets": list(paths),
            "target": target or project.default_target, "trigger": trigger}


def _targets_match(node_id: str, targets: list[str]) -> bool:
    node = node_id.replace("\\", "/")
    for raw in targets:
        t = raw.replace("\\", "/").rstrip("/")
        if node == t or node.startswith(t + "::") or node.startswith(t + "/"):
            return True
    return False


def split_execution(exe: Execution, members: list[list[str]]) -> list[Execution | None]:
    """One shared pytest Execution -> one Execution per member (their own targets' cases, their own verdict).

    None for every member when the shared run produced no per-test evidence (timeout, killed, no report): such a run
    says nothing about any single request, so each of them runs again on its own.
    """
    if exe.status in ("timeout",) or not exe.cases:
        return [None for _ in members]
    out: list[Execution | None] = []
    for targets in members:
        cases = [c for c in exe.cases if _targets_match(str(c["node_id"]), targets)]
        totals = parsers.totals_of(cases)
        if totals["errors"] and not totals["failed"]:
            status = "error"
        else:
            status = "failed" if (totals["failed"] or totals["errors"]) else "passed"
        summary = f"collection/setup errors in {totals['errors']} items: see the batch log"[:500] if status == "error" else None
        out.append(Execution(status, cases, summary, exe.quarantined_deselected, exe.rc, exe.reason))
    return out


def _execution_payload(exe: Execution) -> dict[str, Any]:
    return {"status": exe.status, "cases": exe.cases, "error_summary": exe.error_summary,
            "quarantined_deselected": exe.quarantined_deselected, "rc": exe.rc, "reason": exe.reason}


def execution_from_payload(data: dict[str, Any]) -> Execution:
    return Execution(str(data["status"]), list(data.get("cases") or []), data.get("error_summary"),
                     int(data.get("quarantined_deselected") or 0), data.get("rc"), data.get("reason"))


def wait_and_run(project: Project, target: str | None, trigger: str, legion: LegionClient, lock: ProjectLock,
                 key: str, changed_paths: list[str] | None, timeout_s: float,
                 on_wait: Callable[[int, Any], None] | None = None, schedule_id: int | None = None,
                 on_dequeued: Callable[[], None] | None = None,
                 artifacts_root: Path = ARTIFACTS_ROOT) -> Outcome | None:
    """Queue for a lane slot and run the request, or receive its result from the leader that ran it inside its own
    pytest process; None when the queue wait timed out.

    This path would pass trivially if a handed-off request were reported without its own Legion run: the handoff
    branch therefore goes through `start_and_run` with the leader's slice, so every request still gets its own run
    row, verdict and artifact directory.
    """
    spec = batch_spec(project, target, trigger, changed_paths, key)
    deadline = time.time() + timeout_s
    while True:
        state, payload = lock.wait_slot_or_handoff(key, max(1.0, deadline - time.time()), on_wait=on_wait, spec=spec)
        if on_dequeued is not None:
            on_dequeued()
        if state == "slot":
            try:
                return start_and_run(project, target, trigger, legion, lock, artifacts_root=artifacts_root,
                                     changed_paths=changed_paths, schedule_id=schedule_id)
            finally:
                lock.release()
        if state == "handoff" and payload is not None and payload.get("retry"):
            continue
        if state == "handoff" and payload is not None:
            return start_and_run(project, target, trigger, legion, NullLock(), artifacts_root=artifacts_root,
                                 changed_paths=changed_paths, schedule_id=schedule_id,
                                 precomputed=execution_from_payload(payload["execution"]),
                                 batched_with={"leader_run_id": payload.get("leader_run_id"),
                                               "leader_artifact": payload.get("leader_artifact"),
                                               "elapsed": payload.get("elapsed", 0),
                                               "batch_size": payload.get("batch_size")})
        return None


def start_and_run(project: Project, target: str | None, trigger: str, legion: LegionClient,
                  lock: ProjectLock, on_started: Callable[[int | None, Path], None] | None = None,
                  artifacts_root: Path = ARTIFACTS_ROOT, changed_paths: list[str] | None = None,
                  schedule_id: int | None = None, precomputed: Execution | None = None,
                  batched_with: dict[str, Any] | None = None) -> Outcome:
    tier_name, tier, paths = resolve_target(project, target)
    target_label = target or project.default_target
    peers: list[dict[str, Any]] = []
    own_spec = None
    if precomputed is None and lock.lane == LIGHT and getattr(lock, "mine", False):
        own_spec = batch_spec(project, target, trigger, changed_paths)
        if own_spec is not None:
            lanes = project.profile.get("lanes") or {}
            peers = lock.claim_peers(own_spec["batch_class"], int(lanes.get("batch_max_peers", DEFAULT_BATCH_MAX_PEERS)),
                                     int(lanes.get("batch_max_files", DEFAULT_BATCH_MAX_FILES)))
    if peers:
        union = list(paths or [])
        for peer in peers:
            union += [t for t in peer["spec"]["targets"] if t not in union]
    else:
        union = paths
    try:
        drain_pending(legion, artifacts_root, DRAIN_MAX_ITEMS, DRAIN_BUDGET_S)
    except OSError:
        pass
    framework = tier.get("framework", project.profile.get("framework"))
    art = new_artifact_dir(project.name, artifacts_root)
    sha = git_sha(project.root)
    started_at = time.time()
    run_id: int | None = None
    create_error: LegionError | None = None
    try:
        create_body = {
            "project_id": project.legion_project_id, "target": target_label[:MAX_TARGET_LEN], "tier": tier_name, "trigger": trigger,
            "git_sha": sha, "framework": framework, "runner_host": socket.gethostname(),
            "artifact_path": art.as_posix()}
        if schedule_id is not None:
            create_body["schedule_id"] = schedule_id
        run_id = legion.create_run(create_body)
        lock.set_run_id(run_id)
    except LegionError as exc:
        create_error = exc
        (art / "legion_error.txt").write_text(str(exc), encoding="utf-8")
    if on_started:
        on_started(run_id, art)
    if lock.stale_taken and lock.stale_taken.run_id:
        try:
            legion.post_results(int(lock.stale_taken.run_id), {
                "status": "error", "completed_at": _iso(), "duration_s": 0,
                "totals": {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "rerun_passed": 0},
                "error_summary": "runner process died before completing this run", "cases": []})
        except LegionError:
            pass
    delivered = not peers
    try:
        try:
            exe = precomputed if precomputed is not None else execute_framework(
                pin_snapshot_rev(project, sha), tier, union, trigger, art, legion, target_label, changed_paths)
        except Exception as exc:
            exe = Execution("error", error_summary=f"runner exception: {type(exc).__name__}: {exc}"[:500])
        if peers:
            exe = _finish_batch(lock, exe, list(paths or []), peers, run_id, art, time.time() - started_at)
            delivered = True
    finally:
        if not delivered:
            for peer in peers:
                lock.deliver(peer["ticket"], {"retry": True, "reason": "leader failed before delivering"})
    duration = float(batched_with["elapsed"]) if batched_with else time.time() - started_at
    if batched_with:
        (art / "batch.json").write_text(json.dumps(batched_with), encoding="utf-8")
    totals = parsers.totals_of(exe.cases)
    refusal = zero_test_refusal(framework, tier, changed_paths, exe, totals)
    if refusal:
        exe = Execution("error", exe.cases, refusal, exe.quarantined_deselected, exe.rc, exe.reason)
    sparse_target = target_label == "changed" and trigger != "schedule"
    known: dict[str, str] = {}
    if sparse_target:
        try:
            known = legion.hashes(project.legion_project_id)
        except LegionError:
            known = {}
    rows = parsers.select_rows(exe.cases, known, sparse_target)
    if sum(len(json.dumps(c)) for c in rows) > parsers.MAX_PAYLOAD_BYTES:
        if not known:
            try:
                known = legion.hashes(project.legion_project_id)
            except LegionError:
                known = {}
        rows = parsers.fit_rows(rows, known)
    payload: dict[str, Any] = {
        "status": exe.status, "completed_at": _iso(), "duration_s": round(duration, 2), "totals": totals,
        "error_summary": exe.error_summary, "cases": rows, "quarantined_deselected": exe.quarantined_deselected}
    (art / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    text = ""
    if run_id is not None:
        try:
            resp = legion.post_results(run_id, payload)
            text = str(resp.get("text") or "")
            (art / "verdict.json").write_text(json.dumps(resp, indent=1), encoding="utf-8")
            if not text:
                text = local_verdict(exe.status, totals, exe.cases, exe.error_summary, "no verdict text from Legion")
        except LegionError as exc:
            text = local_verdict(exe.status, totals, exe.cases, exe.error_summary,
                                 f"Legion ingest failed, saved for replay: {str(exc)[:60]}")
            (art / "pending_ingest.json").write_text(json.dumps({"project": project.name, "run_id": run_id,
                                                                  "payload": payload}), encoding="utf-8")
    elif create_error is not None and not retryable(create_error):
        text = local_verdict(exe.status, totals, exe.cases, exe.error_summary,
                             f"Legion rejected the run, not saved for replay: {str(create_error)[:80]}")
    else:
        text = local_verdict(exe.status, totals, exe.cases, exe.error_summary, "Legion unreachable, saved for replay")
        (art / "pending_ingest.json").write_text(json.dumps({"project": project.name, "run_id": None, "payload": payload,
                                                              "create_run": {
                                                                  "project_id": project.legion_project_id,
                                                                  "target": target_label[:MAX_TARGET_LEN], "tier": tier_name,
                                                                  "trigger": trigger, "git_sha": sha,
                                                                  "framework": framework,
                                                                  "runner_host": socket.gethostname(),
                                                                  "artifact_path": art.as_posix()}}), encoding="utf-8")
    if refusal:
        text = refusal
    (art / "verdict.txt").write_text(text, encoding="utf-8")
    return Outcome(run_id, exe.status, text, art)


MAX_TARGET_LEN = 200
CLAIM_STALE_S = 900
DRAIN_MAX_ITEMS = 5
DRAIN_BUDGET_S = 90.0


def _release_stale_claims(root: Path, now: float) -> None:
    for claim in root.glob("*/*/*/ingesting.*.json"):
        try:
            if now - claim.stat().st_mtime > CLAIM_STALE_S:
                claim.rename(claim.with_name("pending_ingest.json"))
        except OSError:
            continue


def drain_pending(legion: LegionClient, root: Path = ARTIFACTS_ROOT, max_items: int | None = None,
                  budget_s: float | None = None) -> list[str]:
    done: list[str] = []
    started = time.time()
    _release_stale_claims(root, started)
    taken = 0
    for pending in sorted(root.glob("*/*/*/pending_ingest.json")):
        if max_items is not None and taken >= max_items:
            break
        if budget_s is not None and time.time() - started > budget_s:
            break
        claim = pending.with_name(f"ingesting.{os.getpid()}.{threading.get_ident()}.json")
        try:
            pending.rename(claim)
        except OSError:
            continue
        taken += 1
        try:
            data = json.loads(claim.read_text(encoding="utf-8"))
            run_id = data.get("run_id")
            if run_id is None:
                run_id = legion.create_run(data["create_run"])
                data["run_id"] = run_id
                claim.write_text(json.dumps(data), encoding="utf-8")
            resp = legion.post_results(int(run_id), data["payload"])
        except LegionError as exc:
            if retryable(exc):
                claim.rename(pending)
                done.append(f"failed {pending.parent.name}: {str(exc)[:80]}")
                break
            claim.rename(pending.with_name("pending_ingest.rejected.json"))
            done.append(f"rejected {pending.parent.name}: {str(exc)[:80]}")
            continue
        except (OSError, ValueError, KeyError) as exc:
            claim.rename(pending.with_name("pending_ingest.bad.json"))
            done.append(f"unreadable {pending.parent.name}: {type(exc).__name__}")
            continue
        (pending.parent / "verdict.txt").write_text(str(resp.get("text") or ""), encoding="utf-8")
        claim.rename(pending.with_name("ingested.json"))
        done.append(f"run {run_id} <- {pending.parent.name}")
    return done


def replay_pending(legion: LegionClient, root: Path = ARTIFACTS_ROOT) -> list[str]:
    return drain_pending(legion, root)


def attach_and_wait(legion: LegionClient, lock: ProjectLock, timeout_s: int = 5400) -> Outcome:
    held = lock.wait_for_run_id()
    if held is None or held.run_id is None:
        return Outcome(None, "error", "attach failed: the in-flight run has not registered a run id", None, True)
    deadline = time.time() + timeout_s
    failures = 0
    while time.time() < deadline:
        try:
            run = legion.run(int(held.run_id))
        except LegionError as exc:
            failures += 1
            if failures >= 5:
                return Outcome(int(held.run_id), "error", f"attach failed: {exc}", None, True)
            time.sleep(5)
            continue
        failures = 0
        status = str(run.get("status", "running"))
        if status != "running":
            text = run.get("text") or (run.get("verdict") or {}).get("text") or run.get("verdict_text") or \
                f"run {held.run_id} finished: {status}"
            return Outcome(int(held.run_id), status, str(text), None, True)
        time.sleep(3)
    return Outcome(int(held.run_id), "timeout", f"attached run {held.run_id} still running after {timeout_s}s", None, True)
