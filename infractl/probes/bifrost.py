"""Gateway health probe — shared-bifrost's own `/health` (fast liveness)
plus an UNAUTHENTICATED `/v1/models` reachability check. Deliberately
unauthenticated — see `bifrost/admin_api.list_models()`'s docstring for the
2026-09-15 finding that an authenticated `/v1/models` call hangs past 30s
on this production Bifrost instance while the same call unauthenticated
(a fast 401) and `/health` both answer in under half a second. This probe
therefore treats "reachable and enforcing auth" (401, or any status <500)
as green; it is not trying to prove any specific model is servable — the
`vllm` / `embed` probes and the scheduler's per-lane synthetic_completion
tick already do that with real, authenticated requests."""
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
        settings.infractl_probe_base, vk=None, timeout_s=5.0
    )
    ok = ok_health and ok_reachable
    return result("bifrost", ok, f"health: {health_detail}; models(unauth): {models_detail}", t0)
