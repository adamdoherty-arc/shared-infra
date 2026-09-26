"""Action allowlist with tiers T0-T3, cooldown + max/day per kind, and the
single `execute()` entrypoint every mutating call goes through: lock ->
drift guard -> plan (pure, validated) -> snapshot -> write -> binding
restart (stop autoheal -> stop gateway -> vk_sync -> start -> /health ->
1-token probe -> start autoheal) -> verify -> rollback on failure -> ledger
finalize -> redacted snapshot -> Discord -> Legion note.

infractl is the SINGLE WRITER of bifrost/config.json and
bifrost/disabled-providers.json (2026-09-25): ADA's bifrost_model_sync.py
posts `bifrost_models_apply`, bifrost-autoheal posts `bifrost_provider_park`,
and humans use the CLI. `.claude/hooks/config_write_gate.py` blocks agent
edits of those files, and scripts/config_autocommit.py (hostcron) commits
what infractl wrote with the ledger rows as the message.

TIERS
  T0  read-only. No lock, not registered here (plain GET handlers).
  T1  auto, no approval, no config.json mutation: restart_sidecar,
      wal_checkpoint, logsdb_quick_check, probe_lanes_regenerate.
  T2  the full snapshot/verify/rollback ladder, because they mutate
      config.json/config.db or restart the gateway: bifrost_models_apply,
      bifrost_provider_park, provider_unpark, models_add, models_remove,
      alias_set, bifrost_restart, vk_resync. heal rules only ever DRY-RUN a
      T2 kind (heal/rules.py `_fire`).
  T3  requires POST /api/actions/{id}/approve, NEVER auto-scheduled by heal
      rules: restart_qwen38_chat, restart_vllm_embed, disk_prune. Only
      kinds whose mechanics are implemented today are registered -- per the
      NEVER SHIP STUBS rule, vk_budget_set/vk_rotate/compose_apply/model_swap
      are NOT registered until their real mechanics exist.
"""
from __future__ import annotations

import asyncio
import dataclasses
import difflib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from infractl.bifrost import admin_api, park, restart
from infractl.bifrost import config as bifrost_config
from infractl.core import discord, legion
from infractl.core import docker as docker_client
from infractl.core import ledger as ledger_mod
from infractl.core.lock import acquire_write_lock, check_drift
from infractl.settings import Settings

# The one requester that must not have bifrost-autoheal stopped under it:
# the sidecar is blocked on the park request for the whole ladder.
AUTOHEAL_REQUESTER = "bifrost-autoheal"


class ActionError(RuntimeError):
    def __init__(self, message: str, code: str = "action_failed"):
        super().__init__(message)
        self.code = code


@dataclasses.dataclass
class ActionSpec:
    kind: str
    tier: str  # "T1" | "T2" | "T3"
    cooldown_s: int
    max_per_day: int
    mutates_config: bool
    description: str


# cooldown/max-per-day intentionally conservative for Wave 1 — these are the
# numbers heal rules are allowed to fire automatically at; a human via the
# API can always request more, but still pays the cooldown.
REGISTRY: dict[str, ActionSpec] = {
    "restart_sidecar": ActionSpec("restart_sidecar", "T1", 120, 20, False, "restart a non-gateway sidecar container"),
    "wal_checkpoint": ActionSpec("wal_checkpoint", "T1", 300, 12, False, "checkpoint logs.db-wal in-container"),
    "logsdb_quick_check": ActionSpec("logsdb_quick_check", "T1", 1800, 6, False, "PRAGMA quick_check in-container"),
    "probe_lanes_regenerate": ActionSpec("probe_lanes_regenerate", "T1", 900, 24, False,
                                         "regenerate /state/probe_lanes.json"),
    "bifrost_models_apply": ActionSpec("bifrost_models_apply", "T2", 60, 12, True,
                                       "add/remove models across a provider's keys ({provider: {add, remove}})"),
    "bifrost_provider_park": ActionSpec("bifrost_provider_park", "T2", 60, 10, True,
                                        "move a provider block to disabled-providers.json"),
    "provider_unpark": ActionSpec("provider_unpark", "T2", 60, 10, True,
                                  "restore a provider block from disabled-providers.json"),
    "models_add": ActionSpec("models_add", "T2", 30, 20, True, "add models to one provider key"),
    "models_remove": ActionSpec("models_remove", "T2", 30, 20, True, "remove models from one provider key"),
    "alias_set": ActionSpec("alias_set", "T2", 30, 20, True, "set an alias on a provider key"),
    "bifrost_restart": ActionSpec("bifrost_restart", "T2", 120, 12, False, "run the binding restart sequence"),
    # A sync only takes effect with Bifrost stopped and restarted, so vk_resync
    # IS the binding restart sequence (it used to write config.db under a
    # running gateway, the WAL hazard 30-docker.md forbids).
    "vk_resync": ActionSpec("vk_resync", "T2", 600, 6, False, "sync config.json -> config.db via the binding restart"),
    "restart_qwen38_chat": ActionSpec("restart_qwen38_chat", "T3", 300, 6, False, "restart the local chat vLLM engine"),
    "restart_vllm_embed": ActionSpec("restart_vllm_embed", "T3", 300, 6, False, "restart the local embed vLLM engine"),
    "disk_prune": ActionSpec("disk_prune", "T3", 3600, 4, False,
                             "prune dangling docker volumes/images + old .bak files"),
}

CONFIG_FILES = ("config.json", "disabled-providers.json")

NON_GATEWAY_SIDECARS = {
    "bifrost-metrics", "bifrost-logs-pruner", "shared-alertmanager", "otelcol", "loki",
    "cadvisor", "dcgm-exporter", "vllm-autoheal", "vllm-wedge-monitor",
}


def _snapshot_dir(state_dir: Path, action_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    d = state_dir / "snapshots" / f"{stamp}-{action_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_config(bifrost_dir: Path, dest: Path) -> list[str]:
    files = []
    for name in ("config.json", "disabled-providers.json", "config.db"):
        src = bifrost_dir / name
        if src.exists():
            shutil.copy2(src, dest / name)
            files.append(name)
    return files


def _restore_config(bifrost_dir: Path, snapshot_dir: Path) -> list[str]:
    """Restores config.json + disabled-providers.json ONLY. config.db is
    never copied back over the live file (the gateway may be running, and a
    copied SQLite file under a live WAL is the corruption class 30-docker.md
    records twice); the binding restart that follows every restore rebuilds
    config.db from the restored config.json via the VK sync, with Bifrost
    stopped. The snapshot's config.db copy is kept for forensics."""
    files = []
    for name in CONFIG_FILES:
        src = snapshot_dir / name
        if src.exists():
            shutil.copy2(src, bifrost_dir / name)
            files.append(name)
    return files


def verify_gateway(settings: Settings) -> tuple[bool, dict]:
    """T2 verify step: health 200 + /v1/models reachable (authenticated with
    INFRA_PROBE_VK when configured -- see bifrost/admin_api.list_models() for
    the 2026-09-25 re-measurement) + synthetic 1-token completion against the
    always-active vllm-local lane (parking/unparking a cloud provider must
    never be judged by that provider's own health — the local lane is the
    invariant gateway-came-back-up signal, and the synthetic completion is
    the decisive check: an authenticated request that actually completes
    end-to-end, which a models listing cannot prove by itself)."""
    detail: dict[str, Any] = {}
    ok_health, health_detail = admin_api.health(settings.infractl_probe_base)
    detail["health"] = health_detail
    if not ok_health:
        return False, detail

    ok_reachable, _count, models_detail = admin_api.list_models(
        settings.infractl_probe_base, vk=settings.infra_probe_vk or None, timeout_s=5.0
    )
    detail["models"] = models_detail
    if not ok_reachable:
        return False, detail

    if settings.infra_probe_vk:
        ok_completion, completion_detail = admin_api.synthetic_completion(
            settings.infractl_probe_base, settings.infra_probe_vk
        )
        detail["synthetic_completion"] = completion_detail
        if not ok_completion:
            return False, detail
    else:
        detail["synthetic_completion"] = "skipped: INFRA_PROBE_VK not set"

    return True, detail


@dataclasses.dataclass
class ConfigPlan:
    """The pure, validated result of a config-mutating kind: the new file
    contents (written in this order -- disabled-providers.json before
    config.json, so a parked recipe is never lost), plus a structured diff."""
    files: dict[str, dict]
    summary: str
    diff: dict
    noop: bool = False


def _read_pair(bifrost_dir: Path) -> tuple[dict, dict]:
    cfg = bifrost_config.read_config(bifrost_dir / "config.json")
    disabled_path = bifrost_dir / "disabled-providers.json"
    disabled = bifrost_config.read_config(disabled_path) if disabled_path.exists() else {"providers": {}}
    return cfg, disabled


def _plan_config_mutation(kind: str, bifrost_dir: Path, payload: dict, requested_by: str) -> ConfigPlan:
    """Compute (never write) the new config files for a T2 config kind.
    Every failure is an ActionError('invalid_request') raised BEFORE the
    snapshot/write/restart ladder starts, so a bad request never restarts
    the gateway. The operator-disabled check uses sync_vk_allowlists.py's own
    `operator_violations()` (the sync refuses the same config anyway; this
    turns a restart + rollback into a clean 400)."""
    cfg, disabled = _read_pair(bifrost_dir)
    try:
        if kind == "bifrost_models_apply":
            changes = payload.get("changes")
            new_cfg, diff = bifrost_config.apply_model_changes(cfg, changes)
            parts = []
            for provider, entry in diff.items():
                if entry["added"]:
                    parts.append(f"{provider} +{entry['added']}")
                if entry["removed"]:
                    parts.append(f"{provider} -{entry['removed']}")
            plan = ConfigPlan({"config.json": new_cfg}, "models: " + ("; ".join(parts) or "no change"),
                              diff, noop=bifrost_config.model_changes_are_noop(diff))
        elif kind == "bifrost_provider_park":
            provider = payload["provider"]
            new_cfg, new_disabled = park.plan_park(cfg, disabled, provider, payload.get("reason", ""), requested_by)
            plan = ConfigPlan({"disabled-providers.json": new_disabled, "config.json": new_cfg},
                              f"parked {provider}", {"parked": provider})
        elif kind == "provider_unpark":
            provider = payload["provider"]
            new_cfg, new_disabled = park.plan_unpark(cfg, disabled, provider)
            plan = ConfigPlan({"disabled-providers.json": new_disabled, "config.json": new_cfg},
                              f"unparked {provider}", {"unparked": provider})
        elif kind in ("models_add", "models_remove", "alias_set"):
            provider, key_name = payload["provider"], payload["key_name"]
            if kind == "models_add":
                new_cfg = bifrost_config.add_models(cfg, provider, key_name, payload["models"])
                summary = f"added {payload['models']} to {provider}/{key_name}"
            elif kind == "models_remove":
                new_cfg = bifrost_config.remove_models(cfg, provider, key_name, payload["models"])
                summary = f"removed {payload['models']} from {provider}/{key_name}"
            else:
                new_cfg = bifrost_config.set_alias(cfg, provider, key_name, payload["alias"], payload["target"])
                summary = f"set alias {payload['alias']}->{payload['target']} on {provider}/{key_name}"
            plan = ConfigPlan({"config.json": new_cfg}, summary, {"provider": provider, "key_name": key_name})
        else:
            raise ActionError(f"kind '{kind}' has no config mutation handler", "not_implemented")
    except (bifrost_config.ConfigError, park.ParkError, KeyError, TypeError) as exc:
        raise ActionError(f"{kind}: invalid request: {exc}", "invalid_request") from exc

    try:
        violations = bifrost_config.operator_violations(plan.files["config.json"], bifrost_dir)
    except bifrost_config.ConfigError as exc:
        raise ActionError(f"{kind}: cannot check operator-disabled list: {exc}", "invalid_request") from exc
    if violations:
        raise ActionError(
            f"{kind}: resulting config.json violates bifrost/operator-disabled.json: {violations[:10]}",
            "operator_disabled",
        )
    return plan


def _unified_diff(bifrost_dir: Path, plan: ConfigPlan, max_lines: int = 400) -> str:
    out: list[str] = []
    for name, new in plan.files.items():
        path = bifrost_dir / name
        old_text = json.dumps(bifrost_config.read_config(path), indent=2) if path.exists() else ""
        new_text = json.dumps(new, indent=2)
        out += difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                    f"a/bifrost/{name}", f"b/bifrost/{name}", n=2, lineterm="")
    if len(out) > max_lines:
        out = out[:max_lines] + [f"... ({len(out) - max_lines} more lines)"]
    return "\n".join(out)


def _write_plan(bifrost_dir: Path, plan: ConfigPlan) -> None:
    for name, data in plan.files.items():
        bifrost_config.atomic_write_json(bifrost_dir / name, data)


def _restart(settings: Settings, requested_by: str) -> dict:
    return restart.restart_bifrost(
        settings.infractl_bifrost_dir, probe_base=settings.infractl_probe_base,
        probe_vk=settings.infra_probe_vk, stop_autoheal=requested_by != AUTOHEAL_REQUESTER,
    )


def _refresh_redacted_snapshot(bifrost_dir: Path) -> str:
    """Keep bifrost/config.snapshot.redacted.json (git-tracked, no key
    material) in step with what infractl just wrote, so the host-side
    config_autocommit commits the matching config.db view with it."""
    try:
        return str(bifrost_config.write_redacted_snapshot(bifrost_dir))
    except Exception as exc:  # noqa: BLE001 -- reported in the ledger, never fails a verified action
        return f"redacted snapshot not refreshed: {exc}"


def _run_t1(kind: str, settings: Settings, payload: dict) -> dict:
    if kind == "restart_sidecar":
        container = payload["container"]
        if container not in NON_GATEWAY_SIDECARS:
            raise ActionError(f"'{container}' is not an allowlisted sidecar", "not_allowlisted")
        docker_client.restart(container)
        return {"restarted": container}

    if kind == "wal_checkpoint":
        return restart.preflight_check_wal(settings.infractl_bifrost_dir, warn_mb=0.0)

    if kind == "logsdb_quick_check":
        exit_code, out = docker_client.exec_run(
            "bifrost-logs-pruner",
            ["python", "-c",
             "import pruner; ok = pruner._integrity_check(); print('QUICK_CHECK_OK' if ok else 'QUICK_CHECK_FAILED')"],
            timeout_s=180,
        )
        return {"exit_code": exit_code, "output": out.strip()[-500:]}

    if kind == "probe_lanes_regenerate":
        from infractl.probes.lanes import regenerate_probe_lanes
        return regenerate_probe_lanes(settings)

    raise ActionError(f"T1 kind '{kind}' has no run handler", "not_implemented")


def _run_t3(kind: str, settings: Settings, payload: dict) -> dict:
    if kind == "restart_qwen38_chat":
        docker_client.restart("qwen38-chat")
        return {"restarted": "qwen38-chat"}
    if kind == "restart_vllm_embed":
        docker_client.restart("vllm-embed")
        return {"restarted": "vllm-embed"}
    if kind == "disk_prune":
        from infractl.bifrost import disk_prune as disk_prune_mod
        return disk_prune_mod.prune(settings)
    raise ActionError(f"T3 kind '{kind}' has no run handler", "not_implemented")


async def execute(ledger: ledger_mod.Ledger, settings: Settings, kind: str, payload: dict,
                   requested_by: str, reason: str, legion_ref: str | None = None,
                   confirm: bool = False, dry_run: bool | None = None,
                   action_id: str | None = None) -> dict:
    """`action_id=None` (the normal path): validate, gate on cooldown/
    max-per-day, create the ledger row, and either execute immediately (T1/
    T2, or T3 with confirm=True) or — a T3 kind requesting approval — stop
    after creating a `pending_approval` row and return its id.
    `action_id=<existing id>` (used only by `approve()` below): re-enter with
    a pre-existing pending-approval row instead of creating a new one, so
    the SAME ledger row that was proposed is the one that gets executed —
    an operator approving action X must see X's own outcome, not a sibling
    row's."""
    spec = REGISTRY.get(kind)
    if spec is None:
        raise ActionError(f"unknown action kind '{kind}'", "unknown_kind")

    if action_id is None:
        last_ts = ledger.last_heal_event_ts(kind)
        if last_ts is not None and (time.time() - last_ts) < spec.cooldown_s:
            raise ActionError(
                f"'{kind}' is in cooldown ({spec.cooldown_s}s); last ran "
                f"{time.time() - last_ts:.0f}s ago", "cooldown",
            )
        today_count = ledger.heal_events_today(kind)
        if today_count >= spec.max_per_day:
            raise ActionError(f"'{kind}' hit its daily cap ({spec.max_per_day})", "max_per_day")

        effective_dry_run = settings.dry_run if dry_run is None else dry_run
        action_id = ledger.create_action(kind, spec.tier, payload, requested_by, reason,
                                          legion_ref=legion_ref, dry_run=effective_dry_run)

        if spec.tier == "T3" and not confirm:
            ledger.update_action(action_id, status="pending_approval")
            return {
                "action_id": action_id, "status": "pending_approval",
                "note": f"POST /api/actions/{action_id}/approve to execute",
            }
        ledger.update_action(action_id, status="running", started_at=time.time())
    else:
        effective_dry_run = settings.dry_run if dry_run is None else dry_run
        ledger.update_action(action_id, status="running", started_at=time.time())

    try:
        async with acquire_write_lock(settings.infractl_lock_timeout_s):
            # The ladder is blocking (docker socket, subprocess sync, health
            # polling for up to minutes): run it off the event loop so /healthz
            # and read-only routes keep answering during a gateway restart.
            result = await asyncio.to_thread(
                _execute_locked, ledger, settings, action_id, spec, kind, payload,
                effective_dry_run, requested_by, legion_ref,
            )
        ledger.record_heal_event(kind, probe_name="api", outcome="dry_run" if effective_dry_run else "executed",
                                  detail=str(result)[:500], action_kind=kind, action_id=action_id)
        return result
    except Exception as exc:  # noqa: BLE001 — finalize the ledger row either way
        # `_execute_locked`'s own T2 rollback branch already sets a
        # specific terminal status ("rolled_back") and re-raises so its
        # caller still sees a real exception. Found live 2026-09-15 during
        # the Wave 1 forced-failure proof: this handler unconditionally
        # overwrote that back to a generic "failed", so a successfully
        # rolled-back action's ledger row lied about what actually
        # happened. Only stamp "failed" here when `_execute_locked` never
        # got the chance to finalize the row itself (e.g. the drift guard
        # or the lock timeout firing before its own try/except starts) —
        # i.e. the row is still "running".
        current = ledger.get_action(action_id)
        if current is not None and current["status"] == "running":
            ledger.update_action(action_id, status="failed", error=str(exc)[:2000], finished_at=time.time())
        discord.post(embed=discord.build_embed(
            f"infractl action failed: {kind}", str(exc)[:1500], level="error",
            fields={"action_id": action_id, "requested_by": requested_by},
        ))
        raise


async def approve(ledger: ledger_mod.Ledger, settings: Settings, action_id: str) -> dict:
    """POST /api/actions/{id}/approve — the only way a T3 action ever runs.
    Re-enters `execute()` with the SAME action_id and `confirm=True`."""
    row = ledger.get_action(action_id)
    if row is None:
        raise ActionError(f"action '{action_id}' not found", "not_found")
    if row["tier"] != "T3":
        raise ActionError(f"action '{action_id}' is tier {row['tier']}, not T3 — nothing to approve",
                           "invalid_state")
    if row["status"] != "pending_approval":
        raise ActionError(f"action '{action_id}' has status '{row['status']}', not pending_approval",
                           "invalid_state")
    return await execute(
        ledger, settings, row["kind"], json.loads(row["payload_json"]), row["requested_by"],
        row["reason"], legion_ref=row["legion_ref"], confirm=True,
        dry_run=bool(row["dry_run"]), action_id=action_id,
    )


async def manual_rollback(ledger: ledger_mod.Ledger, settings: Settings, action_id: str) -> dict:
    """POST /api/actions/{id}/rollback -- operator-triggered rollback of a
    previously SUCCEEDED T2 action back to its own pre-action snapshot, via
    the same restore + binding restart + verify the automatic rollback uses."""
    row = ledger.get_action(action_id)
    if row is None:
        raise ActionError(f"action '{action_id}' not found", "not_found")
    if row["status"] != "succeeded":
        raise ActionError(
            f"action '{action_id}' has status '{row['status']}' -- only a succeeded "
            f"action has a snapshot worth rolling back to", "invalid_state",
        )
    snapshot_dir = row.get("snapshot_dir")
    if not snapshot_dir:
        raise ActionError(f"action '{action_id}' has no snapshot_dir recorded", "no_snapshot")

    async with acquire_write_lock(settings.infractl_lock_timeout_s):
        outcome = await asyncio.to_thread(_rollback_to, settings, Path(snapshot_dir), "operator")
        ledger.update_action(
            action_id, status="rolled_back_manual", post_config_sha256=outcome.pop("post_sha"),
            verify_json=json.dumps(outcome, default=str)[:4000], finished_at=time.time(),
        )
    discord.post(embed=discord.build_embed(
        f"infractl: {row['kind']} manually rolled back", f"action_id={action_id}", level="warn",
        fields={"action_id": action_id, "restored": str(outcome["restored_files"])},
    ))
    return outcome


def _rollback_to(settings: Settings, snap_dir: Path, requested_by: str) -> dict:
    bifrost_dir = settings.infractl_bifrost_dir
    restored = _restore_config(bifrost_dir, snap_dir)
    rr = _restart(settings, requested_by)
    ok_after, verify_after = verify_gateway(settings) if rr["ok"] else (False, {"skipped": "restart failed"})
    return {
        "restored_files": restored, "rollback_restart_ok": rr["ok"], "rollback_steps": rr["steps"],
        "verify_after_rollback": verify_after, "verified_ok": ok_after,
        "redacted_snapshot": _refresh_redacted_snapshot(bifrost_dir),
        "post_sha": bifrost_config.sha256_of(bifrost_dir / "config.json"),
    }


def _execute_locked(ledger: ledger_mod.Ledger, settings: Settings, action_id: str,
                    spec: ActionSpec, kind: str, payload: dict, dry_run: bool,
                    requested_by: str, legion_ref: str | None = None) -> dict:
    bifrost_dir = settings.infractl_bifrost_dir

    if not spec.mutates_config:
        if dry_run and spec.tier != "T1":
            ledger.update_action(action_id, status="succeeded_dry_run", finished_at=time.time())
            return {"dry_run": True, "note": "would run", "kind": kind}
        if kind in ("bifrost_restart", "vk_resync"):
            result = _restart(settings, requested_by)
            if not result["ok"]:
                ledger.update_action(action_id, status="failed", error="binding restart failed",
                                     verify_json=json.dumps(result, default=str)[:4000], finished_at=time.time())
                raise ActionError(f"{kind}: binding restart failed: {result['steps'][-3:]}", "verify_failed")
        elif spec.tier == "T3":
            result = _run_t3(kind, settings, payload)
        else:
            result = _run_t1(kind, settings, payload)
        ledger.update_action(action_id, status="succeeded", verify_json=str(result)[:4000],
                             finished_at=time.time())
        return result

    # ---- T2 config-mutating ladder ----
    check_drift(window_s=settings.infractl_drift_window_s)
    plan = _plan_config_mutation(kind, bifrost_dir, payload, requested_by)
    if plan.noop:
        ledger.update_action(action_id, status="succeeded_noop", finished_at=time.time(),
                             verify_json=json.dumps({"summary": plan.summary, "diff": plan.diff})[:4000])
        return {"noop": True, "summary": plan.summary, "diff": plan.diff, "action_id": action_id}

    pre_sha = bifrost_config.sha256_of(bifrost_dir / "config.json")
    snap_dir = _snapshot_dir(settings.infractl_state_dir, action_id)
    snapshot_files = _snapshot_config(bifrost_dir, snap_dir)
    ledger.record_snapshot(action_id, str(snap_dir), snapshot_files)
    ledger.update_action(action_id, snapshot_dir=str(snap_dir), pre_config_sha256=pre_sha)

    if dry_run:
        text_diff = _unified_diff(bifrost_dir, plan)
        ledger.update_action(action_id, status="succeeded_dry_run", finished_at=time.time(),
                             verify_json=json.dumps({"summary": plan.summary, "diff": plan.diff})[:4000])
        return {"dry_run": True, "action_id": action_id, "summary": plan.summary, "diff": plan.diff,
                "unified_diff": text_diff}

    try:
        _write_plan(bifrost_dir, plan)
        rr = _restart(settings, requested_by)
        if not rr["ok"]:
            code = "sync_refused" if rr["sync_refused"] else "verify_failed"
            raise ActionError(f"binding restart failed after {kind}: {rr['sync_error'] or rr['steps'][-3:]}", code)
        ok, verify_detail = verify_gateway(settings)
        if not ok:
            raise ActionError(f"verify failed after {kind}: {verify_detail}", "verify_failed")
    except Exception as exc:  # noqa: BLE001 -- roll back on ANY failure in the write/restart/verify chain
        outcome = _rollback_to(settings, snap_dir, requested_by)
        ledger.update_action(
            action_id, status="rolled_back", post_config_sha256=outcome.pop("post_sha"),
            error=str(exc)[:2000], verify_json=json.dumps(outcome, default=str)[:4000], finished_at=time.time(),
        )
        discord.post(embed=discord.build_embed(
            f"infractl: {kind} ROLLED BACK", f"{exc}"[:1500], level="error",
            fields={"action_id": action_id, "restored": str(outcome["restored_files"]),
                    "gateway_verified_after_rollback": str(outcome["verified_ok"])},
        ))
        raise ActionError(f"{kind} failed and was rolled back: {exc}", "rolled_back") from exc

    post_sha = bifrost_config.sha256_of(bifrost_dir / "config.json")
    redacted = _refresh_redacted_snapshot(bifrost_dir)
    ledger.update_action(
        action_id, status="succeeded", post_config_sha256=post_sha, finished_at=time.time(),
        verify_json=json.dumps({"summary": plan.summary, "diff": plan.diff, "restart_steps": rr["steps"],
                                "verify": verify_detail, "redacted_snapshot": redacted}, default=str)[:4000],
    )
    if legion_ref:
        legion.post_feature_note(
            "product_feature:shared-infra", "design_decision",
            f"infractl action {kind} succeeded ({action_id})",
            f"payload={payload} verify={verify_detail}", source_ref=f"infractl:action:{action_id}",
        )
    discord.post(embed=discord.build_embed(
        f"infractl: {kind} succeeded", plan.summary[:1500], level="ok",
        fields={"action_id": action_id, "requested_by": requested_by},
    ))
    return {"action_id": action_id, "summary": plan.summary, "diff": plan.diff, "verify": verify_detail}
