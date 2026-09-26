"""Apply bifrost/vk-rate-limits.json to the live gateway (idempotent).

Bifrost stores VK rate limits in config.db, set through its admin API
(PUT /api/governance/virtual-keys/{id} with a `rate_limit` object; verified
2026-09-25 that this leaves provider configs and the key value untouched).
`--check` exits 1 if any VK lacks its configured cap or has no entry in the
file (a new VK must get a cap before it ships). Never prints key values.

    python scripts/apply_vk_rate_limits.py          # apply
    python scripts/apply_vk_rate_limits.py --check  # verify only
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:4445"


def load_limits() -> tuple[dict[str, int], str]:
    cfg = json.loads((ROOT / "bifrost" / "vk-rate-limits.json").read_text(encoding="utf-8"))
    return {k: int(v) for k, v in cfg["limits"].items()}, cfg.get("request_reset_duration", "1m")


def plan(vks: list[dict], limits: dict[str, int], reset: str) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Return (updates as (id, name, limit), problems). Pure; unit-tested."""
    updates, problems = [], []
    for v in vks:
        want = limits.get(v["name"])
        if want is None:
            problems.append(f"{v['name']}: no cap in bifrost/vk-rate-limits.json")
            continue
        rl = v.get("rate_limit") or {}
        if rl.get("request_max_limit") != want or rl.get("request_reset_duration") != reset:
            updates.append((v["id"], v["name"], want))
    return updates, problems


def _vks() -> list[dict]:
    with urllib.request.urlopen(f"{BASE}/api/governance/virtual-keys", timeout=10) as r:
        return json.load(r)["virtual_keys"]


def main() -> int:
    limits, reset = load_limits()
    updates, problems = plan(_vks(), limits, reset)
    if "--check" in sys.argv:
        for _vid, name, want in updates:
            problems.append(f"{name}: cap is not {want}/{reset}")
        for p in problems:
            print("VK RATE LIMIT:", p)
        print("vk rate limits ok" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    for vid, name, want in updates:
        body = json.dumps({"rate_limit": {"request_max_limit": want, "request_reset_duration": reset}}).encode()
        req = urllib.request.Request(
            f"{BASE}/api/governance/virtual-keys/{vid}",
            data=body,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"{name}: {want}/{reset} -> HTTP {r.status}")
    for p in problems:
        print("WARNING:", p)
    print(f"applied {len(updates)} update(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
