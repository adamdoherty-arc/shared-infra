#!/usr/bin/env python3
"""Post-boot / continuous container reconciliation (INF-20, INF-01).

After a Docker Desktop or host restart the stacks come back unevenly: on 2026-09-29
legion-postgres, legion-backend, freellmapi, searxng and ada-redis had different start
times and legion-db-backup never came back at all, with nothing noticing. NSSM's
`docker compose up` services only start what they were asked to start, once.

Every 10 minutes (hostcron job ops-reconcile) this compares `docker ps` against
scripts/ops_expected_containers.json `must_run`. A container that is ABSENT or EXITED
is brought back with `docker compose up -d --no-deps <service>` using the compose files
recorded in that JSON (derived from live labels). A container that is running but
`restarting`/unhealthy is NOT touched: that is a crash loop for a human/agent to diagnose,
and the ops self-check + Prometheus already page on it.

Guard rails:
  * Pause switch: create scripts/ops_reconcile.pause (any content) to stop healing while
    you deliberately hold something down; the job still reports what it WOULD do.
  * Cooldown: a container is healed at most once per 30 minutes so a container that dies
    on start cannot be relaunched in a tight loop.
  * Only the must_run set is ever touched, and only ever `up -d --no-deps <one service>`:
    never down, never recreate of a running container, never volumes.
  * Docker not answering => exit 1 without doing anything.

This gate would pass trivially if `docker ps` returned an empty list (docker hung) and the
job read that as "everything is absent": it therefore refuses to act unless `docker info`
succeeds first and at least one expected container is seen running.

Usage: python scripts/ops_reconcile.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPECTED = json.loads((HERE / "ops_expected_containers.json").read_text(encoding="utf8"))
PAUSE = HERE / "ops_reconcile.pause"
STATE = HERE.parent / "state" / "ops-reconcile.json"
COOLDOWN_S = 1800
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
UA = "shared-infra-ops-reconcile/1"


def sh(cmd: list[str], timeout: int = 60, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                          creationflags=CREATE_NO_WINDOW)


def webhook() -> str:
    try:
        for line in (HERE.parent / ".env").read_text(encoding="utf8", errors="replace").splitlines():
            if line.startswith("DISCORD_INFRA_WEBHOOK="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def notify(msg: str) -> None:
    url = webhook()
    if not url:
        return
    try:
        req = urllib.request.Request(url, data=json.dumps({"content": msg[:1900]}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=15).read()  # noqa: S310 - operator-owned webhook
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: discord post failed: {exc}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if sh(["docker", "info", "--format", "{{.ServerVersion}}"], 30).returncode != 0:
        print("docker daemon not answering; nothing done", file=sys.stderr)
        return 1
    ps = sh(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"], 60)
    if ps.returncode != 0:
        print("docker ps failed; nothing done", file=sys.stderr)
        return 1
    state = {}
    for line in ps.stdout.splitlines():
        name, _, st = line.partition("\t")
        state[name] = st
    must = EXPECTED["must_run"]
    if not any(state.get(n) == "running" for n in must):
        print("no expected container is running: refusing to act on a possibly hung docker", file=sys.stderr)
        return 1

    try:
        last = json.loads(STATE.read_text(encoding="utf8"))
    except (OSError, ValueError):
        last = {}
    paused = PAUSE.exists()
    healed, failed = [], []
    for name in must:
        st = state.get(name)
        if st == "running" or st in ("restarting", "paused", "created"):
            continue
        if time.time() - last.get(name, 0) < COOLDOWN_S:
            continue
        spec = EXPECTED.get("compose", {}).get(name)
        if not spec:
            failed.append(f"{name}: no compose mapping")
            continue
        cmd = ["docker", "compose", "-p", spec["project"], "--project-directory", spec["workdir"]]
        for f in spec["files"]:
            cmd += ["-f", f]
        cmd += ["up", "-d", "--no-deps", spec["service"]]
        if paused or args.dry_run:
            print(f"WOULD heal {name} (state={st}): {' '.join(cmd)}")
            continue
        p = sh(cmd, 600, cwd=spec["workdir"])
        last[name] = time.time()
        if p.returncode == 0:
            healed.append(f"{name} (was {st or 'absent'})")
        else:
            failed.append(f"{name}: {p.stderr.strip()[-200:]}")
    if not (paused or args.dry_run):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(last))
    if healed:
        notify("**ops-reconcile** brought back: " + ", ".join(healed))
    if failed:
        notify("**ops-reconcile FAILED** to heal: " + "; ".join(failed))
    print("healed:", healed or "none", "| failed:", failed or "none", "| paused:", paused)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
