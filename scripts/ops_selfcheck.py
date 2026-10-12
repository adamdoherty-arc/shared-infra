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
  9. GPU budget        VRAM used <= 97%; 12 direct embeddings against vllm-embed with
                       p95 < 1s (the embed SLO); failed requests are failures
 10. DR scratch        no labelled (ada.dr-drill=1) scratch container past its deadline label and no
                       labelled dangling volume; read from Docker, never from the drill's state file
 11. task twins        no enabled Windows scheduled task runs a script an enabled hostcron job runs

This gate would pass trivially if the checks read the same state the failing job
writes (a job that dies before writing leaves the previous "ok" file): every
freshness check therefore compares against wall-clock age, never a stored status,
and an empty/unreadable source is a FAILURE, not a pass.

Usage: python scripts/ops_selfcheck.py [--no-post] [--json]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
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
DR_CONTRACT_DEFAULT = r"C:\code\ADA\scripts\dr_drill_contract.py"
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
            if d.get("container_leaked"):
                scratch = d.get("container", "?")
                fails.append(f"DR drill {target}: scratch container {scratch} LEAKED, its restored database is still "
                             f"running; remove with: docker rm -f -v {scratch}")
            if d.get("status") != "ok":
                why = str(d.get("failure", ""))[:120]
                fails.append(f"DR drill {target}: last run status={d.get('status')} ({why})")
            elif age_d > 10:
                fails.append(f"DR drill {target}: last ok run is {age_d} days old (>10)")
        except Exception as exc:  # noqa: BLE001
            fails.append(f"DR drill {target}: no readable result ({type(exc).__name__})")


def load_dr_contract():
    """Load the DR drill contract by path; the drill and this check share one definition of a scratch object."""
    path = os.environ.get("ADA_DR_CONTRACT", DR_CONTRACT_DEFAULT)
    spec = importlib.util.spec_from_file_location("dr_drill_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_rows(stdout: str, what: str, fails: list[str]) -> list[dict]:
    rows = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            fails.append(f"DR scratch probe: unreadable {what} row {line[:100]!r}")
    return rows


def check_dr_scratch_leaks(fails: list[str], m: dict) -> None:
    """A DR-drill scratch object is recognised by its ada.dr-drill labels and its deadline label, never by name
    or by the drill's state file: a labelled container past its deadline + leak_grace_s (or with no deadline
    label and older than a day), and a labelled volume dangling in two reads volume_recheck_s apart, are leaks.

    Would pass trivially if it read dr-drill/latest.json, if a failing `docker ps` or `docker volume ls` read
    as an empty list, if a volume seen once counted as leaked, or if the classifier ignored the deadline label."""
    try:
        contract = load_dr_contract()
    except Exception as exc:  # noqa: BLE001
        fails.append(f"DR scratch probe: contract unreadable ({type(exc).__name__}: {exc})")
        return
    ps = sh(contract.CONTAINER_QUERY)
    if ps.returncode != 0:
        fails.append("DR scratch probe: docker ps -a failed: " + (ps.stderr or "").strip()[-120:])
        return
    first = sh(contract.VOLUME_QUERY)
    if first.returncode != 0:
        fails.append("DR scratch probe: docker volume ls failed: " + (first.stderr or "").strip()[-120:])
        return
    time.sleep(contract.CONTRACT["volume_recheck_s"])
    second = sh(contract.VOLUME_QUERY)
    if second.returncode != 0:
        fails.append("DR scratch probe: docker volume ls failed: " + (second.stderr or "").strip()[-120:])
        return
    problems: list[str] = []
    containers = _json_rows(ps.stdout, "docker ps", problems)
    volumes_first = _json_rows(first.stdout, "docker volume ls", problems)
    volumes_second = _json_rows(second.stdout, "docker volume ls", problems)
    fails.extend(problems)
    found = contract.classify_scratch_objects(containers, volumes_first, volumes_second, time.time())
    leaked_containers = [c for c in found["expired_containers"] if c["removable"]]
    leaked_volumes = found["leaked_volumes"]
    for c in leaked_containers:
        fails.append(f"DR drill scratch container {c['name']} leaked: {c['reason']} (state {c['state'] or '?'}, "
                     f"target {c['target']}), its restored database is still there; "
                     f"remove with: docker rm -f -v {c['name']}")
    for v in leaked_volumes:
        fails.append(f"DR drill scratch volume {v['name']} leaked: dangling and labelled (run {v['run']}); "
                     f"remove with: docker volume rm {v['name']}")
    m["ops_dr_scratch_containers_leaked"] = len(leaked_containers)
    m["ops_dr_scratch_volumes_leaked"] = len(leaked_volumes)


INDEPENDENT_TWINS = frozenset({"ADA Master - Watchdog"})


def _script_tokens(cmd: object) -> set[str]:
    tokens = set()
    for part in cmd if isinstance(cmd, list) else [cmd]:
        text = str(part).replace("/", "\\").lower()
        if text.endswith((".py", ".cmd", ".bat", ".ps1")) and "\\" in text:
            tokens.add(text)
    return tokens


def check_task_twins(fails: list[str], m: dict) -> None:
    """An enabled Windows scheduled task whose action runs the same script as an enabled hostcron job runs the
    job twice. Matched by script-path token (case and slash insensitive).

    INDEPENDENT_TWINS are tasks kept enabled on purpose as a watchdog independent of hostcron (shared-infra 7672344).

    Would pass trivially if a failing task listing read as no tasks, or if only the job name were compared."""
    schedule = json.loads((HERE / "hostcron" / "schedule.json").read_text(encoding="utf8"))
    wanted: dict[str, str] = {}
    for job in schedule.get("jobs", []):
        if job.get("enabled", True) is False:
            continue
        for token in _script_tokens(job.get("cmd")):
            wanted[token] = job["name"]
    script = ("Get-ScheduledTask | Where-Object { $_.State -ne 'Disabled' } | ForEach-Object { "
              "[pscustomobject]@{Name=$_.TaskName; Text=(($_.Actions | ForEach-Object { $_.Execute + ' ' + "
              "$_.Arguments }) -join ' ')} } | ConvertTo-Json -Compress")
    p = sh(["powershell", "-NoProfile", "-Command", script])
    if p.returncode != 0:
        fails.append("task twin probe: Get-ScheduledTask failed: " + (p.stderr or "").strip()[-120:])
        return
    try:
        raw = json.loads(p.stdout or "[]")
    except ValueError:
        fails.append("task twin probe: unreadable Get-ScheduledTask output")
        return
    tasks = [raw] if isinstance(raw, dict) else raw
    twins = 0
    for task in tasks:
        if task.get("Name") in INDEPENDENT_TWINS:
            continue
        text = str(task.get("Text", "")).replace("/", "\\").lower()
        for token, job in wanted.items():
            if token in text:
                twins += 1
                fails.append(f"Windows task {task.get('Name')!r} is enabled and runs {token}, which hostcron job "
                             f"{job} already runs; unregister the task")
                break
    m["ops_hostcron_task_twins"] = twins


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
    try:
        live = {j["name"] for j in json.loads((HERE / "hostcron" / "schedule.json").read_text(encoding="utf8"))["jobs"]
                if j.get("enabled", True)}
    except (OSError, ValueError, KeyError) as exc:
        fails.append(f"hostcron schedule unreadable: {exc}")
        live = set(per_job)
    for job, sts in per_job.items():
        if job not in live:
            continue
        streak = 0
        for s in reversed(sts):
            if s == "ok":
                break
            streak += 1
        m[f'ops_hostcron_consecutive_failures{{hostcron_job="{job}"}}'] = streak
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


def check_default_pg_password(fails: list[str], m: dict) -> None:
    """INF-05: neither Postgres (host superuser, Legion's container) may accept the historical
    default password. Passes only on an authentication failure; a connection that cannot be
    attempted at all (container down) is a failure too, never a pass."""
    targets = [
        ("ops_pg_default_password_accepted", "Postgres superuser", "ada-db-backup",
         "host.docker.internal", "postgres", "postgres"),
        ("ops_legion_pg_default_password_accepted", "Legion Postgres role", "legion-db-backup",
         "legion-postgres", "legion", "legion"),
    ]
    for metric, label, via, host, user, db in targets:
        p = sh(["docker", "exec", via, "sh", "-c",
                f"PGPASSWORD=postgres123 PGCONNECT_TIMEOUT=8 psql -h {host} -U {user} "
                f"-d {db} -tAc 'select 1' 2>&1"], 30)
        out = (p.stdout + p.stderr).strip()
        accepted = p.returncode == 0 and out.startswith("1")
        refused = "password authentication failed" in out or "no pg_hba.conf entry" in out
        m[metric] = 1 if accepted else 0
        if accepted:
            fails.append(f"{label} still accepts the default password (INF-05)")
        elif not refused:
            fails.append(f"default-password probe for {label} inconclusive: {out[:120]!r}")


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


HOST_MEM_MIN_AVAILABLE_GIB = 4.0
HOST_COMMIT_MAX_RATIO = 0.90
HOST_MEM_TOP_LOG = HERE.parent / "state" / "host-mem-top.jsonl"
HOST_MEM_TOP_N = 8


def host_memory_top_consumers(n: int = HOST_MEM_TOP_N) -> list[dict]:
    """The n processes with the largest private (committed) bytes, with command lines, so a
    low-memory incident names its culprit while the process still exists. 2026-10-07 23:05 ET a
    host python.exe held 46.9 GB of commit (Windows event 2004) and was gone after the reboot it
    forced, unattributable."""
    import psutil

    rows = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            private = proc.memory_info().private
        except (psutil.Error, AttributeError):
            continue
        rows.append((private, proc))
    rows.sort(key=lambda r: r[0], reverse=True)
    out = []
    for private, proc in rows[:n]:
        try:
            cmd = " ".join(proc.cmdline())[:300]
        except psutil.Error:
            cmd = ""
        out.append({"pid": proc.info["pid"], "name": proc.info["name"],
                    "private_gib": round(private / 2**30, 2), "cmd": cmd})
    return out


def _record_host_memory_top(avail_gib: float, commit_ratio: float) -> list[dict]:
    top = host_memory_top_consumers()
    HOST_MEM_TOP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HOST_MEM_TOP_LOG.open("a", encoding="utf8") as fh:
        fh.write(json.dumps({"ts": datetime.now(UTC).isoformat(), "available_gib": round(avail_gib, 2),
                             "commit_ratio": round(commit_ratio, 4), "top": top}) + "\n")
    return top


def check_host_memory(fails: list[str], m: dict) -> None:
    """Host RAM: Windows available physical memory (free + standby). ~200 MB available was measured
    2026-09-30 while ADA latency degraded. Would pass trivially if the ctypes call failed to
    populate (it raises instead, which the caller records as a crashed check)."""
    import ctypes

    class MEMSTATUS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMSTATUS()
    st.dwLength = ctypes.sizeof(MEMSTATUS)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)) or not st.ullTotalPhys:
        raise RuntimeError("GlobalMemoryStatusEx returned no data")
    m["ops_host_mem_available_bytes"] = st.ullAvailPhys
    m["ops_host_mem_total_bytes"] = st.ullTotalPhys
    m["ops_host_mem_used_ratio"] = round(1 - st.ullAvailPhys / st.ullTotalPhys, 4)
    commit_ratio = 1 - st.ullAvailPageFile / st.ullTotalPageFile if st.ullTotalPageFile else 0.0
    m["ops_host_commit_limit_bytes"] = st.ullTotalPageFile
    m["ops_host_commit_used_ratio"] = round(commit_ratio, 4)
    gib = st.ullAvailPhys / 2**30
    low_phys = gib < HOST_MEM_MIN_AVAILABLE_GIB
    high_commit = commit_ratio > HOST_COMMIT_MAX_RATIO
    if not (low_phys or high_commit):
        return
    top = _record_host_memory_top(gib, commit_ratio)
    culprits = ", ".join(f"{t['name']}({t['pid']}) {t['private_gib']}G" for t in top[:3])
    if low_phys:
        fails.append(f"host memory available {gib:.2f} GiB (<{HOST_MEM_MIN_AVAILABLE_GIB:.0f} GiB); top: {culprits}")
    if high_commit:
        fails.append(f"host commit charge {commit_ratio * 100:.1f}% of limit (>{HOST_COMMIT_MAX_RATIO * 100:.0f}%); "
                     f"top: {culprits}")


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


DOCKER_BOOT_SHIMS = {
    r"C:\tmp\start-docker-if-down.ps1": r"C:\code\shared-infra\scripts\docker-boot-start.ps1",
    r"C:\tmp\reclaim-docker-gui.ps1": r"C:\code\shared-infra\scripts\docker-gui-reclaim.ps1",
}


def docker_boot_shim_findings(read=lambda p: Path(p).read_text(encoding="utf8", errors="replace")) -> list[str]:
    """The ZeroInfra-DockerAtBoot / -DockerGuiReclaim tasks run C:\\tmp shims (repointing them needs elevation).
    A missing shim, or one that no longer forwards to the versioned script, silently brings back the 2026-10-09
    double engine restart that left ada-bitcoin-prod down 8 minutes. This gate would pass trivially if it only
    checked that the shim file exists: it requires the versioned target path inside it and the target on disk."""
    out = []
    for shim, target in DOCKER_BOOT_SHIMS.items():
        try:
            text = read(shim)
        except OSError:
            out.append(f"Docker boot-task shim missing: {shim} (must forward to {target})")
            continue
        if target.lower() not in text.lower():
            out.append(f"Docker boot-task shim {shim} does not forward to {target}")
        try:
            read(target)
        except OSError:
            out.append(f"Docker boot-task target missing: {target}")
    return out


def check_docker_boot_tasks(fails: list[str], m: dict) -> None:
    found = docker_boot_shim_findings()
    m["ops_docker_boot_shim_findings"] = len(found)
    fails.extend(found)


def check_gpu(fails: list[str], m: dict) -> None:
    p = sh(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
    if p.returncode == 0 and p.stdout.strip().isdigit():
        free = int(p.stdout.strip())
        m["ops_gpu_free_mib"] = free
        if free < 400:
            fails.append(f"GPU free VRAM {free} MiB (<400)")


EMBED_URL = "http://127.0.0.1:8001/v1/embeddings"
EMBED_P95_SLO_S = 1.0


def check_gpu_budget(fails: list[str], m: dict) -> None:
    """Would pass trivially if the embed probe swallowed request errors as fast responses; a failed
    request here is counted as a failure, never as a latency sample."""
    p = sh(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"])
    if p.returncode != 0:
        raise RuntimeError("nvidia-smi failed: " + p.stderr[-120:])
    used, total, util = (int(x) for x in p.stdout.strip().split(","))
    m["ops_gpu_vram_used_ratio"] = round(used / total, 4)
    m["ops_gpu_util_percent"] = util
    if used / total > 0.97:
        fails.append(f"GPU VRAM {used}/{total} MiB ({used / total * 100:.1f}% > 97%)")
    body = json.dumps({"model": "Qwen/Qwen3-Embedding-0.6B",
                       "input": "ops self-check embedding latency probe " * 4}).encode()
    lat: list[float] = []
    errors = 0
    for _ in range(12):
        t0 = time.time()
        try:
            req = urllib.request.Request(EMBED_URL, data=body, headers={"Content-Type": "application/json"})
            vec = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"][0]["embedding"]  # noqa: S310
            if len(vec) != 1024:
                raise ValueError(f"embedding dim {len(vec)} != 1024")
            lat.append(time.time() - t0)
        except Exception:  # noqa: BLE001
            errors += 1
    if errors:
        fails.append(f"vllm-embed probe: {errors}/12 requests failed")
        return
    lat.sort()
    p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) > 1 else lat[0]
    m["ops_embed_probe_p95_seconds"] = round(p95, 3)
    if p95 > EMBED_P95_SLO_S:
        fails.append(f"embedding probe p95 {p95:.2f}s > {EMBED_P95_SLO_S:.0f}s SLO")


def push(m: dict, ok: bool, job: str = "ops_selfcheck") -> None:
    now = int(time.time())
    lines = [f"ops_selfcheck_last_run_timestamp_seconds {now}", f"ops_selfcheck_ok {1 if ok else 0}"]
    lines += [f"{k} {v}" for k, v in m.items()]
    try:
        req = urllib.request.Request(f"{PUSHGATEWAY}/metrics/job/{job}",
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
    ap.add_argument("--host-memory-only", action="store_true", help="run only the 15-minute host RAM probe")
    args = ap.parse_args()
    fails: list[str] = []
    metrics: dict = {}
    checks = ((check_host_memory,) if args.host_memory_only else
              (check_backups, check_offvolume, check_drills, check_dr_scratch_leaks, check_task_twins,
               check_hostcron, check_exposure,
               check_default_pg_password, check_capacity, check_host_memory, check_containers,
               check_docker_boot_tasks, check_gpu, check_gpu_budget))
    for fn in checks:
        try:
            if fn in (check_offvolume,):
                fn(fails)
            else:
                fn(fails, metrics)
        except Exception as exc:  # noqa: BLE001 - a crashed check is itself a failure
            fails.append(f"{fn.__name__} crashed: {type(exc).__name__}: {exc}")
    ok = not fails
    if not args.no_post:
        push(metrics, ok, "ops_hostmem" if args.host_memory_only else "ops_selfcheck")
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
