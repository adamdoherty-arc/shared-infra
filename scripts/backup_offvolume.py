#!/usr/bin/env python3
"""Copy the newest ADA + Legion database dumps OUT of the Docker VHDX (INF-03).

The backup volumes (ada-backups, legion-backups) live inside docker_data.vhdx on
C:. A VHDX corruption or a Docker reset therefore loses the data and every backup
together. This job lands a verified second copy on the host NTFS filesystem, outside
the VHDX (default C:\\ProgramData\\ops-backups), keeps a few generations, records a
sha256 manifest, and pushes ops_offvolume_backup_* metrics so a silent stop alerts.

Limits, stated plainly: this host has ONE physical disk (Corsair NVMe), so this
protects against VHDX/Docker loss and volume corruption, NOT against loss of the
disk itself. A true offsite copy needs a target only the owner can provide (an
external drive or a cloud bucket + credentials); point BACKUP_OFFSITE_DIR at it and
this same script mirrors there too.

This gate would pass trivially if the sha256 comparison hashed the copy against
itself: the source hash is computed INSIDE the backup container from the volume and
the destination hash on the host from the copied file, two independent reads.

Usage: python scripts/backup_offvolume.py [--dest DIR] [--keep-ada 2] [--keep-legion 3]
Exit code non-zero on any failure (hostcron then records `failed` and pages).
"""
from __future__ import annotations

import argparse
import hashlib
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

DEFAULT_DEST = Path(os.environ.get("BACKUP_OFFVOLUME_DIR", r"C:\ProgramData\ops-backups"))
OFFSITE = os.environ.get("BACKUP_OFFSITE_DIR", "")
PUSHGATEWAY = "http://127.0.0.1:9091"

SOURCES = {
    "ada": {"container": "ada-db-backup", "rx": r"^ada_\d{8}_\d{6}\.dump$", "keep": 2},
    "legion": {"container": "legion-db-backup", "rx": r"^legion_\d{8}_\d{6}\.sql\.gz$", "keep": 3},
}


def sh(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def newest(container: str, rx: str) -> tuple[str, int]:
    p = sh(["docker", "exec", container, "sh", "-c", "cd /backups && stat -c '%n %s' *"], 60)
    best: tuple[str, int] | None = None
    for line in p.stdout.splitlines():
        name, _, size = line.rpartition(" ")
        if re.match(rx, name) and size.isdigit() and int(size) > 1_000_000:
            if best is None or name > best[0]:
                best = (name, int(size))
    if best is None:
        raise RuntimeError(f"no dump found in {container}")
    return best


def host_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def container_sha256(container: str, name: str) -> str:
    p = sh(["docker", "exec", container, "sha256sum", f"/backups/{name}"], 1800)
    if p.returncode != 0:
        raise RuntimeError(f"sha256sum failed in {container}: {p.stderr[-300:]}")
    return p.stdout.split()[0]


def prune(folder: Path, rx: str, keep: int) -> list[str]:
    files = sorted(f for f in folder.iterdir() if re.match(rx, f.name))
    gone = []
    for f in files[:-keep] if keep > 0 else []:
        f.unlink()
        gone.append(f.name)
    return gone


def copy_one(store: str, dest_root: Path, extra: Path | None) -> dict:
    cfg = SOURCES[store]
    name, size = newest(cfg["container"], cfg["rx"])
    folder = dest_root / store
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    result = {"store": store, "file": name, "bytes": size}
    if target.exists() and target.stat().st_size == size:
        result["skipped"] = "already copied"
    else:
        need = size * 1.1
        free = shutil.disk_usage(str(dest_root)).free
        if free < need + 20 * 1024**3:
            raise RuntimeError(
                f"refusing copy: {free / 1e9:.0f} GB free on destination, need {need / 1e9:.0f} GB + 20 GB floor")
        tmp = folder / (name + ".part")
        t0 = time.time()
        p = sh(["docker", "cp", f"{cfg['container']}:/backups/{name}", str(tmp)], 3600)
        if p.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"docker cp failed: {p.stderr[-300:]}")
        if tmp.stat().st_size != size:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"size mismatch after copy: {tmp.stat().st_size} != {size}")
        src_hash = container_sha256(cfg["container"], name)
        dst_hash = host_sha256(tmp)
        if src_hash != dst_hash:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"sha256 mismatch for {name}: source {src_hash} copy {dst_hash}")
        tmp.replace(target)
        result.update(sha256=dst_hash, copy_seconds=round(time.time() - t0, 1))
    result["pruned"] = prune(folder, cfg["rx"], cfg["keep"])
    if extra is not None:
        extra_folder = extra / store
        extra_folder.mkdir(parents=True, exist_ok=True)
        if not (extra_folder / name).exists():
            shutil.copy2(target, extra_folder / name)
        result["offsite_copy"] = str(extra_folder / name)
        prune(extra_folder, cfg["rx"], cfg["keep"])
    return result


def push(ok: bool, total_bytes: int) -> None:
    lines = [f"ops_offvolume_backup_last_run_timestamp_seconds {int(time.time())}",
             f"ops_offvolume_backup_bytes {total_bytes}"]
    if ok:
        lines.append(f"ops_offvolume_backup_last_success_timestamp_seconds {int(time.time())}")
    try:
        req = urllib.request.Request(f"{PUSHGATEWAY}/metrics/job/offvolume_backup",
                                     data=("\n".join(lines) + "\n").encode(), method="POST")
        urllib.request.urlopen(req, timeout=10).read()  # noqa: S310 - fixed loopback URL
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: pushgateway push failed: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", default=str(DEFAULT_DEST))
    ap.add_argument("--keep-ada", type=int, default=SOURCES["ada"]["keep"])
    ap.add_argument("--keep-legion", type=int, default=SOURCES["legion"]["keep"])
    args = ap.parse_args()
    SOURCES["ada"]["keep"], SOURCES["legion"]["keep"] = args.keep_ada, args.keep_legion
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    extra = Path(OFFSITE) if OFFSITE else None
    manifest: dict = {"ts": datetime.now(UTC).isoformat(), "dest": str(dest), "results": []}
    failed = False
    for store in SOURCES:
        try:
            manifest["results"].append(copy_one(store, dest, extra))
        except Exception as exc:  # noqa: BLE001 - recorded, then the run exits non-zero
            failed = True
            manifest["results"].append({"store": store, "error": f"{type(exc).__name__}: {exc}"})
            print(f"FAILED {store}: {exc}", file=sys.stderr)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    total = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file() and f.suffix != ".json")
    push(not failed, total)
    print(json.dumps(manifest, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
