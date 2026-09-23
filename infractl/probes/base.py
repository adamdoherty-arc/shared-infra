"""Shared result-shaping helper for every probe module."""
from __future__ import annotations

import time


def result(name: str, ok: bool, detail: str, t0: float) -> dict:
    return {
        "name": name,
        "ok": bool(ok),
        "detail": detail[:2000],
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "ts": time.time(),
    }
