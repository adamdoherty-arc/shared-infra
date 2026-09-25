"""Probe every cloud provider key in bifrost/config.json directly upstream.

Sends a 1-token chat completion per key (up to two models, stops at the
first 200) and reports the HTTP status. Never prints key values. Used to
decide which providers have no working key (2026-09-25 operator order:
such providers are parked and stay off until the operator re-enables them;
see bifrost/operator-disabled.json).

    python scripts/probe_provider_keys.py            # all active cloud providers
    python scripts/probe_provider_keys.py groq aion  # a subset
    python scripts/probe_provider_keys.py --json
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH_DEAD = {401, 402, 403}


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def probe_one(base: str, key: str, model: str, timeout: float = 25.0) -> tuple[int | None, str]:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "shared-infra-keyprobe/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:160].decode("utf-8", "replace").replace("\n", " ")
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:120]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    env = load_env()
    cfg = json.loads((ROOT / "bifrost" / "config.json").read_text(encoding="utf-8"))
    report = []
    for name, p in cfg["providers"].items():
        if name.endswith("-local") or (args and name not in args):
            continue
        base = p.get("network_config", {}).get("base_url", "")
        base = base.replace("shared-freellmapi:3001", "127.0.0.1:3015")
        for key in p["keys"]:
            ref = str(key.get("value", ""))
            val = env.get(ref[4:], "") if ref.startswith("env.") else ref
            aliases = key.get("aliases") or {}
            models = [m for m in key.get("models", []) if m not in aliases][:2]
            tries = []
            if val:
                for m in models:
                    code, err = probe_one(base, val, m)
                    tries.append({"model": m, "status": code, "error": err})
                    if code == 200:
                        break
            ok = any(t["status"] == 200 for t in tries)
            auth_dead = bool(tries) and all(t["status"] in AUTH_DEAD for t in tries)
            row = {
                "provider": name,
                "key": key.get("name"),
                "env_set": bool(val),
                "ok": ok,
                "auth_dead": auth_dead or not val,
                "tries": tries,
            }
            report.append(row)
            if not as_json:
                print(
                    f"{name:12} {row['key']:26} env_set={row['env_set']} ok={ok} auth_dead={row['auth_dead']}",
                    flush=True,
                )
                for t in tries:
                    print(f"      {t['status']} {t['model']} {t['error']}", flush=True)
    if as_json:
        print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
