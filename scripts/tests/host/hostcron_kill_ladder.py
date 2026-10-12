"""Host test: hostcron's real _run_job against real Windows child processes at the kill ladder's three rungs.

Run: C:/Python314/python.exe scripts/tests/host/hostcron_kill_ladder.py   (exit 0 = all rungs behave)

Rungs, each with a control beside the sabotage:
  1. a grace job that handles CTRL_BREAK exits by itself inside grace_s: detail says graceful, no hard kill
  2. a grace job that ignores CTRL_BREAK is hard-killed when grace_s expires
  3. a job without grace_s is hard-killed at its timeout with the original "exceeded <N>s" detail
All state goes to a scratch dir (HOSTCRON_SCHEDULE/STATE_DIR/LOG_DIR); the live ledger is never written.

This gate would pass trivially if the child exited on its own before the timeout, if rung 1 accepted a hard kill as
graceful (it checks the child's own marker file), or if the scratch redirect were missing (the live runs.jsonl would
then grow, and the final check compares its size).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import types
from pathlib import Path

HOSTCRON_PY = Path(__file__).resolve().parents[2] / "hostcron" / "hostcron.py"
LIVE_RUNS = HOSTCRON_PY.parent / "state" / "runs.jsonl"
PYTHON = sys.executable

HANDLES_BREAK = textwrap.dedent("""
    import signal, sys, time, pathlib
    marker = pathlib.Path(sys.argv[1])
    def on_break(*_):
        marker.write_text("handled")
        sys.exit(0)
    signal.signal(signal.SIGBREAK, on_break)
    while True:
        time.sleep(0.2)
""")
IGNORES_BREAK = textwrap.dedent("""
    import signal, time
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    while True:
        time.sleep(0.2)
""")


def load_hostcron(scratch: Path) -> types.ModuleType:
    env = {
        "HOSTCRON_SCHEDULE": str(scratch / "schedule.json"),
        "HOSTCRON_STATE_DIR": str(scratch / "state"),
        "HOSTCRON_LOG_DIR": str(scratch / "logs"),
    }
    (scratch / "schedule.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        module = types.ModuleType("hostcron_ladder")
        module.__file__ = str(HOSTCRON_PY)
        exec(compile(HOSTCRON_PY.read_text(encoding="utf-8"), str(HOSTCRON_PY), "exec"), module.__dict__)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


def last_run(scratch: Path) -> dict:
    lines = (scratch / "state" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def main() -> int:
    live_before = LIVE_RUNS.stat().st_size if LIVE_RUNS.exists() else 0
    scratch = Path(tempfile.mkdtemp(prefix="hostcron-ladder-"))
    failures: list[str] = []
    try:
        hostcron = load_hostcron(scratch)
        marker = scratch / "handled.txt"
        handles = scratch / "handles.py"
        handles.write_text(HANDLES_BREAK, encoding="utf-8")
        ignores = scratch / "ignores.py"
        ignores.write_text(IGNORES_BREAK, encoding="utf-8")

        def run(name: str, script: Path, extra_args: list[str], grace_s: int | None) -> dict:
            job = {
                "name": name, "cmd": [PYTHON, str(script), *extra_args], "cwd": str(scratch),
                "timeout_s": 3, "notify_on_failure": False,
            }
            if grace_s is not None:
                job["grace_s"] = grace_s
            started = time.monotonic()
            hostcron._run_job(job)
            row = last_run(scratch)
            row["wall_s"] = round(time.monotonic() - started, 1)
            return row

        graceful = run("ladder-graceful", handles, [str(marker)], 20)
        if graceful.get("status") != "timeout" or "graceful stop" not in graceful.get("detail", ""):
            failures.append(f"rung 1 not graceful: {graceful}")
        if not marker.exists() or graceful["wall_s"] > 15:
            failures.append(f"rung 1: child did not handle the break itself (marker={marker.exists()}): {graceful}")

        stubborn = run("ladder-stubborn", ignores, [], 4)
        if stubborn.get("status") != "timeout" or "hard kill" not in stubborn.get("detail", ""):
            failures.append(f"rung 2 not hard-killed after grace: {stubborn}")
        if not 6 <= stubborn["wall_s"] <= 20:
            failures.append(f"rung 2 wall time outside timeout+grace window: {stubborn}")

        plain = run("ladder-plain", ignores, [], None)
        if plain.get("status") != "timeout" or plain.get("detail") != "exceeded 3s":
            failures.append(f"rung 3 detail changed for a job without grace_s: {plain}")
        if plain["wall_s"] > 10:
            failures.append(f"rung 3 slow hard kill: {plain}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    live_after = LIVE_RUNS.stat().st_size if LIVE_RUNS.exists() else 0
    if live_after < live_before:
        failures.append("live runs.jsonl shrank during the test")
    for failure in failures:
        print("FAIL", failure)
    print("hostcron_kill_ladder:", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
