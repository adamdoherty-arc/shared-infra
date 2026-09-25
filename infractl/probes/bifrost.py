"""Gateway health probe -- shared-bifrost's own `/health` (fast liveness)
plus an AUTHENTICATED `/v1/models` using INFRA_PROBE_VK (0.15s on v2.0.0,
re-measured 2026-09-25). The unauthenticated form made Bifrost log a
"virtual key is required" error per provider on every tick; see
`bifrost/admin_api.list_models()`. Model servability itself is proven by the
`vllm` / `embed` probes and the scheduler's per-lane synthetic_completion."""
from __future__ import annotations

import time

from infractl.bifrost import admin_api
from infractl.probes.base import result
from infractl.settings import Settings


def probe(settings: Settings) -> dict:
    t0 = time.time()
    ok_health, health_detail = admin_api.health(settings.infractl_probe_base)
    if not ok_health:
        return result("bifrost", False, f"health: {health_detail}", t0)
    ok_reachable, _count, models_detail = admin_api.list_models(
        settings.infractl_probe_base, vk=settings.infra_probe_vk or None, timeout_s=5.0
    )
    ok = ok_health and ok_reachable
    return result("bifrost", ok, f"health: {health_detail}; models: {models_detail}", t0)
