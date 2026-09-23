"""Discord webhook posting. Explicit User-Agent — Cloudflare 1010 blocks the
default `Python-urllib/x.y` UA (same fix bifrost/auth_autoheal.py already
carries, `DISCORD_USER_AGENT = "shared-infra-bifrost-autoheal/1.1 ..."`;
infractl gets its own UA string so the two sidecars are distinguishable in
any future Cloudflare/Discord-side audit)."""
from __future__ import annotations

import json
import time
import urllib.request

from infractl.settings import get_settings

USER_AGENT = "shared-infra-infractl/0.1 (+https://github.com/adamdoherty-arc/shared-infra)"

_COLOR = {"info": 0x3498DB, "warn": 0xF1C40F, "error": 0xE74C3C, "ok": 0x2ECC71}


def build_embed(title: str, description: str, level: str = "info",
                 fields: dict[str, str] | None = None) -> dict:
    embed: dict = {
        "title": title,
        "description": description[:4000],
        "color": _COLOR.get(level, _COLOR["info"]),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if fields:
        embed["fields"] = [
            {"name": k, "value": str(v)[:1000], "inline": True} for k, v in fields.items()
        ]
    return embed


def post(content: str | None = None, embed: dict | None = None, webhook_url: str | None = None) -> bool:
    """Best-effort. Returns False (never raises) on failure — Discord being
    down must never block a heal action or probe cycle."""
    url = webhook_url or get_settings().discord_infra_webhook
    if not url:
        return False
    payload: dict = {}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]
    if not payload:
        return False
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:  # noqa: BLE001 — best-effort notifier, never propagate
        return False
