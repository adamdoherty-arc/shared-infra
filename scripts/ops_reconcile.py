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

Published-port liveness (2026-09-30 incident): after `wsl --shutdown` + Docker Desktop restart,
restart-policy containers came back running/healthy but their HOST publishes (ada-backend 8006,
ada-frontend 5420, legion-frontend 3005, shared-grafana 3050, erpnext-frontend-1 8080) reset every
connection although com.docker.backend.exe was LISTENING. For each running container in must_run plus
`port_probe_extra`, every TCP publish bound to 0.0.0.0/127.0.0.1 is probed (connect + minimal HTTP GET;
any reply counts alive; a silent-but-connected port counts alive, so non-HTTP services pass on
connect). A reset/refused twice, 5s apart, on a container up > 90s is a dead publish and is healed:
`ada-*` via ADA's request_restart.py --recreate, shared-bifrost alert-only (special restart sequence),
everything else `docker restart`. One heal per container per 30 min; pause switch honoured.

This gate would pass trivially if it only looked at container State.Running / health (all of those
containers were running and healthy while dead): plan_port_heals() therefore decides from PROBE
results, and its sabotage test feeds running+healthy containers with failed probes.

Usage: python scripts/ops_reconcile.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPECTED = json.loads((HERE / "ops_expected_containers.json").read_text(encoding="utf8"))
PAUSE = HERE / "ops_reconcile.pause"
STATE = HERE.parent / "state" / "ops-reconcile.json"
COOLDOWN_S = 1800
PORT_UPTIME_FLOOR_S = 90
PROBE_TIMEOUT_S = 3.5
PROBE_RETRY_GAP_S = 5
PASSIVE_WAIT_S = 1.0
ADA_RESTART = r"C:\code\ADA\scripts\request_restart.py"
ALERT_ONLY = {"shared-bifrost"}
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
UA = "shared-infra-ops-reconcile/1"


def sh(cmd: list[str], timeout: int = 60, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=cwd,
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


def probe_port(port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Alive = the host publish holds a connection open. A dead Docker publish accepts the TCP
    connection and immediately closes/resets it (seen 2026-09-30 on 5420/8006/3005/3050/8080/9187/4445),
    so: phase 1 passive wait (live servers stay silent; EOF/reset = dead); phase 2 a minimal HTTP GET
    (any reply = alive; a non-HTTP server closing after the garbage request is still alive because the
    connection survived phase 1). Refused/timeout on connect = dead."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
            c.settimeout(PASSIVE_WAIT_S)
            try:
                if c.recv(64) == b"":
                    return False
                return True
            except TimeoutError:
                pass
            except OSError:
                return False
            c.settimeout(timeout)
            try:
                c.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\nUser-Agent: " + UA.encode() + b"\r\n\r\n")
                c.recv(64)
            except OSError:
                pass
            return True
    except OSError:
        return False


def published_tcp_ports(inspect: dict) -> list[int]:
    """Host TCP ports published on 0.0.0.0 / 127.0.0.1 from a `docker inspect` object."""
    out: set[int] = set()
    for key, binds in ((inspect.get("NetworkSettings") or {}).get("Ports") or {}).items():
        if not key.endswith("/tcp") or not binds:
            continue
        for b in binds:
            if b.get("HostIp") in ("0.0.0.0", "127.0.0.1", "") and str(b.get("HostPort", "")).isdigit():
                out.add(int(b["HostPort"]))
    return sorted(out)


def uptime_s(inspect: dict, now: datetime | None = None) -> float:
    started = (inspect.get("State") or {}).get("StartedAt", "")
    try:
        t = datetime.fromisoformat(started.rstrip("Z").split(".")[0]).replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    return ((now or datetime.now(UTC)) - t).total_seconds()


def plan_port_heals(inspects: dict[str, dict], probe, last: dict, now_ts: float,
                    now: datetime | None = None, sleep=time.sleep) -> list[tuple[str, int]]:
    """Decide which containers have a dead host publish from PROBE results (inject `probe`/`sleep`
    in tests). Returns [(container, first_dead_port)]."""
    heal: list[tuple[str, int]] = []
    for name, ins in inspects.items():
        st = ins.get("State") or {}
        if not st.get("Running") or st.get("Restarting") or st.get("Paused"):
            continue
        if uptime_s(ins, now) <= PORT_UPTIME_FLOOR_S:
            continue
        if (st.get("Health") or {}).get("Status") == "starting":
            continue
        if now_ts - last.get(f"port:{name}", 0) < COOLDOWN_S:
            continue
        dead = [p for p in published_tcp_ports(ins) if not probe(p)]
        if not dead:
            continue
        sleep(PROBE_RETRY_GAP_S)
        dead = [p for p in dead if not probe(p)]
        if dead:
            heal.append((name, dead[0]))
    return heal


def port_heal_cmd(name: str, port: int) -> list[str] | None:
    if name in ALERT_ONLY:
        return None
    if name.startswith("ada-"):
        cmd = [sys.executable, ADA_RESTART, "--now", "--recreate", "--containers", name,
               "--reason", f"ops_reconcile: dead host port {port}"]
        if name == "ada-frontend":
            cmd.append("--frontend-config-changed")
        return cmd
    return ["docker", "restart", name]


def save_state(last: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(last))


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
    probe_names = [n for n in dict.fromkeys(list(must) + EXPECTED.get("port_probe_extra", []))
                   if state.get(n) == "running"]
    inspects: dict[str, dict] = {}
    if probe_names:
        ins = sh(["docker", "inspect", *probe_names], 60)
        if ins.returncode == 0:
            try:
                for o in json.loads(ins.stdout or "[]"):
                    inspects[o["Name"].lstrip("/")] = o
            except (ValueError, KeyError, TypeError):
                inspects = {}
    for name, port in plan_port_heals(inspects, probe_port, last, time.time()):
        cmd = port_heal_cmd(name, port)
        if cmd is None:
            failed.append(f"{name}: dead host port {port} (alert only; use the Bifrost restart sequence)")
            continue
        if paused or args.dry_run:
            print(f"WOULD heal {name} (dead host port {port}): {' '.join(cmd)}")
            continue
        last[f"port:{name}"] = time.time()
        save_state(last)
        try:
            p = sh(cmd, 900)
        except subprocess.TimeoutExpired:
            failed.append(f"{name}: dead port {port}, heal timed out")
            continue
        if p.returncode == 0:
            healed.append(f"{name} (dead host port {port})")
        else:
            tail = (p.stderr or p.stdout).strip()[-200:]
            failed.append(f"{name}: dead port {port}, heal rc={p.returncode} {tail}")
    if not (paused or args.dry_run):
        save_state(last)
    if healed:
        notify("**ops-reconcile** brought back: " + ", ".join(healed))
    if failed:
        notify("**ops-reconcile FAILED** to heal: " + "; ".join(failed))
    print("healed:", healed or "none", "| failed:", failed or "none", "| paused:", paused)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
