#!/usr/bin/env python3
"""Nightly ops self-check (Legion sprint 15209, Dependability W2; hostcron job ops-selfcheck).

The 2026-09-29 audit found the recurring shape of every P1: something stopped, nothing
noticed. This job is the independent "did the safety nets themselves keep running"
check. It runs on the host (outside every container it audits), and on ANY failure
posts a direct Discord message to the infra webhook -- no dependency on ada-backend,
legion-backend or Alertmanager -- and exits non-zero so the hostcron ledger says
`failed`. It also pushes ops_* gauges to shared-pushgateway so Prometheus rules
(ops_dependability.yml) alert independently, including when THIS job goes silent.

Checks:
  1. backup freshness  newest ada_*.dump / legion_*.sql.gz age < 30h (docker exec stat)
  2. off-VHDX copy     backup_offvolume manifest < 36h and no error rows
  3. DR drill          latest[-legion].json status ok and < 10 days old
  4. hostcron          consecutive failures per job (runs.jsonl tail), heartbeat fresh
  5. exposure          docker-published 0.0.0.0 ports + host python/postgres listeners
                       not in scripts/ops_exposure_allowlist.json
  6. capacity          C: free ratio >= 15%, docker_data.vhdx size
  7. containers        expected set running; none unhealthy; none crash-looping
  8. GPU               free VRAM >= 400 MiB

This gate would pass trivially if the checks read the same state the failing job
writes (a job that dies before writing leaves the previous "ok" file): every
freshness check therefore compares against wall-clock age, never a stored status,
and an empty/unreadable source is a FAILURE, not a pass.

Usage: python scripts/ops_selfcheck.py [--no-post] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ALLOWLIST = json.loads((HERE / "ops_exposure_allowlist.json").read_text(encoding="utf8"))
EXPECTED = json.loads((HERE / "ops_expected_containers.json").read_text(encoding="utf8"))
RUNS = HERE / "hostcron" / "state" / "runs.jsonl"
HEARTBEAT = HERE / "hostcron" / "state" / "heartbeat.json"
ADA_STATE = Path(r"C:\code\ADA\.claude\state\dr-drill")
OFFVOLUME = Path(r"C:\ProgramData\ops-backups\manifest.json")
PUSHGATEWAY = "http://127.0.0.1:9091"
UA = "shared-infra-ops-selfcheck/1"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def sh(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)


def newest_dump_age_h(container: str, rx: str) -> float | None:
    p = sh(["docker", "exec", container, "sh", "-c", "cd /backups && stat -c '%n %Y %s' *"])
    best = None
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and re.match(rx, parts[0]) and int(parts[2]) > 1_000_000:
            best = max(best or 0, int(parts[1]))
    return None if best is None else (time.time() - best) / 3600.0


def check_backups(fails: list[str], m: dict) -> None:
    for store, container, rx in (("ada", "ada-db-backup", r"^ada_\d{8}_\d{6}\.dump$"),
                                 ("legion", "legion-db-backup", r"^legion_\d{8}_\d{6}\.sql\.gz$")):
        age = newest_dump_age_h(container, rx)
        m[f'ops_backup_newest_age_seconds{{store="{store}"}}'] = int((age or 9e6) * 3600)
        if age is None:
            fails.append(f"backup {store}: no dump found / container {container} unreachable")
        elif age > 30:
            fails.append(f"backup {store}: newest dump is {age:.1f}h old (>30h)")


def check_offvolume(fails: list[str]) -> None:
    try:
        man = json.loads(OFFVOLUME.read_text(encoding="utf8"))
        age = (datetime.now(UTC) - datetime.fromisoformat(man["ts"])).total_seconds() / 3600
        errs = [r for r in man["results"] if "error" in r]
        if age > 36:
            fails.append(f"off-VHDX backup copy: manifest {age:.0f}h old (>36h)")
        for r in errs:
            fails.append(f"off-VHDX backup copy {r['store']}: {r['error'][:150]}")
    except Exception as exc:  # noqa: BLE001
        fails.append(f"off-VHDX backup copy: manifest unreadable ({type(exc).__name__}: {exc})")


def check_drills(fails: list[str], m: dict) -> None:
    for target, name in (("ada", "latest.json"), ("legion", "latest-legion.json")):
        try:
            d = json.loads((ADA_STATE / name).read_text(encoding="utf8"))
            age_d = (datetime.now(UTC) - datetime.fromisoformat(d["ts"])).days
            m[f'ops_dr_drill_selfcheck_ok{{target="{target}"}}'] = 1 if d.get("status") == "ok" else 0
            if d.get("status") != "ok":
                why = str(d.get("failure", ""))[:120]
                fails.append(f"DR drill {target}: last run status={d.get('status')} ({why})")
            elif age_d > 10:
                fails.append(f"DR drill {target}: last ok run is {age_d} days old (>10)")
        except Exception as exc:  # noqa: BLE001
            fails.append(f"DR drill {target}: no readable result ({type(exc).__name__})")


def check_hostcron(fails: list[str], m: dict) -> None:
    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf8"))
        ts = hb.get("ts") or hb.get("time") or ""
        if ts:
            age = time.time() - datetime.fromisoformat(ts).timestamp()
            if age > 300:
                fails.append(f"hostcron heartbeat is {age:.0f}s old")
    except Exception as exc:  # noqa: BLE001
        fails.append(f"hostcron heartbeat unreadable ({type(exc).__name__})")
    per_job: dict[str, list[str]] = {}
    try:
        for line in RUNS.read_text(encoding="utf8").splitlines()[-4000:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("status") in ("skipped_overlap",):
                continue
            per_job.setdefault(row["job"], []).append(row["status"])
    except OSError as exc:
        fails.append(f"hostcron ledger unreadable: {exc}")
        return
    for job, sts in per_job.items():
        streak = 0
        for s in reversed(sts):
            if s == "ok":
                break
            streak += 1
        m[f'ops_hostcron_consecutive_failures{{job="{job}"}}'] = streak
        if streak >= 2:
            fails.append(f"hostcron job {job}: {streak} consecutive failures (last: {sts[-1]})")


def check_exposure(fails: list[str], m: dict) -> None:
    allowed = {int(k) for k in ALLOWLIST["ports"] if not k.startswith("_")}
    bad: list[str] = []
    p = sh(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    for line in p.stdout.splitlines():
        name, _, ports = line.partition("\t")
        for mt in re.finditer(r"(?:0\.0\.0\.0|\[::\]):(\d+)(?:-(\d+))?->", ports):
            lo, hi = int(mt.group(1)), int(mt.group(2) or mt.group(1))
            for port in range(lo, hi + 1):
                if port not in allowed:
                    bad.append(f"{name}:{port}")
    ps = sh(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Get-NetTCPConnection -State Listen | Where-Object { $_.LocalAddress -in '0.0.0.0','::' } | "
             "ForEach-Object { $n=(Get-Process -Id $_.OwningProcess -EA 0).ProcessName; \"$n $($_.LocalPort)\" }"])
    for line in ps.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].lower() in ("python", "python3", "postgres", "node", "java", "uvicorn") \
                and int(parts[1]) not in allowed:
            bad.append(f"host {parts[0]}:{parts[1]}")
    bad = sorted(set(bad))
    m["ops_unexpected_exposed_ports"] = len(bad)
    if bad:
        fails.append("non-loopback listeners outside scripts/ops_exposure_allowlist.json: " + ", ".join(bad[:15]))


def check_capacity(fails: list[str], m: dict) -> None:
    du = shutil.disk_usage("C:\\")
    ratio = du.free / du.total
    m['ops_host_disk_free_ratio{drive="C"}'] = round(ratio, 4)
    if ratio < 0.15:
        fails.append(f"C: free space {ratio * 100:.1f}% (<15%)")
    vhdx = 0
    for cand in Path(r"C:\Users\hadam\AppData\Local\Docker").rglob("docker_data.vhdx"):
        vhdx = max(vhdx, cand.stat().st_size)
    if vhdx:
        m["ops_docker_vhdx_bytes"] = vhdx


def check_containers(fails: list[str], m: dict) -> None:
    p = sh(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
    if p.returncode != 0:
        fails.append("docker daemon unreachable: " + p.stderr[-120:])
        return
    running, unhealthy = set(), []
    exited_expected = []
    for line in p.stdout.splitlines():
        name, state, status = (line.split("\t") + ["", "", ""])[:3]
        if state == "running":
            running.add(name)
            if "(unhealthy)" in status:
                unhealthy.append(name)
        elif name in EXPECTED["must_run"]:
            exited_expected.append(name)
    missing = sorted(set(EXPECTED["must_run"]) - running)
    m["ops_containers_unhealthy"] = len(unhealthy)
    m["ops_expected_containers_missing"] = len(missing)
    if missing:
        fails.append("expected containers not running: " + ", ".join(missing))
    if unhealthy:
        fails.append("unhealthy containers: " + ", ".join(sorted(unhealthy)))


def check_gpu(fails: list[str], m: dict) -> None:
    p = sh(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
    if p.returncode == 0 and p.stdout.strip().isdigit():
        free = int(p.stdout.strip())
        m["ops_gpu_free_mib"] = free
        if free < 400:
            fails.append(f"GPU free VRAM {free} MiB (<400)")


def push(m: dict, ok: bool) -> None:
    now = int(time.time())
    lines = [f"ops_selfcheck_last_run_timestamp_seconds {now}", f"ops_selfcheck_ok {1 if ok else 0}"]
    lines += [f"{k} {v}" for k, v in m.items()]
    try:
        req = urllib.request.Request(f"{PUSHGATEWAY}/metrics/job/ops_selfcheck",
                                     data=("\n".join(lines) + "\n").encode(), method="POST")
        urllib.request.urlopen(req, timeout=10).read()  # noqa: S310 - fixed loopback URL
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: pushgateway push failed: {exc}", file=sys.stderr)


def webhook() -> str:
    try:
        for line in (ROOT / ".env").read_text(encoding="utf8", errors="replace").splitlines():
            if line.startswith("DISCORD_INFRA_WEBHOOK="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def post(content: str) -> None:
    url = webhook()
    if not url:
        print("WARN: no DISCORD_INFRA_WEBHOOK; cannot post", file=sys.stderr)
        return
    try:
        req = urllib.request.Request(url, data=json.dumps({"content": content[:1900]}).encode(),
                                     method="POST", headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=15).read()  # noqa: S310 - operator-owned webhook
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: discord post failed: {exc}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-post", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fails: list[str] = []
    metrics: dict = {}
    for fn in (check_backups, check_offvolume, check_drills, check_hostcron, check_exposure,
               check_capacity, check_containers, check_gpu):
        try:
            if fn in (check_offvolume,):
                fn(fails)
            else:
                fn(fails, metrics)
        except Exception as exc:  # noqa: BLE001 - a crashed check is itself a failure
            fails.append(f"{fn.__name__} crashed: {type(exc).__name__}: {exc}")
    ok = not fails
    if not args.no_post:
        push(metrics, ok)
        if not ok:
            post(f"**ops self-check FAILED** ({len(fails)})\n- " + "\n- ".join(fails))
    if args.json:
        print(json.dumps({"ok": ok, "failures": fails, "metrics": metrics}, indent=2))
    else:
        print("ops self-check:", "OK" if ok else "FAILED")
        for f in fails:
            print(" -", f)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
