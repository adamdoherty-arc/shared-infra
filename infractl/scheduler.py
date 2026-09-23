"""In-process APScheduler jobs: 5-min pulse (probes + heal rules), 15-min
synthetic per-lane completion, hourly probe_lanes.json regeneration. Runs
inside the same event loop as the FastAPI app (no separate worker process —
Wave 1 scope is a single infractl replica)."""
from __future__ import annotations

import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from infractl.bifrost import admin_api
from infractl.core import actions as actions_mod
from infractl.core.ledger import Ledger
from infractl.heal import rules as heal_rules
from infractl.probes import registry as probe_registry
from infractl.probes.lanes import load_probe_lanes
from infractl.settings import Settings

import asyncio

logger = logging.getLogger("infractl.scheduler")


async def pulse_tick(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    ledger: Ledger = app.state.ledger
    try:
        results = await asyncio.to_thread(probe_registry.run_all, settings, ledger)
        await heal_rules.evaluate(ledger, settings, results)
    except Exception:  # noqa: BLE001 — a scheduler tick must never crash the loop
        logger.exception("pulse_tick failed")


def _run_synthetic_sync(settings: Settings, ledger: Ledger) -> None:
    if not settings.infra_probe_vk:
        logger.info("synthetic_tick skipped: INFRA_PROBE_VK not set")
        return
    try:
        lanes, _skipped = load_probe_lanes(settings.config_json_path)
    except Exception:  # noqa: BLE001
        logger.exception("synthetic_tick: could not load probe lanes")
        return
    for lane in lanes:
        model = lane["model"]
        t0 = time.time()
        try:
            ok, detail = admin_api.synthetic_completion(
                settings.infractl_probe_base, settings.infra_probe_vk, model=model,
                timeout_s=float(lane["timeout_s"]),
            )
        except Exception as exc:  # noqa: BLE001 — one bad lane must not skip the rest
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.time() - t0) * 1000, 1)
        provider = model.split("/", 1)[0]
        ledger.con.execute(
            "INSERT INTO lane_health (provider, model, up, latency_ms, ts) VALUES (?,?,?,?,?) "
            "ON CONFLICT(provider, model) DO UPDATE SET up=excluded.up, "
            "latency_ms=excluded.latency_ms, ts=excluded.ts",
            (provider, model, int(ok), latency_ms, time.time()),
        )
        if not ok:
            logger.warning("synthetic_tick: lane %s down (%s)", model, detail)


async def synthetic_tick(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    ledger: Ledger = app.state.ledger
    try:
        await asyncio.to_thread(_run_synthetic_sync, settings, ledger)
    except Exception:  # noqa: BLE001
        logger.exception("synthetic_tick failed")


async def probe_lanes_tick(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    ledger: Ledger = app.state.ledger
    try:
        await actions_mod.execute(
            ledger, settings, "probe_lanes_regenerate", {}, requested_by="scheduler",
            reason="hourly probe_lanes.json regeneration",
        )
    except actions_mod.ActionError as exc:
        logger.info("probe_lanes_tick did not fire: %s", exc)
    except Exception:  # noqa: BLE001
        logger.exception("probe_lanes_tick failed")


async def brain_evaluator_tick(app: FastAPI) -> None:
    evaluator = getattr(app.state, "brain_evaluator", None)
    if evaluator is None:
        return
    try:
        await evaluator.evaluate_system_health()
    except Exception:  # noqa: BLE001
        logger.exception("brain_evaluator_tick failed")


async def model_discovery_tick(app: FastAPI) -> None:
    scanner = getattr(app.state, "model_scanner", None)
    if scanner is None:
        return
    try:
        await scanner.run_discovery_and_reconcile()
    except Exception:  # noqa: BLE001
        logger.exception("model_discovery_tick failed")


def start_scheduler(app: FastAPI) -> AsyncIOScheduler:
    settings: Settings = app.state.settings
    scheduler = AsyncIOScheduler()
    scheduler.add_job(pulse_tick, "interval", seconds=settings.infractl_pulse_interval_s,
                       args=[app], id="pulse_tick", max_instances=1, coalesce=True)
    scheduler.add_job(synthetic_tick, "interval", seconds=settings.infractl_synthetic_interval_s,
                       args=[app], id="synthetic_tick", max_instances=1, coalesce=True)
    scheduler.add_job(probe_lanes_tick, "interval", seconds=settings.infractl_probe_lanes_interval_s,
                       args=[app], id="probe_lanes_tick", max_instances=1, coalesce=True)
    scheduler.add_job(brain_evaluator_tick, "interval", seconds=1800,
                       args=[app], id="brain_evaluator_tick", max_instances=1, coalesce=True)
    scheduler.add_job(model_discovery_tick, "interval", seconds=21600,
                       args=[app], id="model_discovery_tick", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler


def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    scheduler.shutdown(wait=False)
