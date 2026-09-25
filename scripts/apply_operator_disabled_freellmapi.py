"""Apply bifrost/operator-disabled.json to the freellmapi aggregator.

freellmapi (shared-freellmapi, host 127.0.0.1:3015) keeps its own key vault
and fallback chain, so disabling a provider in Bifrost does not stop the
aggregator from routing to it. This script disables, inside freellmapi:
  - every key on a platform listed in `freellmapi_platforms`,
  - every fallback entry whose model matches a `model_patterns` entry,
    is on a disabled platform, is listed in `freellmapi_models`, or has
    no enabled key.
It never enables anything: re-enabling is the operator's call, made by
editing operator-disabled.json and toggling the entry in the freellmapi UI.
Idempotent; run by hostcron daily so a UI click or an upstream re-seed
cannot silently bring a disabled model back.

    python scripts/apply_operator_disabled_freellmapi.py [--dry-run]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:3015"


def _call(method: str, path: str, body: object | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def plan(od: dict, keys: list[dict], chain: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (keys_to_disable, fallback_entries_to_disable). Pure, unit-tested."""
    platforms = set(od.get("freellmapi_platforms", {}))
    patterns = [p.lower() for p in od.get("model_patterns", {})]
    dead_models = set(od.get("freellmapi_models", {}))
    kill_keys = [k for k in keys if k["platform"] in platforms and k.get("enabled")]
    live_platforms = {k["platform"] for k in keys if k.get("enabled") and k["platform"] not in platforms}
    kill_entries = []
    for e in chain:
        if not e.get("enabled"):
            continue
        mid = e["modelId"]
        if (
            e["platform"] in platforms
            or f"{e['platform']}/{mid}" in dead_models
            or any(p in mid.lower() for p in patterns)
            or e["platform"] not in live_platforms
        ):
            kill_entries.append(e)
    return kill_keys, kill_entries


def main() -> int:
    dry = "--dry-run" in sys.argv
    od = json.loads((ROOT / "bifrost" / "operator-disabled.json").read_text(encoding="utf-8"))
    keys = _call("GET", "/api/health")["keys"]  # type: ignore[index]
    chain = _call("GET", "/api/fallback")
    kill_keys, kill_entries = plan(od, keys, chain)  # type: ignore[arg-type]
    for k in kill_keys:
        print(f"disable key {k['id']} ({k['platform']})")
        if not dry:
            _call("PATCH", f"/api/keys/{k['id']}", {"enabled": False})
    kill_ids = {e["modelDbId"] for e in kill_entries}
    for e in kill_entries:
        print(f"disable fallback {e['modelDbId']} {e['platform']}/{e['modelId']}")
    if kill_ids and not dry:
        _call(
            "PUT",
            "/api/fallback",
            [
                {
                    "modelDbId": e["modelDbId"],
                    "priority": e["priority"],
                    "enabled": e["enabled"] and e["modelDbId"] not in kill_ids,
                }
                for e in chain  # type: ignore[union-attr]
            ],
        )
    print(f"{'would disable' if dry else 'disabled'}: {len(kill_keys)} keys, {len(kill_entries)} fallback entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
