#!/usr/bin/env python3
"""scripts/gate.py — shared-infra's quality gate (WS7, 2026-09-25).

Runs the checks that keep this repo from silently drifting the way it did
before WS7 (config that couldn't be validated, no test floor, no lint
signal, no stub detector). Modeled on ADA's `pre_commit_bug_zero.sh` +
`scripts/audits/*.py` ratchet pattern, scaled to what this repo actually
has: compose files instead of a frontend/backend split, and a much smaller
Python surface (infractl + bifrost + bifrost-metrics-exporter + scripts).

Stages:
  (a) `docker compose -f <file> config -q` for every root docker-compose*.yml
      (skips *.bak* variants).
  (b) `python -m pytest -m "not live"` over bifrost/tests, bifrost-metrics-exporter/tests,
      .claude/hooks/tests, scripts/tests, and the two wedge-monitor test files on the host, plus
      `bash scripts/run_infractl_tests.sh` for infractl's suite (it imports
      fcntl, Linux-only, so it runs inside the disposable container that
      script already sets up -- see that script's header).
  (f) every published host port binds 127.0.0.1 (all profiles); Grafana excepted
      but must not use a default admin password.
  (e) docs/PROVIDERS.md is regenerated from bifrost/config.json and must match.
  (c) `ruff check .` against the ratchet baseline
      (`.audit-baselines/ruff_errors.json`) -- fails if any rule's count grew.
  (d) stub detector (`scripts/audits/stub_detector.py`) against
      `.audit-baselines/stubs.json` -- fails if any banned-phrasing count grew.

Writes a JSON result to `.claude/state/test-results/gate-<ts>.json` and
`.claude/state/test-results/latest.json`. Exit nonzero if any stage fails.

Usage:
    python scripts/gate.py             # all stages
    python scripts/gate.py --fast      # same as above; "fast" here means
                                        # "skip nothing that isn't `-m live`"
                                        # -- there is no slow stage to skip
                                        # yet in this repo, unlike ADA's
                                        # frontend/backend gate. Kept as a
                                        # flag so .githooks/pre-commit's
                                        # invocation matches ADA's convention
                                        # and doesn't need editing if a slow
                                        # stage is added later.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / ".claude" / "state" / "test-results"


def _bash_executable() -> str:
    """Resolve a bash that actually has Docker Desktop's PATH wired in.

    On Windows, plain `"bash"` resolution from a python.exe-spawned
    subprocess is ambiguous: PATH commonly lists Git Bash's
    `usr\\bin\\bash.exe`, WSL's `System32\\bash.exe` launcher, and a
    WindowsApps shim, and which one wins depends on invocation context in a
    way that does NOT match what an interactive Git Bash terminal resolves.
    Measured live (WS7, 2026-09-25): the ambiguous `"bash"` lookup landed on
    a WSL distro with no `docker` CLI installed in it, even though Docker
    Desktop's WSL integration and Git Bash both work fine interactively.
    Git Bash's top-level `bin\\bash.exe` (not `usr\\bin\\bash.exe`) is the
    one that carries Docker Desktop's PATH additions on this class of setup,
    so prefer it explicitly when present; fall back to plain `"bash"` on any
    other platform (Linux/macOS CI) where this ambiguity doesn't exist.
    """
    candidate = r"C:\Program Files\Git\bin\bash.exe"
    if os.name == "nt" and Path(candidate).exists():
        return candidate
    return "bash"


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out[-8000:]
    except subprocess.TimeoutExpired as exc:
        return 1, f"TIMEOUT after {timeout}s: {exc}"
    except FileNotFoundError as exc:
        return 1, f"command not found: {exc}"
    except Exception as exc:  # noqa: BLE001
        return 1, f"error running {cmd}: {exc}"


def stage_compose_config() -> dict:
    files = sorted(f for f in glob.glob(str(REPO_ROOT / "docker-compose*.yml")) if ".bak" not in Path(f).name)
    results = []
    ok = True
    for f in files:
        rc, out = _run(["docker", "compose", "-f", f, "config", "-q"], timeout=60)
        results.append({"file": Path(f).name, "rc": rc, "output": out if rc else ""})
        if rc != 0:
            ok = False
    return {"stage": "compose_config", "ok": ok, "files_checked": len(files), "results": results}


# Services allowed to publish on every interface. Grafana has its own login;
# everything else here is unauthenticated (Bifrost's /api/* returned every
# provider key and virtual key in plaintext on 0.0.0.0:4445 until 2026-09-25).
PUBLIC_PORT_ALLOWLIST = {"shared-grafana"}


def stage_loopback_ports() -> dict:
    """Every published host port must bind 127.0.0.1 unless allowlisted."""
    files = sorted(f for f in glob.glob(str(REPO_ROOT / "docker-compose*.yml")) if ".bak" not in Path(f).name)
    exposed = []
    errors = []
    for f in files:
        try:
            proc = subprocess.run(
                ["docker", "compose", "-f", f, "--profile", "*", "config", "--format", "json"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
                encoding="utf-8",
                errors="replace",
            )
            services = json.loads(proc.stdout).get("services", {})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{Path(f).name}: {exc}")
            continue
        for name, svc in services.items():
            if name in PUBLIC_PORT_ALLOWLIST:
                # LAN-reachable, so it must not run on a default credential
                # (Grafana answered admin/admin on 3050 until 2026-09-25).
                env = svc.get("environment") or {}
                pw = env.get("GF_SECURITY_ADMIN_PASSWORD") if isinstance(env, dict) else None
                if pw is not None and (not pw or pw.lower() in {"admin", "password", "changeme"}):
                    exposed.append(f"{Path(f).name}:{name}: default admin password on a LAN port")
                continue
            for port in svc.get("ports") or []:
                if port.get("host_ip") not in ("127.0.0.1", "::1"):
                    exposed.append(f"{Path(f).name}:{name}:{port.get('published')}")
    ok = not exposed and not errors
    return {"stage": "loopback_ports", "ok": ok, "exposed": exposed, "errors": errors}


def stage_tests() -> dict:
    rc1, out1 = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "bifrost/tests",
            "bifrost-metrics-exporter/tests",
            ".claude/hooks/tests",
            "scripts/tests",
            "infractl/tests/test_vllm_wedge_monitor_predicates.py",
            "infractl/tests/test_vllm_wedge_monitor.py",
            "-p",
            "no:cacheprovider",
            "-q",
            "-m",
            "not live",
        ],
        timeout=180,
    )

    runner = REPO_ROOT / "scripts" / "run_infractl_tests.sh"
    if runner.exists():
        # A RELATIVE path here, resolved via subprocess's `cwd=` (which the OS
        # applies before bash ever sees the argument). An absolute path is a
        # trap on this host: PATH can resolve "bash" to either Git Bash or
        # WSL's bash.exe depending on invocation context, and WSL bash cannot
        # open a raw `C:/...` path (no drive-letter concept in its filesystem
        # namespace) -- it needs `/mnt/c/...`. A relative path sidesteps the
        # ambiguity entirely because both bash flavors resolve it against the
        # OS-level cwd the same way.
        rc2, out2 = _run([_bash_executable(), "scripts/run_infractl_tests.sh"], timeout=300)
    else:
        rc2, out2 = (
            1,
            "scripts/run_infractl_tests.sh not found -- infractl/tests cannot run on this host (fcntl import)",
        )

    ok = rc1 == 0 and rc2 == 0
    return {
        "stage": "tests",
        "ok": ok,
        "bifrost_tests": {"rc": rc1, "output": out1},
        "infractl_tests": {"rc": rc2, "output": out2},
    }


def _ruff_cmd() -> list[str]:
    """`python -m ruff` finds its binary only in the CURRENT user's Scripts dir.
    Under hostcron (LocalSystem) that is the system profile, so the nightly gate
    failed with RuffNotFound (2026-09-25). Resolve the binary next to the
    installed package (<prefix>/site-packages/ruff -> <prefix>/Scripts/ruff.exe)."""
    try:
        import ruff  # noqa: PLC0415

        pkg = Path(ruff.__file__).resolve().parent
        for cand in (pkg.parents[1] / "Scripts" / "ruff.exe", pkg.parents[1] / "bin" / "ruff"):
            if cand.exists():
                return [str(cand)]
    except Exception:  # noqa: BLE001
        pass
    return [sys.executable, "-m", "ruff"]


def stage_ruff() -> dict:
    baseline_path = REPO_ROOT / ".audit-baselines" / "ruff_errors.json"
    # ruff exits 1 when it finds violations -- that's expected, not a gate
    # failure by itself; the ratchet comparison below decides pass/fail.
    # Parse the FULL stdout: _run() keeps only the last 8 KB, which truncated
    # the JSON, and the old `else []` then reported "0 violations" -- the
    # ratchet failed OPEN (found 2026-09-25: gate said 0, ruff said 155).
    raw = ""
    try:
        proc = subprocess.run(
            [*_ruff_cmd(), "check", "--output-format=json", "."],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
        raw = (proc.stdout or "") + (proc.stderr or "")
        violations = json.loads(proc.stdout)
        if not isinstance(violations, list):
            violations = None
    except Exception:
        violations = None

    if violations is None:
        return {"stage": "ruff", "ok": False, "error": "could not parse ruff JSON output", "raw": raw[-2000:]}

    counts: dict[str, int] = {}
    for v in violations:
        code = v.get("code") or "UNKNOWN"
        counts[code] = counts.get(code, 0) + 1

    baseline = {}
    if baseline_path.exists():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8")).get("counts", {})
        except Exception:
            baseline = {}

    regressions = []
    for code, count in counts.items():
        prior = baseline.get(code, 0)
        if count > prior:
            regressions.append(f"{code}: {prior} -> {count}")

    ok = len(regressions) == 0
    return {
        "stage": "ruff",
        "ok": ok,
        "total_violations": sum(counts.values()),
        "counts": counts,
        "regressions": regressions,
    }


def stage_providers_doc() -> dict:
    """docs/PROVIDERS.md must match bifrost/config.json (it is generated)."""
    rc, out = _run([sys.executable, "-X", "utf8", "scripts/render_providers_doc.py", "--check"], timeout=30)
    return {"stage": "providers_doc", "ok": rc == 0, "output": out.strip()[-500:]}


def stage_stub_detector() -> dict:
    rc, out = _run([sys.executable, "scripts/audits/stub_detector.py", "--json"], timeout=60)
    try:
        result = json.loads(out)
    except Exception:
        return {"stage": "stub_detector", "ok": False, "error": "could not parse output", "raw": out[-2000:]}
    return {
        "stage": "stub_detector",
        "ok": result.get("ok", False),
        "total_hits": result.get("total", 0),
        "messages": result.get("messages", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fast", action="store_true", help="see module docstring -- currently a no-op, kept for CLI compatibility"
    )
    parser.parse_args()

    started = time.time()
    stages = [
        stage_compose_config(),
        stage_loopback_ports(),
        stage_tests(),
        stage_ruff(),
        stage_stub_detector(),
        stage_providers_doc(),
    ]
    ok = all(s["ok"] for s in stages)
    elapsed = time.time() - started

    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": ok,
        "elapsed_s": round(elapsed, 1),
        "stages": stages,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts_name = time.strftime("gate-%Y%m%dT%H%M%SZ.json", time.gmtime())
    (STATE_DIR / ts_name).write_text(json.dumps(result, indent=2), encoding="utf-8")
    (STATE_DIR / "latest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"=== shared-infra gate: {'PASS' if ok else 'FAIL'} ({elapsed:.1f}s) ===")
    for s in stages:
        status = "ok" if s["ok"] else "FAIL"
        extra = ""
        if s["stage"] == "compose_config":
            extra = f"({s['files_checked']} files)"
        elif s["stage"] == "ruff":
            extra = f"({s.get('total_violations', '?')} violations, {len(s.get('regressions', []))} regressions)"
        elif s["stage"] == "stub_detector":
            extra = f"({s.get('total_hits', '?')} hits)"
        elif s["stage"] == "tests":
            extra = f"(bifrost rc={s['bifrost_tests']['rc']}, infractl rc={s['infractl_tests']['rc']})"
        print(f"  [{status}] {s['stage']} {extra}")
        if not s["ok"]:
            if s["stage"] == "compose_config":
                for r in s["results"]:
                    if r["rc"] != 0:
                        print(f"    {r['file']}: {r['output'][-500:]}")
            elif s["stage"] == "tests":
                if s["bifrost_tests"]["rc"] != 0:
                    print(
                        "    bifrost/tests:\n"
                        + "\n".join("    " + ln for ln in s["bifrost_tests"]["output"].splitlines()[-30:])
                    )
                if s["infractl_tests"]["rc"] != 0:
                    print(
                        "    infractl/tests:\n"
                        + "\n".join("    " + ln for ln in s["infractl_tests"]["output"].splitlines()[-30:])
                    )
            elif s["stage"] == "ruff":
                for r in s.get("regressions", []):
                    print(f"    REGRESSION {r}")
            elif s["stage"] == "loopback_ports":
                for e in s.get("exposed", []) + s.get("errors", []):
                    print(f"    NOT LOOPBACK {e}")
            elif s["stage"] == "stub_detector":
                for m in s.get("messages", []):
                    print(f"    {m}")

    print(f"result written to {STATE_DIR / 'latest.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
