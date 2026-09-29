from __future__ import annotations

import datetime as dt
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import parsers, selection as sel
from .artifacts import new_artifact_dir
from .legion import LegionClient, LegionError
from .lock import ProjectLock
from .profile import ARTIFACTS_ROOT, Project

TRIGGERS = ("claude", "schedule", "hook", "manual")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
OUTPUT_TAIL_LINES = 12
SCHEDULE_RERUNS = 2
MAX_DESELECT = 400


class RunError(Exception):
    pass


@dataclass
class Execution:
    status: str
    cases: list[dict[str, Any]] = field(default_factory=list)
    error_summary: str | None = None
    quarantined_deselected: int = 0
    rc: int | None = None


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
    try:
        return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=20, creationflags=NO_WINDOW).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def resolve_target(project: Project, target: str | None) -> tuple[str, dict[str, Any], list[str] | None]:
    target = target or project.default_target
    tier = project.tier(target)
    if tier is not None:
        return target, tier, None
    path_tier = project.profile.get("path_tier")
    if not path_tier:
        raise RunError(f"target {target!r} is not a tier ({', '.join(project.tier_names)}) and the profile has no path_tier")
    return path_tier, project.tier(path_tier), [target]


def preflight(project: Project, target: str | None, changed_paths: list[str] | None) -> None:
    """Refuse before a Legion run row exists when the request cannot be scoped."""
    _, tier, _ = resolve_target(project, target)
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


def _summary(text: str, limit: int = 500) -> str:
    return " | ".join(ln.strip() for ln in text.splitlines() if ln.strip())[-limit:]


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


def _pytest_args(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str,
                 report_file: str, deselect: list[str], extra: list[str]) -> list[str]:
    cfg = project.profile.get("pytest", {})
    prefix = cfg.get("path_prefix", "")

    def strip(value: str) -> str:
        return value[len(prefix):] if prefix and value.startswith(prefix) else value

    args = list(cfg.get("command", ["python", "-m", "pytest"]))
    args += [strip(p) for p in (paths or tier.get("paths") or cfg.get("paths") or [])]
    deselect = [strip(d) for d in deselect]
    args += list(cfg.get("common_args", []))
    workers = int(tier.get("workers", cfg.get("workers", 0)))
    args += ["-n", str(workers), "--dist", "loadfile"] if workers > 0 else ["-n0"]
    if tier.get("marker"):
        args += ["-m", tier["marker"]]
    args += list(tier.get("args", []))
    args += extra
    if trigger == "schedule":
        args += ["--reruns", str(SCHEDULE_RERUNS)]
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


def run_pytest(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, art: Path,
               legion: LegionClient, target: str, changed_paths: list[str] | None = None) -> Execution:
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
    env: dict[str, str] = dict(runtime.get("env", {}))
    if tier.get("mode") == "changed":
        data_file = tier.get("testmon_datafile", "/tmp/testplatform/.testmondata")
        env["TESTMON_DATAFILE"] = data_file
        if changed_paths:
            mapped, selection = sel.select_for_paths(project.root, runtime, data_file, changed_paths,
                                                     tier.get("test_globs", []))
            if not mapped:
                (art / "selection.json").write_text(json.dumps(selection, indent=1), encoding="utf-8")
                return Execution("passed", [], None, 0, 0)
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
    (art / "selection.json").write_text(json.dumps(selection, indent=1), encoding="utf-8")

    report_name = f"{uuid.uuid4().hex}.json"
    out_log = art / "output.log"
    report_host = art / "report.json"
    timeout_prefix = ["timeout", "-s", "TERM", "-k", "20", str(timeout_s)]
    started = time.time()
    if runtime["kind"] == "exec":
        problem = ensure_container(runtime, project.root)
        if problem:
            return Execution("error", error_summary=problem)
        tmp_dir = runtime.get("tmp_dir", "/tmp/testplatform")
        report_in = f"{tmp_dir}/{report_name}"
        pytest_args = _pytest_args(project, tier, paths, trigger, report_in, quarantined, extra)
        cmd = ["docker", "exec", "-w", runtime.get("workdir", "/app")]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [runtime["container"], "sh", "-c", f'mkdir -p {tmp_dir}; exec "$@"', "sh"] + timeout_prefix + pytest_args
        rc, _ = _run(cmd, timeout_s + 120, out_log)
        cp_rc = pull_file(runtime['container'], report_in, report_host)
        _run(["docker", "exec", runtime["container"], "rm", "-f", report_in], 30)
    else:
        report_in = f"/out/{report_name}"
        pytest_args = _pytest_args(project, tier, paths, trigger, report_in, quarantined, extra)
        cmd = ["docker", "run", "--rm", "--init", "--entrypoint", "sh", "-w", runtime.get("workdir", "/app")]
        if runtime.get("network"):
            cmd += ["--network", runtime["network"]]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        for mount in runtime.get("mounts", []):
            cmd += ["-v", f"{(project.root / mount['host']).as_posix()}:{mount['container']}:{mount.get('mode', 'ro')}"]
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
                            elapsed, timeout_s, len(quarantined[:MAX_DESELECT]))


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


def interpret_pytest(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, rc: int,
                     report_path: Path | None, out_log: Path, elapsed: float, timeout_s: int,
                     quarantined_count: int) -> Execution:
    tail = _summary(_tail(out_log))
    if rc in (124, 137) and elapsed >= timeout_s - 5:
        return Execution("timeout", error_summary=f"exceeded {timeout_s}s; {tail}"[:500], rc=rc,
                         quarantined_deselected=quarantined_count)
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
    if tier.get("floors_file") and paths is None:
        breach = check_floors(project.root / tier["floors_file"], cases)
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
        return Execution("error", cases, f"executed {executed} tests, tier requires at least {min_executed}",
                         quarantined_count, rc)
    if totals["errors"] and totals["failed"] == 0 and rc != 1:
        status = "error"
    else:
        status = "failed" if (totals["failed"] or totals["errors"] or rc == 1) else "passed"
    summary = None
    if status == "error":
        summary = f"collection/setup errors in {totals['errors']} items: {tail}"[:500]
    return Execution(status, cases, summary, quarantined_count, rc)


def run_vitest(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, art: Path,
               legion: LegionClient, target: str) -> Execution:
    runtime = project.profile["runtime"]
    vt = project.profile.get("vitest", {})
    container = vt.get("container", runtime["container"])
    timeout_s = int(tier["timeout_s"])
    rt = {**runtime, "container": container, "ensure_up": vt.get("ensure_up")}
    problem = ensure_container(rt, project.root)
    if problem:
        return Execution("error", error_summary=problem)
    report_in = f"/tmp/{uuid.uuid4().hex}.json"
    cmd = ["docker", "exec", "-w", vt.get("workdir", "/app"), container, "timeout", "-s", "TERM", "-k", "20",
           str(timeout_s), "npx", "vitest", "run", "--reporter=json", f"--outputFile={report_in}"]
    cmd += list(tier.get("args", [])) + list(paths or tier.get("paths", []))
    out_log = art / "output.log"
    started = time.time()
    rc, _ = _run(cmd, timeout_s + 120, out_log)
    report_host = art / "report.json"
    cp_rc = pull_file(container, report_in, report_host)
    _run(["docker", "exec", container, "rm", "-f", report_in], 30)
    tail = _summary(_tail(out_log))
    if rc in (124, 137) and time.time() - started >= timeout_s - 5:
        return Execution("timeout", error_summary=f"exceeded {timeout_s}s; {tail}"[:500], rc=rc)
    if cp_rc != 0 or not report_host.exists():
        return Execution("error", error_summary=f"vitest exit {rc}, no report: {tail}"[:500], rc=rc)
    report = json.loads(report_host.read_text(encoding="utf-8"))
    cases = parsers.parse_vitest_json(report, project.root, vt.get("container_root", "/app"))
    totals = parsers.totals_of(cases)
    status = "failed" if (totals["failed"] or totals["errors"] or rc == 1) else "passed"
    return Execution(status, cases, None, 0, rc)


def run_schemathesis(project: Project, tier: dict[str, Any], trigger: str, art: Path) -> Execution:
    runtime = project.profile["runtime"]
    timeout_s = int(tier["timeout_s"])
    problem = ensure_container(runtime, project.root)
    if problem:
        return Execution("error", error_summary=problem)
    report_in = f"{runtime.get('tmp_dir', '/tmp/testplatform')}/{uuid.uuid4().hex}.xml"
    seed = str(tier.get("seed", 20260929))
    max_examples = str(tier.get("max_examples", 5))
    methods = tier.get("methods", ["GET"])
    args = ["schemathesis", "run", tier["schema_url"], "--url", tier.get("base_url", tier["schema_url"].rsplit("/", 1)[0]),
            "--seed", seed, "--max-examples", str(max_examples), "--checks", tier.get("checks", "not_a_server_error"),
            "--report", "junit", "--report-junit-path", report_in, "--no-color"]
    for m in methods:
        args += ["--include-method", m]
    args += list(tier.get("args", []))
    cmd = ["docker", "exec", "-w", runtime.get("workdir", "/app"), runtime["container"], "sh", "-c",
           f'mkdir -p {runtime.get("tmp_dir", "/tmp/testplatform")}; exec "$@"', "sh",
           "timeout", "-s", "TERM", "-k", "20", str(timeout_s)] + args
    out_log = art / "output.log"
    started = time.time()
    rc, _ = _run(cmd, timeout_s + 120, out_log)
    report_host = art / "report.xml"
    cp_rc = pull_file(runtime['container'], report_in, report_host)
    _run(["docker", "exec", runtime["container"], "rm", "-f", report_in], 30)
    tail = _summary(_tail(out_log))
    if rc in (124, 137) and time.time() - started >= timeout_s - 5:
        return Execution("timeout", error_summary=f"exceeded {timeout_s}s; {tail}"[:500], rc=rc)
    if cp_rc != 0 or not report_host.exists():
        return Execution("error", error_summary=f"schemathesis exit {rc}, no junit: {tail}"[:500], rc=rc)
    cases = parsers.parse_junit_xml(report_host.read_text(encoding="utf-8"), prefix="fuzz")
    totals = parsers.totals_of(cases)
    if not cases:
        return Execution("error", error_summary=f"schemathesis produced no test cases: {tail}"[:500], rc=rc)
    status = "failed" if (totals["failed"] or totals["errors"]) else "passed"
    return Execution(status, cases, None, 0, rc)


def run_playwright(project: Project, tier: dict[str, Any], art: Path) -> Execution:
    runner = project.root / tier.get("runner", ".claude/skills/playwright-testing/runner.py")
    base = tier["base_url"].rstrip("/")
    timeout_s = int(tier["timeout_s"])
    per_page = max(30, timeout_s // max(1, len(tier["pages"])))
    results: list[dict[str, Any]] = []
    log = art / "output.log"
    deadline = time.time() + timeout_s
    for page in tier["pages"]:
        if time.time() > deadline:
            break
        url = f"{base}{page}"
        started = time.time()
        proc_rc, text = _run([sys.executable, str(runner), "smoke", url], per_page)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"== {url} rc={proc_rc}\n{text}\n")
        parsed: dict[str, Any] = {}
        for line in reversed(text.strip().splitlines()):
            try:
                parsed = json.loads(line)
                break
            except ValueError:
                continue
        results.append({"url": url, "returncode": proc_rc, "output": parsed, "stderr": text[-300:],
                        "duration_ms": (time.time() - started) * 1000})
    cases = parsers.parse_playwright_smoke(results)
    (art / "report.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    if not cases:
        return Execution("error", error_summary="no e2e pages ran")
    totals = parsers.totals_of(cases)
    return Execution("failed" if totals["failed"] else "passed", cases, None, 0, 0)


def run_commands(project: Project, tier: dict[str, Any], art: Path) -> Execution:
    """Static gates: each configured command is one case; exit 0 passes, 1 fails, 2 or a timeout errors."""
    import hashlib
    specs = tier.get("commands") or []
    if not specs:
        return Execution("error", error_summary="commands tier has no commands")
    deadline = time.time() + int(tier["timeout_s"])
    cases: list[dict[str, Any]] = []
    log = art / "output.log"
    for spec in specs:
        name = spec["name"]
        argv = [sys.executable if a == "python" and i == 0 else a for i, a in enumerate(spec["run"])]
        left = deadline - time.time()
        node_id = f"{tier.get('case_prefix', 'gates')}::{name}"
        started = time.time()
        if left <= 0:
            rc, text = 124, "tier deadline reached before this gate ran"
        else:
            rc, text = _run(argv, min(float(spec.get("timeout_s", 900)), left), cwd=project.root)
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


def execute_framework(project: Project, tier: dict[str, Any], paths: list[str] | None, trigger: str, art: Path,
                      legion: LegionClient, target: str, changed_paths: list[str] | None = None) -> Execution:
    framework = tier.get("framework", project.profile.get("framework"))
    if framework == "pytest":
        return run_pytest(project, tier, paths, trigger, art, legion, target, changed_paths)
    if framework == "vitest":
        return run_vitest(project, tier, paths, trigger, art, legion, target)
    if framework == "schemathesis":
        return run_schemathesis(project, tier, trigger, art)
    if framework == "playwright":
        return run_playwright(project, tier, art)
    if framework == "commands":
        return run_commands(project, tier, art)
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


def start_and_run(project: Project, target: str | None, trigger: str, legion: LegionClient,
                  lock: ProjectLock, on_started: Callable[[int | None, Path], None] | None = None,
                  artifacts_root: Path = ARTIFACTS_ROOT, changed_paths: list[str] | None = None) -> Outcome:
    tier_name, tier, paths = resolve_target(project, target)
    target_label = target or project.default_target
    try:
        drain_pending(legion, artifacts_root, DRAIN_MAX_ITEMS, DRAIN_BUDGET_S)
    except OSError:
        pass
    framework = tier.get("framework", project.profile.get("framework"))
    art = new_artifact_dir(project.name, artifacts_root)
    sha = git_sha(project.root)
    started_at = time.time()
    run_id: int | None = None
    try:
        run_id = legion.create_run({
            "project_id": project.legion_project_id, "target": target_label, "tier": tier_name, "trigger": trigger,
            "git_sha": sha, "framework": framework, "runner_host": socket.gethostname(),
            "artifact_path": art.as_posix()})
        lock.set_run_id(run_id)
    except LegionError as exc:
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
    try:
        exe = execute_framework(project, tier, paths, trigger, art, legion, target_label, changed_paths)
    except Exception as exc:
        exe = Execution("error", error_summary=f"runner exception: {type(exc).__name__}: {exc}"[:500])
    duration = time.time() - started_at
    totals = parsers.totals_of(exe.cases)
    sparse_target = target_label == "changed" and trigger != "schedule"
    known: dict[str, str] = {}
    if sparse_target:
        try:
            known = legion.hashes(project.legion_project_id)
        except LegionError:
            known = {}
    rows = parsers.select_rows(exe.cases, known, sparse_target)
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
    else:
        text = local_verdict(exe.status, totals, exe.cases, exe.error_summary, "Legion unreachable, saved for replay")
        (art / "pending_ingest.json").write_text(json.dumps({"project": project.name, "run_id": None, "payload": payload,
                                                              "create_run": {
                                                                  "project_id": project.legion_project_id,
                                                                  "target": target_label, "tier": tier_name,
                                                                  "trigger": trigger, "git_sha": sha,
                                                                  "framework": framework,
                                                                  "runner_host": socket.gethostname(),
                                                                  "artifact_path": art.as_posix()}}), encoding="utf-8")
    (art / "verdict.txt").write_text(text, encoding="utf-8")
    return Outcome(run_id, exe.status, text, art)


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
            claim.rename(pending)
            done.append(f"failed {pending.parent.name}: {str(exc)[:80]}")
            break
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
