"""Alertmanager health probe — its own `/-/healthy` endpoint."""
from __future__ import annotations

import time

import httpx

from infractl.probes.base import result
from infractl.settings import Settings


def probe(settings: Settings) -> dict:
    t0 = time.time()
    try:
        resp = httpx.get(f"{settings.infractl_alertmanager_url}/-/healthy", timeout=10.0)
        return result("alertmanager", resp.status_code == 200, f"HTTP {resp.status_code}", t0)
    except httpx.HTTPError as exc:
        return result("alertmanager", False, str(exc), t0)
