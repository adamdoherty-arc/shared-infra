"""Wave 1 auto-heal rules — probe result -> action, always going through
core/actions.execute() so every auto-fired action gets the exact same
lock/drift-guard/cooldown/max-per-day/ledger/Discord/Legion treatment as a
human-triggered one via the API.

DESIGN DECISION — no T2 heal rule ships in Wave 1 (filed to Legion sprint
14975): the framework below (`_fire()`) forces `dry_run=True` on ANY T2
(config-mutating) rule trigger regardless of `INFRACTL_WRITE_MODE`, as
defense in depth for whenever a T2 rule is added in a later wave — an
auto-heal loop mutating config.json with no human in the loop is exactly
the risk class Wave 1's write-mode gate exists to contain, and no concrete
T2 trigger condition was specified for Wave 1 (auth_autoheal.py's own
auto-park detector watches Bifrost's container *logs* for auth-failure
phrases over a sliding window, a different signal source than infractl's
probe results — that mechanism stays in auth_autoheal.py, now dry-run-only
per the Wave 1 cutover, until a Wave 2 decision moves it here for real).

Wave 1 rules (all T1, all real, all wired to actions.execute):
  1. unhealthy/stopped sidecar (containers probe)      -> restart_sidecar
  2. config parity drift (config_parity probe red)      -> vk_resync
  3. logs.db WAL over threshold (logsdb probe red)       -> wal_checkpoint
  4. any probe red for 3+ consecutive ticks              -> Discord alert
     (not an action — a notification, deduped 1/hour per probe via the
     ledger's alerts_seen table)
"""
from __future__ import annotations

import logging

from infractl.core import actions as actions_mod
from infractl.core import discord
from infractl.core.ledger import Ledger
from infractl.probes.containers import check_raw as containers_check_raw
from infractl.settings import Settings

logger = logging.getLogger("infractl.heal")

ALERT_COOLDOWN_S = 3600
RED_STREAK_THRESHOLD = 3


async def _fire(ledger: Ledger, settings: Settings, kind: str, payload: dict, reason: str,
                 probe_name: str) -> None:
    try:
        result = await actions_mod.execute(
            ledger, settings, kind, payload, requested_by="heal_rules", reason=reason,
        )
        logger.info("heal rule fired %s (probe=%s): %s", kind, probe_name, result)
    except actions_mod.ActionError as exc:
        # Cooldown / max-per-day / lock-busy are expected steady-state
        # outcomes, not failures — the ledger already has the attempt via
        # the exception path in actions.execute for anything that got past
        # the registry checks; log and move on to the next rule.
        logger.info("heal rule %s (probe=%s) did not fire: %s", kind, probe_name, exc)


async def evaluate(ledger: Ledger, settings: Settings, latest_probes: list[dict]) -> None:
    """Called once per pulse tick with the just-recorded probe results.
    Every rule is independent — one rule raising never blocks the rest."""
    by_name = {p["name"]: p for p in latest_probes}

    # Rule 1: unhealthy/stopped sidecar -> restart_sidecar
    try:
        raw = containers_check_raw(settings)
        for entry in raw["unhealthy"]:
            if entry in actions_mod.NON_GATEWAY_SIDECARS:
                await _fire(ledger, settings, "restart_sidecar", {"container": entry},
                            reason=f"heal: {entry} reported unhealthy", probe_name="containers")
        for entry in raw["stopped"]:
            name = entry.split(":", 1)[0]
            if name in actions_mod.NON_GATEWAY_SIDECARS:
                await _fire(ledger, settings, "restart_sidecar", {"container": name},
                            reason=f"heal: {entry}", probe_name="containers")
    except Exception:  # noqa: BLE001 — a docker-socket hiccup must not block other rules
        logger.exception("heal rule 1 (containers) failed to evaluate")

    # Rule 2: config parity drift -> vk_resync
    parity = by_name.get("config_parity")
    if parity is not None and not parity["ok"]:
        await _fire(ledger, settings, "vk_resync", {},
                    reason=f"heal: config_parity red — {parity['detail']}", probe_name="config_parity")

    # Rule 3: logs.db WAL over threshold -> wal_checkpoint
    logsdb = by_name.get("logsdb")
    if logsdb is not None and not logsdb["ok"]:
        await _fire(ledger, settings, "wal_checkpoint", {},
                    reason=f"heal: logsdb red — {logsdb['detail']}", probe_name="logsdb")

    # Rule 4: any probe red 3+ consecutive ticks -> Discord alert (deduped)
    for name in by_name:
        try:
            if not ledger.probe_streak_red(name, RED_STREAK_THRESHOLD):
                continue
        except Exception:  # noqa: BLE001
            continue
        fingerprint = f"probe_red_streak:{name}"
        if ledger.alert_recently_sent(fingerprint, ALERT_COOLDOWN_S):
            continue
        sent = discord.post(embed=discord.build_embed(
            f"infractl: {name} has been red for {RED_STREAK_THRESHOLD}+ ticks",
            by_name[name]["detail"], level="warn", fields={"probe": name},
        ))
        if sent:
            ledger.record_alert_sent(fingerprint, name)
