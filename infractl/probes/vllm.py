"""Local chat engine (qwen38-chat) probe — hits the upstream vLLM engine's
own `/health` directly (not through Bifrost), same target
`vllm_wedge_monitor.py` polls via `VLLM_METRICS_URL`/`/metrics`. This probe
stays cheap (a plain /health GET); the wedge-specific num_requests_running
vs generation_tokens_total stall detection is vllm-wedge-monitor's own job
(a stateful, cross-poll comparison) and is not duplicated here."""
from __future__ import annotations

import time

import httpx

from infractl.probes.base import result
from infractl.settings import Settings


def probe(settings: Settings) -> dict:
    t0 = time.time()
    try:
        resp = httpx.get(f"{settings.infractl_qwen38_url}/health", timeout=10.0)
        ok = resp.status_code == 200
        return result("vllm_chat", ok, f"HTTP {resp.status_code}", t0)
    except httpx.HTTPError as exc:
        return result("vllm_chat", False, str(exc), t0)
