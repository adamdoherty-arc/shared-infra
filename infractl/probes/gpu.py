"""GPU probe — dcgm-exporter has no HTTP health endpoint (the base image
carries no wget/curl/nc/sh, per docker-compose.observability.yml's comment
on why it has no HEALTHCHECK), so liveness here IS reachability of its own
`/metrics` Prometheus exposition, same as observability's own
`up{job="dcgm-exporter"}` approach. When reachable, the current GPU
utilization + framebuffer-used gauges are extracted for the detail string
so a red probe carries a number, not just 'down'."""
from __future__ import annotations

import time

import httpx

from infractl.probes.base import result
from infractl.settings import Settings


def _extract_gauge(metrics_text: str, name: str) -> float | None:
    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        # e.g. `DCGM_FI_DEV_GPU_UTIL{gpu="0",UUID="..."} 42`
        try:
            return float(line.rsplit(" ", 1)[-1])
        except ValueError:
            continue
    return None


def probe(settings: Settings) -> dict:
    t0 = time.time()
    try:
        resp = httpx.get(settings.infractl_dcgm_url, timeout=10.0)
        resp.raise_for_status()
        text = resp.text
    except httpx.HTTPError as exc:
        return result("gpu", False, str(exc), t0)

    util = _extract_gauge(text, "DCGM_FI_DEV_GPU_UTIL")
    mem_used = _extract_gauge(text, "DCGM_FI_DEV_FB_USED")
    detail = f"reachable; gpu_util={util}% fb_used_mib={mem_used}"
    return result("gpu", True, detail, t0)
