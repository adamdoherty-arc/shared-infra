from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

from . import runner, server
from .artifacts import prune
from .legion import LegionClient, LegionError
from .lock import LIGHT, ProjectLock, queue_snapshot
from .printer import budget, emit
from .profile import ARTIFACTS_ROOT, PLATFORM_ROOT, ProfileError, legion_url, load_project, parse_target_spec

QUEUE_TIMEOUT_S = 5400


def _client() -> LegionClient:
    return LegionClient(legion_url())


def _rows(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _fmt_ts(value: Any) -> str:
    return str(value or "")[:16].replace("T", " ")


def _finish_run(outcome: runner.Outcome, detail: bool, quiet: bool = False) -> int:
    if quiet and outcome.status == "passed":
        return 0
    lines = outcome.text.splitlines() or [f"run {outcome.run_id}: {outcome.status}"]
    if outcome.attached:
        lines.insert(0, f"attached to in-flight run {outcome.run_id}")
    if outcome.artifact_dir is not None and len(lines) < budget(detail):
        lines.append(f"artifacts: {outcome.artifact_dir.as_posix()}")
    emit(lines, detail)
    return outcome.exit_code


def cmd_run(args: argparse.Namespace) -> int:
    name, target = parse_target_spec(args.spec)
    project = load_project(name)
    runner.preflight(project, target, args.paths)
    trigger = args.trigger
    client = _client()
    lock = runner.lock_for(project, target, args.paths)
    if not args.wait:
        return _run_detached(args, name, lock)
    key = runner.request_key(target or project.default_target, args.paths)
    ok, held = lock.acquire(key)
    if not ok and held is not None and held.key != key:
        def announce(position: int, holder: Any) -> None:
            behind = f" behind run {holder.run_id}" if holder is not None and holder.run_id else ""
            emit(f"queued #{position} in the {lock.lane} lane{behind}", stream=sys.stderr)

        if not lock.wait_acquire(key, QUEUE_TIMEOUT_S, on_wait=announce):
            emit([f"error: {name} test lock still held by another run after {QUEUE_TIMEOUT_S}s"])
            return 2
        ok = True
    if not ok:
        return _finish_run(runner.attach_and_wait(client, lock), args.detail, args.quiet)
    try:
        outcome = runner.start_and_run(project, target, trigger, client, lock, changed_paths=args.paths)
    finally:
        lock.release()
    return _finish_run(outcome, args.detail, args.quiet)


def _run_detached(args: argparse.Namespace, name: str, lock: ProjectLock) -> int:
    log_dir = PLATFORM_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / f"detached-{int(time.time())}.log", "ab")
    flags = 0x00000008 | 0x08000000 | 0x00000200 if sys.platform == "win32" else 0
    extra = ["--paths", *args.paths] if args.paths else []
    subprocess.Popen([sys.executable, str(PLATFORM_ROOT / "testctl.py"), "run", args.spec, "--wait",
                      "--trigger", args.trigger, *extra], stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    project = load_project(name)
    _, target = parse_target_spec(args.spec)
    mine = runner.request_key(target or project.default_target, args.paths)
    held = lock.wait_for_run_id(20.0, mine) if lock.lane == LIGHT else lock.wait_for_run_id(20.0)
    if held is not None and held.run_id is not None:
        if held.key != mine:
            deadline = time.time() + 5.0
            position = None
            while position is None and time.time() < deadline:
                mine_row = next((r for r in queue_snapshot() if r["project"] == name and r["lane"] == lock.lane
                                 and r["key"] == mine), None)
                position = mine_row["position"] if mine_row else None
                if position is None:
                    time.sleep(0.2)
            emit([f"queued #{position or '?'} {args.spec} behind run {held.run_id} in the {lock.lane} lane "
                  f"(first come, first served); poll with: testctl status"])
            return 0
        emit([f"started run {held.run_id} for {args.spec}; poll with: testctl status {held.run_id}"])
        return 0
    emit([f"started {args.spec}; run id not registered yet; poll with: testctl status"])
    return 0


def cmd_failure(args: argparse.Namespace) -> int:
    data = _client().failure(args.failure_id)
    head = [f"failure {args.failure_id}: {data.get('node_id')}",
            f"{data.get('type')}: {str(data.get('message', ''))[:180]}",
            f"first seen run {data.get('first_seen_run')}, occurrences {data.get('occurrences')}, "
            f"issue {data.get('issue_ref') or 'none'}"]
    tail = str(data.get("trace_tail") or "").splitlines()
    room = budget(args.detail) - len(head) - 1
    shown = tail[-room:] if room > 0 else []
    emit(head + (["--- trace tail ---"] if shown else []) + shown, args.detail)
    return 0


def cmd_run_failures(args: argparse.Namespace) -> int:
    rows = _rows(_client().run_failures(args.run_id), "failures", "items")
    if not rows:
        emit([f"run {args.run_id}: no failures recorded"])
        return 0
    limit = budget(args.detail) - 2
    lines = [f"run {args.run_id}: {len(rows)} failures"]
    for row in rows[:limit]:
        lines.append(f"{row.get('failure_id')}  {row.get('one_line') or row.get('node_id')}")
    if len(rows) > limit:
        lines.append(f"+{len(rows) - limit} more (--detail)")
    emit(lines, args.detail)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    data = _client().history(project.legion_project_id, args.node_id)
    results = _rows(data, "results", "history", "cases")
    revisions = _rows(data, "revisions") if isinstance(data, dict) else []
    glyph = {"passed": ".", "failed": "F", "error": "E", "skipped": "s", "rerun_passed": "r", "xfail": "x", "xpass": "X"}
    strip = "".join(glyph.get(str(r.get("status")), "?") for r in reversed(results[:20]))
    lines = [f"{args.node_id}", f"last {len(results)} (oldest->newest): {strip}"]
    limit = budget(args.detail) - len(lines) - (1 if revisions else 0)
    for row in results[:limit]:
        lines.append(f"{_fmt_ts(row.get('created_at') or row.get('completed_at') or row.get('ts'))} "
                     f"run {row.get('run_id')} {row.get('status')} {row.get('duration_ms', '')}ms "
                     f"{str(row.get('git_sha', ''))[:8]}".rstrip())
    if revisions:
        lines.append(f"revisions: {len(revisions)} (latest {str(revisions[0].get('git_sha', ''))[:8]})")
    emit(lines, args.detail)
    return 0


def cmd_flaky(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    rows = _rows(_client().flaky(project.legion_project_id), "flaky", "items")
    if not rows:
        emit([f"{args.project}: no flaky tests"])
        return 0
    rows = sorted(rows, key=lambda r: -float(r.get("flake_score") or 0))
    limit = budget(args.detail) - 2
    lines = [f"{args.project}: {len(rows)} flaky tests (score = flaky events / runs, 30d)"]
    for r in rows[:limit]:
        q = " QUARANTINED" if r.get("quarantined") else ""
        lines.append(f"{float(r.get('flake_score') or 0):.2f} {r.get('flaky_events_30d')}/{r.get('runs_30d')} "
                     f"{r.get('node_id')}{q}")
    if len(rows) > limit:
        lines.append(f"+{len(rows) - limit} more (--detail)")
    emit(lines, args.detail)
    return 0


def _local_recent(limit: int = 3) -> list[str]:
    files = sorted(ARTIFACTS_ROOT.glob("*/*/*/verdict.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    lines = []
    for f in files[:limit]:
        first = (f.read_text(encoding="utf-8", errors="replace").splitlines() or [""])[0]
        lines.append(f"{f.parents[2].name} {f.parents[1].name}: {first}")
    return lines


def cmd_status(args: argparse.Namespace) -> int:
    if args.run_id is not None:
        run = _client().run(args.run_id)
        text = run.get("text") or (run.get("verdict") or {}).get("text") or run.get("verdict_text")
        if text:
            emit(str(text).splitlines(), args.detail)
        else:
            emit([f"run {args.run_id}: {run.get('status')} target {run.get('target')} "
                  f"totals {run.get('totals')}"], args.detail)
        return 0
    active = server.active_runs()
    lines = [f"active: {a['project']} {a['lane']} run {a['run_id']} (pid {a['pid']})" for a in active] or ["active: none"]
    for q in queue_snapshot():
        key = json.loads(q["key"]) if q.get("key") else ["?", []]
        lines.append(f"queued #{q['position']}: {q['project']} {q['lane']} {key[0] or 'default'}"
                     f"{' ' + ','.join(key[1]) if key[1] else ''}")
    lines.append(f"runner service: {'up' if server.is_up() else 'DOWN'} on :{server.DEFAULT_PORT}")
    lines += _local_recent()
    emit(lines, args.detail)
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    rows = _rows(_client().schedules(), "schedules", "items")
    if not rows:
        emit(["no schedules"])
        return 0
    limit = budget(args.detail) - 2
    lines = [f"{len(rows)} schedules"]
    for r in rows[:limit]:
        on = "on " if r.get("enabled") else "off"
        lines.append(f"#{r.get('id')} {on} p{r.get('project_id')} {r.get('target')} [{r.get('tier')}] "
                     f"{r.get('cron')} {r.get('timezone', '')} last={_fmt_ts(r.get('last_fired_at'))} "
                     f"run={r.get('last_run_id')}")
    if len(rows) > limit:
        lines.append(f"+{len(rows) - limit} more (--detail)")
    emit(lines, args.detail)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    if args.restart:
        emit([f"testctl serve: exit requested={server.request_exit(args.port)}; hostcron restarts it within a minute"])
        return 0
    if args.ensure:
        emit([f"testctl serve: {server.ensure_running(args.port)}"])
        return 0
    server.serve(args.host, args.port)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    emit([f"pruned {prune()} day-directories older than 14 days"])
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    done = runner.replay_pending(_client())
    emit(done or ["nothing pending"], args.detail)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="testctl", description="Run tests, get a verdict of at most 15 lines.")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("--detail", action="store_true", help="allow up to 60 lines")
        sp.set_defaults(fn=fn)
        return sp

    r = add("run", cmd_run, help="run <project>[:<target>]")
    r.add_argument("spec")
    grp = r.add_mutually_exclusive_group()
    grp.add_argument("--wait", dest="wait", action="store_true", default=True)
    grp.add_argument("--no-wait", dest="wait", action="store_false")
    r.add_argument("--trigger", default=os.environ.get("TESTCTL_TRIGGER", "claude"),
                   choices=list(runner.TRIGGERS))
    r.add_argument("--paths", nargs="+", metavar="FILE", default=None,
                   help="changed tier only: run the tests that depend on these source/test files")
    r.add_argument("--quiet", action="store_true", help="print nothing on pass; the exit code gates")
    f = add("failure", cmd_failure)
    f.add_argument("failure_id", type=int)
    rf = add("run-failures", cmd_run_failures)
    rf.add_argument("run_id", type=int)
    h = add("history", cmd_history)
    h.add_argument("project")
    h.add_argument("node_id")
    fl = add("flaky", cmd_flaky)
    fl.add_argument("project")
    st = add("status", cmd_status)
    st.add_argument("run_id", type=int, nargs="?")
    sc = add("schedule", cmd_schedule)
    sc.add_argument("action", choices=["list"])
    sv = add("serve", cmd_serve)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    sv.add_argument("--restart", action="store_true", help="ask the running service to exit; the supervisor restarts it")
    sv.add_argument("--ensure", action="store_true", help="start the service detached if it is not answering")
    add("prune", cmd_prune)
    add("replay", cmd_replay, help="ingest results saved while Legion was unreachable")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ProfileError, runner.RunError) as exc:
        emit([f"error: {exc}"])
        return 2
    except LegionError as exc:
        emit([f"error: {exc}"])
        return 2
    except KeyboardInterrupt:
        emit(["interrupted"])
        return 2
