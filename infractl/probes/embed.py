"""Local embed engine (vllm-embed) probe — same pattern as probes/vllm.py."""
from __future__ import annotations

import time

import httpx

from infractl.probes.base import result
from infractl.settings import Settings


def probe(settings: Settings) -> dict:
    t0 = time.time()
    try:
        resp = httpx.get(f"{settings.infractl_vllm_embed_url}/health", timeout=10.0)
        ok = resp.status_code == 200
        return result("vllm_embed", ok, f"HTTP {resp.status_code}", t0)
    except httpx.HTTPError as exc:
        return result("vllm_embed", False, str(exc), t0)
