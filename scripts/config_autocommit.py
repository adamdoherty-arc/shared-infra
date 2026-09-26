#!/usr/bin/env python3
"""Commit live Bifrost routing state to git, with infractl's ledger as the message.

infractl (container shared-infra-control) is the single writer of
bifrost/config.json and bifrost/disabled-providers.json, and it refreshes
bifrost/config.snapshot.redacted.json after every verified change. The
container has no .git, so this host-side script (hostcron, every 15 min) is
what makes git reflect live routing:

  1. If none of those three files differs from HEAD: exit 0.
  2. If any of them has STAGED changes in the index, a human is mid-commit:
     skip (exit 0) and leave it to them.
  3. If any changed file was modified in the last SETTLE_S seconds (an action
     or an operator edit still in flight), or is not valid JSON: skip.
  4. Build the message from infractl's ledger rows (GET /api/audit) since the
     last commit that touched these files: `config: <summaries>` plus one body
     line per action (kind, requester, status, action id). A change with no
     ledger row is committed too, and says so: it was written outside
     infractl (an INFRA_CONFIG_WRITE_OK=1 edit), which is exactly what a
     reviewer needs to see.
  5. Re-render docs/PROVIDERS.md (generated from config.json; the gate fails
     when it is stale, so every infractl write would otherwise block its own
     commit) and include it when it changed.
  6. `git commit -m <msg> -- <changed paths>`: only those paths, whatever else
     is staged or dirty; the repo's pre-commit gate runs normally (never
     --no-verify). A gate failure is reported and the files stay uncommitted
     until the next run.

Runs as LocalSystem under hostcron: every git call carries
`-c safe.directory=<repo>`, a fallback committer identity is supplied when
none is configured, and the directory of the running Python is prepended to
PATH so the pre-commit hook's `python` resolves.

Usage: python scripts/config_autocommit.py [--dry-run] [--settle-s N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PATHS = (
    "bifrost/config.json",
    "bifrost/disabled-providers.json",
    "bifrost/config.snapshot.redacted.json",
)
CONFIG_KINDS = {
    "bifrost_models_apply", "bifrost_provider_park", "provider_park", "provider_unpark",
    "models_add", "models_remove", "alias_set",
}
LANDED_STATUSES = {"succeeded", "rolled_back", "rolled_back_manual"}
TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
FALLBACK_NAME = "shared-infra config-autocommit"
FALLBACK_EMAIL = "config-autocommit@shared-infra.local"
PROVIDERS_DOC = "docs/PROVIDERS.md"
PROVIDERS_RENDERER = "scripts/render_providers_doc.py"
INFRACTL_URL = os.environ.get("INFRACTL_URL", "http://127.0.0.1:8095").rstrip("/")


def log(msg: str) -> None:
    print(f"[config_autocommit] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def git(*args: str, check: bool = True, timeout: int = 60, env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", f"safe.directory={REPO.as_posix()}", "-C", str(REPO), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {proc.returncode}: {proc.stderr.strip()[-500:]}")
    return proc


def read_env_value(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k.strip() == key:
                return v.strip().strip('"').strip("'")
    return ""


def changed_paths() -> list[str]:
    out = git("status", "--porcelain=v1", "--", *PATHS).stdout
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def staged_paths() -> list[str]:
    return [p for p in git("diff", "--cached", "--name-only", "--", *PATHS).stdout.splitlines() if p.strip()]


def unsettled_or_invalid(paths: list[str], settle_s: int) -> str | None:
    now = time.time()
    for rel in paths:
        path = REPO / rel
        if not path.exists():
            continue
        age = now - path.stat().st_mtime
        if age < settle_s:
            return f"{rel} modified {age:.0f}s ago (< {settle_s}s settle window)"
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return f"{rel} is not valid JSON ({exc}); refusing to commit a half-written file"
    return None


def last_commit_ts() -> float:
    out = git("log", "-1", "--format=%ct", "--", *PATHS[:2]).stdout.strip()
    return float(out) if out else 0.0


def ledger_rows(since: float) -> tuple[list[dict], str | None]:
    token = read_env_value("INFRACTL_TOKEN")
    if not token:
        return [], "INFRACTL_TOKEN not found in env or shared-infra/.env"
    req = urllib.request.Request(f"{INFRACTL_URL}/api/audit?since={since}",
                                 headers={"X-Infractl-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read()).get("data") or []
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return [], f"infractl ledger unreachable ({exc})"
    landed = [r for r in rows if r.get("kind") in CONFIG_KINDS and r.get("status") in LANDED_STATUSES
              and not r.get("dry_run")]
    return sorted(landed, key=lambda r: r.get("created_at") or 0), None


def _row_summary(row: dict) -> str:
    try:
        summary = json.loads(row.get("verify_json") or "{}").get("summary")
    except ValueError:
        summary = None
    if not summary:
        payload = json.loads(row.get("payload_json") or "{}")
        summary = f"{row['kind']} {payload.get('provider') or payload.get('changes') or ''}".strip()
    if row["status"] != "succeeded":
        summary = f"{summary} [{row['status']}]"
    return summary


def build_message(rows: list[dict], ledger_error: str | None, since: float) -> str:
    if rows:
        landed = [_row_summary(r) for r in rows if r["status"] == "succeeded"] or [_row_summary(rows[-1])]
        subject = "config: " + "; ".join(landed)
        body = ["infractl ledger rows since the last config commit:"]
        body += [f"- {r['kind']} by {r['requested_by']} ({r['status']}, action {r['id']}): {r.get('reason', '')}"[:300]
                 for r in rows]
    else:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(since)) if since else "the first commit"
        why = ledger_error or f"no infractl ledger row since {stamp}"
        subject = "config: live routing change written outside infractl"
        body = [f"{why}. The change did not come through infractl's validated ladder",
                "(an INFRA_CONFIG_WRITE_OK=1 edit, or a writer that bypassed it); review the diff."]
    if len(subject) > 120:
        subject = subject[:117] + "..."
    return subject + "\n\n" + "\n".join(body) + "\n\n" + TRAILER + "\n"


def render_providers_doc() -> list[str]:
    """Regenerate docs/PROVIDERS.md from the config; return it if it now differs from HEAD."""
    renderer = REPO / PROVIDERS_RENDERER
    if not renderer.exists():
        return []
    proc = subprocess.run([sys.executable, str(renderer)], cwd=str(REPO), capture_output=True,
                          text=True, timeout=60, check=False)
    if proc.returncode != 0:
        log(f"render_providers_doc failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
        return []
    return [PROVIDERS_DOC] if git("status", "--porcelain=v1", "--", PROVIDERS_DOC).stdout.strip() else []


def commit(paths: list[str], message: str) -> int:
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    identity: list[str] = []
    if not git("config", "user.name", check=False).stdout.strip():
        identity = ["-c", f"user.name={FALLBACK_NAME}", "-c", f"user.email={FALLBACK_EMAIL}"]
    proc = git(*identity, "commit", "-m", message, "--", *paths, check=False, timeout=560, env=env)
    log(proc.stdout.strip()[-3000:])
    if proc.returncode != 0:
        log(f"commit FAILED (exit {proc.returncode}); files stay uncommitted until the next run:\n"
            f"{proc.stderr.strip()[-3000:]}")
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print the commit that would be made; commit nothing")
    ap.add_argument("--settle-s", type=int, default=120)
    args = ap.parse_args()

    paths = changed_paths()
    if not paths:
        log("config files match HEAD; nothing to commit")
        return 0
    staged = staged_paths()
    if staged:
        log(f"skip: {staged} already staged in the index (a human is mid-commit)")
        return 0
    problem = unsettled_or_invalid(paths, args.settle_s)
    if problem:
        log(f"skip: {problem}")
        return 0

    since = last_commit_ts()
    rows, ledger_error = ledger_rows(since)
    message = build_message(rows, ledger_error, since)
    if args.dry_run:
        log(f"DRY-RUN: would commit {paths} with message:\n{message}")
        return 0
    paths += [p for p in render_providers_doc() if p not in paths]
    log(f"committing {paths}")
    return commit(paths, message)


if __name__ == "__main__":
    sys.exit(main())
