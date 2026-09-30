"""Probe registry — the full Wave 1 probe list, run every pulse tick
(5 min default, `INFRACTL_PULSE_INTERVAL_S`). Each probe module is isolated:
an exception inside one never stops the rest from running, and is itself
recorded as a red probe result so a probe module's own bug is visible
instead of silently skipping that check forever."""
from __future__ import annotations

import time
import traceback
from typing import Callable

from infractl.core.ledger import Ledger
from infractl.probes import (
    alertmanager, bifrost, config_parity, consumers, containers, disk, embed,
    gpu, logsdb, prometheus, vllm,
)
from infractl.settings import Settings

PROBE_MODULES: list[Callable[[Settings], dict]] = [
    containers.probe,
    bifrost.probe,
    vllm.probe,
    embed.probe,
    gpu.probe,
    disk.probe,
    logsdb.probe,
    config_parity.probe,
    prometheus.probe,
    alertmanager.probe,
    consumers.probe,
]


def run_all(settings: Settings, ledger: Ledger | None = None) -> list[dict]:
    results: list[dict] = []
    for probe_fn in PROBE_MODULES:
        t0 = time.time()
        try:
            r = probe_fn(settings)
        except Exception as exc:  # noqa: BLE001 — a probe crashing must not skip the rest
            r = {
                "name": getattr(probe_fn, "__module__", "unknown").rsplit(".", 1)[-1],
                "ok": False,
                "detail": f"probe raised {type(exc).__name__}: {exc}\n{traceback.format_exc()[-500:]}",
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "ts": time.time(),
            }
        results.append(r)
        if ledger is not None:
            ledger.record_probe(r["name"], r["ok"], r["detail"], r["latency_ms"], r["ts"])
    return results
