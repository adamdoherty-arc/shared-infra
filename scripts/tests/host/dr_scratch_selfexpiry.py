"""Host test: a DR scratch container built from the contract removes itself and its labelled volume, even when its
parent is hard-killed, and ops_selfcheck's probe sees it while it lives and not afterwards.

Run: C:/Python314/python.exe scripts/tests/host/dr_scratch_selfexpiry.py   (exit 0 = pass)

It starts a throwaway pgvector (GNU timeout) container (never the drill, never a real backup)
with the contract's own labels, --rm,
the contract's labelled anonymous mount and an in-container `timeout -s INT` deadman of a few seconds, then
hard-kills (taskkill /F /T) the python parent that started it and waits for the daemon to remove the container.

This gate would pass trivially if the container were removed by the test (the parent here is killed first and the test
only polls), if labels were not applied (the label filter would list nothing in the live phase and the live-phase
assertion fails), or if only the container, not its volume, were checked.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time

CONTRACT_PATH = os.environ.get("ADA_DR_CONTRACT", r"C:\code\ADA\scripts\dr_drill_contract.py")
LIFETIME_S = 8
spec = importlib.util.spec_from_file_location("dr_drill_contract", CONTRACT_PATH)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)

PARENT = """
import subprocess, sys, time
cmd = sys.argv[1:]
subprocess.run(cmd, check=True)
time.sleep(600)
"""


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def labelled_rows(kind: str) -> list[dict]:
    query = contract.CONTAINER_QUERY if kind == "container" else contract.VOLUME_QUERY[:-4] + ["--format", "{{json .}}"]
    out = docker(*query[1:]).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def main() -> int:
    run_id = contract.new_run_id()
    labels = contract.scratch_labels("ada", run_id, int(time.time()) + LIFETIME_S)
    name = f"ada-dr-selftest-{run_id}"
    cmd = [
        "docker", "run", "-d", "--rm", "--name", name, *contract.docker_label_args(labels),
        "--mount", contract.pgdata_mount_arg(labels),
        "--entrypoint", "timeout", "pgvector/pgvector:pg17", "-s", "INT", "-k", "3", str(LIFETIME_S), "sleep", "600",
    ]
    parent = subprocess.Popen(
        [sys.executable, "-c", PARENT, *cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    failures: list[str] = []
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not any(r.get("Names") == name for r in labelled_rows("container")):
            time.sleep(0.5)
        live = [r for r in labelled_rows("container") if r.get("Names") == name]
        if not live:
            failures.append("labelled container never appeared in the label-filtered docker ps")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(parent.pid)], capture_output=True)
        gone_by = time.time() + LIFETIME_S + 40
        while time.time() < gone_by and any(r.get("Names") == name for r in labelled_rows("container")):
            time.sleep(1)
        if any(r.get("Names") == name for r in labelled_rows("container")):
            failures.append(f"container {name} survived its deadline plus 40 s after the parent was hard-killed")
        time.sleep(3)
        run_label = f"{contract.CONTRACT['label_run']}={run_id}"
        leftover = [r for r in labelled_rows("volume") if run_label in str(r.get("Labels"))]
        if leftover:
            failures.append(f"labelled volume survived: {leftover}")
            for row in leftover:
                docker("volume", "rm", row["Name"])
    finally:
        docker("rm", "-f", "-v", name)
    for failure in failures:
        print("FAIL", failure)
    print("dr_scratch_selfexpiry:", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
