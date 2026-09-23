"""X-Infractl-Token auth. /healthz and /metrics are open (scraped by
Prometheus + Docker healthcheck, neither of which can carry a bearer token
in the compose file's `test:` array without leaking it into `docker inspect`)."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from infractl.settings import get_settings

OPEN_PATHS = {"/healthz", "/metrics"}


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def require_token(x_infractl_token: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.infractl_token:
        # No token configured -> refuse everything rather than silently
        # running open. This is a fail-closed default, not a convenience.
        raise HTTPException(status_code=503, detail="infractl_token_not_configured")
    if not x_infractl_token or not constant_time_eq(x_infractl_token, settings.infractl_token):
        raise HTTPException(status_code=401, detail="invalid_token")
