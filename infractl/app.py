"""FastAPI app — the shared-infra control plane API. Every response uses the
`{ok, data, error}` envelope. `/healthz` and `/metrics` are open (scraped by
Docker's own HEALTHCHECK + Prometheus, neither of which can carry a bearer
token); every other route depends on `auth.require_token`."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from infractl import consumers as consumers_pkg
from infractl.auth import require_token
from infractl.bifrost import config as bifrost_config
from infractl.bifrost import configdb
from infractl.brain.client import GeminiBrainClient
from infractl.brain.evaluator import BrainEvaluator
from infractl.brain.model_scanner import ModelScanner
from infractl.core import actions as actions_mod
from infractl.core.actions import ActionError
from infractl.core.ledger import Ledger
from infractl.core.lock import LockTimeoutError, WriterConflictError
from infractl.heal import rules as heal_rules
from infractl.probes import registry as probe_registry
from infractl.scheduler import start_scheduler, stop_scheduler
from infractl.settings import Settings, get_settings

__version__ = "0.1.0"


def envelope_ok(data: Any = None) -> dict:
    return {"ok": True, "data": data, "error": None}


def envelope_err(message: str, code: str = "error") -> dict:
    return {"ok": False, "data": None, "error": {"code": code, "message": message}}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ledger = Ledger(settings.ledger_db_path)
    app.state.settings = settings
    app.state.ledger = ledger
    app.state.start_ts = time.time()
    brain_client = GeminiBrainClient(settings, ledger)
    app.state.brain_client = brain_client
    app.state.brain_evaluator = BrainEvaluator(settings, ledger, brain_client)
    app.state.model_scanner = ModelScanner(settings, ledger, brain_client)
    scheduler = start_scheduler(app)
    yield
    stop_scheduler(scheduler)
    ledger.close()


app = FastAPI(title="infractl", version=__version__, lifespan=lifespan)


def get_ledger(request: Request) -> Ledger:
    return request.app.state.ledger


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


# ---- error handling: every raised ActionError/LockTimeoutError/
# WriterConflictError maps to the envelope shape instead of FastAPI's
# default {detail: ...} ----

@app.exception_handler(ActionError)
async def _action_error_handler(request: Request, exc: ActionError):
    status = {
        "unknown_kind": 404, "not_found": 404, "cooldown": 429, "max_per_day": 429,
        "requires_approval": 409, "invalid_state": 409, "not_allowlisted": 400,
        "not_implemented": 501, "verify_failed": 502, "rolled_back": 502, "no_snapshot": 409,
    }.get(exc.code, 400)
    return JSONResponse(status_code=status, content=envelope_err(str(exc), exc.code))


@app.exception_handler(LockTimeoutError)
async def _lock_timeout_handler(request: Request, exc: LockTimeoutError):
    return JSONResponse(status_code=423, content=envelope_err(str(exc), "locked"))


@app.exception_handler(WriterConflictError)
async def _writer_conflict_handler(request: Request, exc: WriterConflictError):
    return JSONResponse(status_code=409, content=envelope_err(str(exc), "writer_conflict"))


# ---- open routes ----

@app.get("/healthz")
async def healthz(request: Request):
    ledger: Ledger = request.app.state.ledger
    try:
        ledger.con.execute("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"ok": db_ok, "version": __version__, "uptime_s": round(time.time() - request.app.state.start_ts, 1)}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    lines = [
        "# HELP infractl_probe_ok 1 if the named probe's last run was healthy",
        "# TYPE infractl_probe_ok gauge",
    ]
    for p in ledger.latest_probes():
        lines.append(f'infractl_probe_ok{{name="{p["name"]}"}} {1 if p["ok"] else 0}')
    lines.append("# HELP infractl_last_probe_ts unix timestamp of the last probe run seen")
    lines.append("# TYPE infractl_last_probe_ts gauge")
    latest_ts = max((p["ts"] for p in ledger.latest_probes()), default=0)
    lines.append(f"infractl_last_probe_ts {latest_ts}")

    lines.append("# HELP infractl_action_total actions by kind and terminal status")
    lines.append("# TYPE infractl_action_total counter")
    counts: dict[tuple[str, str], int] = {}
    for a in ledger.list_actions():
        counts[(a["kind"], a["status"])] = counts.get((a["kind"], a["status"]), 0) + 1
    for (kind, status), n in counts.items():
        lines.append(f'infractl_action_total{{kind="{kind}",status="{status}"}} {n}')

    lines.append("# HELP infractl_brain_calls_today infra-brain (Gemini) calls used today")
    lines.append("# TYPE infractl_brain_calls_today gauge")
    lines.append(f"infractl_brain_calls_today {ledger.brain_calls_today()}")
    return "\n".join(lines) + "\n"


# ---- authed routes ----

@app.get("/api/status", dependencies=[Depends(require_token)])
async def api_status(request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    return envelope_ok({
        "version": __version__,
        "write_mode": settings.infractl_write_mode,
        "uptime_s": round(time.time() - request.app.state.start_ts, 1),
        "probes": ledger.latest_probes(),
        "recent_actions": ledger.list_actions()[:10],
    })


@app.get("/api/health", dependencies=[Depends(require_token)])
async def api_health(request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    results = await asyncio.to_thread(probe_registry.run_all, settings, ledger)
    return envelope_ok({"probes": results, "all_ok": all(r["ok"] for r in results)})


@app.get("/api/probes", dependencies=[Depends(require_token)])
async def api_probes(request: Request):
    ledger: Ledger = request.app.state.ledger
    return envelope_ok(ledger.latest_probes())


@app.post("/api/probes/run", dependencies=[Depends(require_token)])
async def api_probes_run(request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    results = await asyncio.to_thread(probe_registry.run_all, settings, ledger)
    await heal_rules.evaluate(ledger, settings, results)
    return envelope_ok(results)


@app.get("/api/consumers", dependencies=[Depends(require_token)])
async def api_consumers(request: Request):
    settings: Settings = request.app.state.settings
    try:
        data = consumers_pkg.list_consumers(settings)
    except consumers_pkg.ConsumersSchemaError as exc:
        return JSONResponse(status_code=422, content=envelope_err(str(exc), "consumers_schema_invalid"))
    return envelope_ok(data)


@app.get("/api/config/providers", dependencies=[Depends(require_token)])
async def api_config_providers(request: Request):
    settings: Settings = request.app.state.settings
    active = bifrost_config.read_config(settings.config_json_path).get("providers", {})
    disabled = {}
    if settings.disabled_providers_path.exists():
        disabled = bifrost_config.read_config(settings.disabled_providers_path).get("providers", {})
    return envelope_ok({"active": sorted(active.keys()), "disabled": sorted(disabled.keys())})


@app.get("/api/config/lint", dependencies=[Depends(require_token)])
async def api_config_lint(request: Request):
    settings: Settings = request.app.state.settings
    cfg = bifrost_config.read_config(settings.config_json_path)
    problems = bifrost_config.lint(cfg)
    return envelope_ok({"problems": problems, "clean": not problems})


@app.get("/api/config/vks", dependencies=[Depends(require_token)])
async def api_config_vks(request: Request):
    settings: Settings = request.app.state.settings
    return envelope_ok(configdb.list_virtual_keys(settings.config_db_path))


@app.get("/api/config/snapshot", dependencies=[Depends(require_token)])
async def api_config_snapshot(request: Request):
    settings: Settings = request.app.state.settings
    return envelope_ok(bifrost_config.render_redacted_snapshot(settings.infractl_bifrost_dir))


class ParkBody(BaseModel):
    reason: str = ""
    requested_by: str = "api"
    legion_ref: str | None = None
    dry_run: bool | None = None


@app.post("/api/config/providers/{name}/park", dependencies=[Depends(require_token)])
async def api_park_provider(name: str, body: ParkBody, request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    result = await actions_mod.execute(
        ledger, settings, "provider_park", {"provider": name, "reason": body.reason},
        requested_by=body.requested_by, reason=body.reason or f"park {name} via API",
        legion_ref=body.legion_ref, dry_run=body.dry_run,
    )
    return envelope_ok(result)


class UnparkBody(BaseModel):
    requested_by: str = "api"
    legion_ref: str | None = None
    dry_run: bool | None = None


@app.post("/api/config/providers/{name}/unpark", dependencies=[Depends(require_token)])
async def api_unpark_provider(name: str, body: UnparkBody, request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    result = await actions_mod.execute(
        ledger, settings, "provider_unpark", {"provider": name},
        requested_by=body.requested_by, reason=f"unpark {name} via API",
        legion_ref=body.legion_ref, dry_run=body.dry_run,
    )
    return envelope_ok(result)


class ModelsBody(BaseModel):
    provider: str
    key_name: str
    models: list[str]
    op: str  # "add" | "remove"
    requested_by: str = "api"
    legion_ref: str | None = None
    dry_run: bool | None = None


@app.post("/api/config/models", dependencies=[Depends(require_token)])
async def api_config_models(body: ModelsBody, request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    if body.op not in ("add", "remove"):
        return JSONResponse(status_code=400, content=envelope_err("op must be 'add' or 'remove'", "bad_request"))
    kind = "models_add" if body.op == "add" else "models_remove"
    result = await actions_mod.execute(
        ledger, settings, kind,
        {"provider": body.provider, "key_name": body.key_name, "models": body.models},
        requested_by=body.requested_by, reason=f"{body.op} models via API",
        legion_ref=body.legion_ref, dry_run=body.dry_run,
    )
    return envelope_ok(result)


class ResyncBody(BaseModel):
    requested_by: str = "api"
    dry_run: bool | None = None


@app.post("/api/vk/resync", dependencies=[Depends(require_token)])
async def api_vk_resync(body: ResyncBody, request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    result = await actions_mod.execute(
        ledger, settings, "vk_resync", {}, requested_by=body.requested_by,
        reason="manual vk_resync via API", dry_run=body.dry_run,
    )
    return envelope_ok(result)


class ActionBody(BaseModel):
    kind: str
    payload: dict = {}
    reason: str
    requested_by: str = "api"
    legion_ref: str | None = None
    confirm: bool = False
    dry_run: bool | None = None


@app.post("/api/actions", dependencies=[Depends(require_token)])
async def api_create_action(body: ActionBody, request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    result = await actions_mod.execute(
        ledger, settings, body.kind, body.payload, requested_by=body.requested_by,
        reason=body.reason, legion_ref=body.legion_ref, confirm=body.confirm, dry_run=body.dry_run,
    )
    return envelope_ok(result)


@app.get("/api/actions/{action_id}", dependencies=[Depends(require_token)])
async def api_get_action(action_id: str, request: Request):
    ledger: Ledger = request.app.state.ledger
    row = ledger.get_action(action_id)
    if row is None:
        return JSONResponse(status_code=404, content=envelope_err(f"action '{action_id}' not found", "not_found"))
    return envelope_ok(row)


@app.post("/api/actions/{action_id}/approve", dependencies=[Depends(require_token)])
async def api_approve_action(action_id: str, request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    result = await actions_mod.approve(ledger, settings, action_id)
    return envelope_ok(result)


@app.post("/api/actions/{action_id}/rollback", dependencies=[Depends(require_token)])
async def api_rollback_action(action_id: str, request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    result = await actions_mod.manual_rollback(ledger, settings, action_id)
    return envelope_ok(result)


@app.get("/api/audit", dependencies=[Depends(require_token)])
async def api_audit(request: Request, since: float | None = None):
    ledger: Ledger = request.app.state.ledger
    return envelope_ok(ledger.list_actions(since=since))


class AlertmanagerWebhook(BaseModel):
    receiver: str | None = None
    status: str | None = None
    alerts: list[dict] = []


@app.post("/api/notify/alertmanager", dependencies=[Depends(require_token)])
async def api_notify_alertmanager(body: AlertmanagerWebhook, request: Request):
    """Alertmanager webhook receiver — forwards firing/resolved alerts to
    Discord with dedupe via the ledger's alerts_seen table, keyed on
    Alertmanager's own fingerprint so a repeat notify for the same alert
    inside the cooldown window is a no-op."""
    from infractl.core import discord as discord_mod

    ledger: Ledger = request.app.state.ledger
    forwarded = []
    for alert in body.alerts:
        fingerprint = alert.get("fingerprint") or str(hash(str(alert.get("labels"))))
        if ledger.alert_recently_sent(f"am:{fingerprint}", 3600):
            continue
        name = (alert.get("labels") or {}).get("alertname", "unknown")
        level = "error" if alert.get("status") == "firing" else "ok"
        sent = discord_mod.post(embed=discord_mod.build_embed(
            f"Alertmanager: {name} {alert.get('status', '')}",
            (alert.get("annotations") or {}).get("description", ""),
            level=level, fields=(alert.get("labels") or {}),
        ))
        if sent:
            ledger.record_alert_sent(f"am:{fingerprint}", name)
            forwarded.append(name)
    return envelope_ok({"forwarded": forwarded, "received": len(body.alerts)})


# ---- brain & improvement routes ----

@app.get("/api/brain/status", dependencies=[Depends(require_token)])
async def api_brain_status(request: Request):
    settings: Settings = request.app.state.settings
    ledger: Ledger = request.app.state.ledger
    brain_client: GeminiBrainClient = request.app.state.brain_client
    return envelope_ok({
        "model": settings.infra_gemini_model,
        "calls_today": ledger.brain_calls_today(),
        "max_calls_per_day": settings.infra_brain_max_calls_per_day,
        "key_pool": brain_client.get_pool_status(),
        "active_keys_count": len([k for k in brain_client.key_states.values() if k.status == "active"]),
    })


@app.post("/api/brain/evaluate", dependencies=[Depends(require_token)])
async def api_brain_evaluate(request: Request):
    evaluator: BrainEvaluator = request.app.state.brain_evaluator
    result = await evaluator.evaluate_system_health()
    return envelope_ok(result)


class ScanBody(BaseModel):
    dry_run: bool | None = None


@app.post("/api/brain/scan", dependencies=[Depends(require_token)])
async def api_brain_scan(request: Request, body: ScanBody = ScanBody()):
    scanner: ModelScanner = request.app.state.model_scanner
    result = await scanner.run_discovery_and_reconcile(dry_run=body.dry_run)
    return envelope_ok(result)


@app.get("/api/improvements", dependencies=[Depends(require_token)])
async def api_improvements(request: Request, limit: int = 50):
    ledger: Ledger = request.app.state.ledger
    rows = ledger.con.execute(
        "SELECT id, title, detail, source, ts FROM improvement_ledger ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return envelope_ok([dict(r) for r in rows])


# ---- static UI ----
import os as _os

_UI_DIR = _os.path.join(_os.path.dirname(__file__), "ui")
if _os.path.isdir(_UI_DIR):
    app.mount("/", StaticFiles(directory=_UI_DIR, html=True), name="ui")
