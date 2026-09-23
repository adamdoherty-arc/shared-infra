"""Action allowlist with tiers T0-T3, cooldown + max/day per kind, and the
single `execute()` entrypoint every mutating call goes through: lock ->
drift guard -> snapshot -> apply -> vk_sync -> restart ladder -> verify ->
rollback on failure -> ledger finalize -> Discord -> Legion note.

TIERS
  T0  read-only. No lock, not registered here (plain GET handlers).
  T1  auto, no approval, no config.json mutation: restart_sidecar,
      vk_resync, wal_checkpoint, logsdb_quick_check, probe_lanes_regenerate.
  T2  auto, WITH the full snapshot/verify/rollback ladder because they
      mutate config.json/config.db: provider_park, provider_unpark,
      models_add, models_remove, alias_set, bifrost_restart.
  T3  requires POST /api/actions/{id}/approve, NEVER auto-scheduled by heal
      rules: restart_qwen38_chat, restart_vllm_embed, disk_prune. Only
      kinds whose mechanics are implemented today are registered — per the
      NEVER SHIP STUBS rule, vk_budget_set/vk_rotate/compose_apply/model_swap
      are NOT registered until their real mechanics exist (registering an
      unimplemented kind so a client could pick it out of GET /api/actions
      would be exactly the disguised-stub pattern that rule bans).
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from infractl.bifrost import admin_api, config as bifrost_config, configdb, park, restart, vk_sync
from infractl.core import discord, ledger as ledger_mod, legion
from infractl.core.docker import DockerError
from infractl.core import docker as docker_client
from infractl.core.lock import WriterConflictError, acquire_write_lock, check_drift, current_config_mtime
from infractl.settings import Settings


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
    "vk_resync": ActionSpec("vk_resync", "T1", 60, 48, False, "re-run sync_vk_allowlists.py"),
    "wal_checkpoint": ActionSpec("wal_checkpoint", "T1", 300, 12, False, "checkpoint logs.db-wal in-container"),
    "logsdb_quick_check": ActionSpec("logsdb_quick_check", "T1", 1800, 6, False, "PRAGMA quick_check in-container"),
    "probe_lanes_regenerate": ActionSpec("probe_lanes_regenerate", "T1", 900, 24, False, "regenerate /state/probe_lanes.json"),
    "provider_park": ActionSpec("provider_park", "T2", 60, 10, True, "move a provider block to disabled-providers.json"),
    "provider_unpark": ActionSpec("provider_unpark", "T2", 60, 10, True, "restore a provider block from disabled-providers.json"),
    "models_add": ActionSpec("models_add", "T2", 30, 20, True, "add models to a provider key"),
    "models_remove": ActionSpec("models_remove", "T2", 30, 20, True, "remove models from a provider key"),
    "alias_set": ActionSpec("alias_set", "T2", 30, 20, True, "set an alias on a provider key"),
    "bifrost_restart": ActionSpec("bifrost_restart", "T2", 120, 12, False, "run the full restart ladder"),
    "restart_qwen38_chat": ActionSpec("restart_qwen38_chat", "T3", 300, 6, False, "restart the local chat vLLM engine"),
    "restart_vllm_embed": ActionSpec("restart_vllm_embed", "T3", 300, 6, False, "restart the local embed vLLM engine"),
    "disk_prune": ActionSpec("disk_prune", "T3", 3600, 4, False, "prune dangling docker volumes/images + old .bak files"),
}

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
    files = []
    for name in ("config.json", "disabled-providers.json", "config.db"):
        src = snapshot_dir / name
        if src.exists():
            shutil.copy2(src, bifrost_dir / name)
            files.append(name)
    return files


def verify_gateway(settings: Settings) -> tuple[bool, dict]:
    """T2 verify step: health 200 + /v1/models reachable (unauthenticated —
    see bifrost/admin_api.list_models()'s docstring for why an authenticated
    call is never used here, a real 2026-09-15 finding: it hangs 30s+ on
    this production instance) + synthetic 1-token completion against the
    always-active vllm-local lane (parking/unparking a cloud provider must
    never be judged by that provider's own health — the local lane is the
    invariant gateway-came-back-up signal, and the synthetic completion is
    the decisive check: an authenticated request that actually completes
    end-to-end, which the deliberately-unauthenticated /v1/models call
    above cannot prove by itself)."""
    detail: dict[str, Any] = {}
    ok_health, health_detail = admin_api.health(settings.infractl_probe_base)
    detail["health"] = health_detail
    if not ok_health:
        return False, detail

    ok_reachable, _count, models_detail = admin_api.list_models(
        settings.infractl_probe_base, vk=None, timeout_s=5.0
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


def _apply_config_mutation(kind: str, bifrost_dir: Path, payload: dict) -> str:
    """Runs the pure-function edit + atomic write for T2 config-mutating
    kinds. Returns a short human-readable summary."""
    config_json = bifrost_dir / "config.json"
    disabled_json = bifrost_dir / "disabled-providers.json"

    if kind == "provider_park":
        provider = payload["provider"]
        reason = payload.get("reason", "")
        park.park_provider_files(bifrost_dir, provider, reason)
        return f"parked {provider}"

    if kind == "provider_unpark":
        provider = payload["provider"]
        park.unpark_provider_files(bifrost_dir, provider)
        return f"unparked {provider}"

    if kind in ("models_add", "models_remove", "alias_set"):
        cfg = bifrost_config.read_config(config_json)
        provider = payload["provider"]
        key_name = payload["key_name"]
        if kind == "models_add":
            new_cfg = bifrost_config.add_models(cfg, provider, key_name, payload["models"])
            summary = f"added {payload['models']} to {provider}/{key_name}"
        elif kind == "models_remove":
            new_cfg = bifrost_config.remove_models(cfg, provider, key_name, payload["models"])
            summary = f"removed {payload['models']} from {provider}/{key_name}"
        else:
            new_cfg = bifrost_config.set_alias(cfg, provider, key_name, payload["alias"], payload["target"])
            summary = f"set alias {payload['alias']}->{payload['target']} on {provider}/{key_name}"
        bifrost_config.atomic_write_json(config_json, new_cfg)
        return summary

    raise ActionError(f"kind '{kind}' has no config mutation handler", "not_implemented")


def _run_t1(kind: str, settings: Settings, payload: dict) -> dict:
    if kind == "restart_sidecar":
        container = payload["container"]
        if container not in NON_GATEWAY_SIDECARS:
            raise ActionError(f"'{container}' is not an allowlisted sidecar", "not_allowlisted")
        docker_client.restart(container)
        return {"restarted": container}

    if kind == "vk_resync":
        out = vk_sync.run_vk_sync(settings.infractl_bifrost_dir)
        return {"output_tail": out.strip().splitlines()[-5:] if out.strip() else []}

    if kind == "wal_checkpoint":
        return restart.preflight_check_wal(settings.infractl_bifrost_dir, warn_mb=0.0)

    if kind == "logsdb_quick_check":
        exit_code, out = docker_client.exec_run(
            "bifrost-logs-pruner",
            ["python", "-c", "import pruner; ok = pruner._integrity_check(); print('QUICK_CHECK_OK' if ok else 'QUICK_CHECK_FAILED')"],
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
            result = await _execute_locked(ledger, settings, action_id, spec, kind, payload,
                                            effective_dry_run, legion_ref)
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
    """POST /api/actions/{id}/rollback — operator-triggered rollback of a
    previously SUCCEEDED T2 action back to its own pre-action snapshot.
    Goes through the same lock + vk_sync + restart + verify sequence the
    automatic on-failure rollback inside `_execute_locked` uses; the only
    difference is the trigger (operator request vs. a failed verify)."""
    row = ledger.get_action(action_id)
    if row is None:
        raise ActionError(f"action '{action_id}' not found", "not_found")
    if row["status"] != "succeeded":
        raise ActionError(
            f"action '{action_id}' has status '{row['status']}' — only a succeeded "
            f"action has a snapshot worth rolling back to", "invalid_state",
        )
    snapshot_dir = row.get("snapshot_dir")
    if not snapshot_dir:
        raise ActionError(f"action '{action_id}' has no snapshot_dir recorded", "no_snapshot")

    bifrost_dir = settings.infractl_bifrost_dir
    async with acquire_write_lock(settings.infractl_lock_timeout_s):
        restored = _restore_config(bifrost_dir, Path(snapshot_dir))
        try:
            vk_sync.run_vk_sync(bifrost_dir)
        except vk_sync.VkSyncError:
            pass
        restart_result = restart.restart_bifrost(bifrost_dir, probe_base=settings.infractl_probe_base)
        ok_after, verify_after = verify_gateway(settings)
        post_sha = bifrost_config.sha256_of(bifrost_dir / "config.json")
        ledger.update_action(
            action_id, status="rolled_back_manual", post_config_sha256=post_sha,
            verify_json=str({"restored_files": restored, "rollback_healthy": restart_result["healthy"],
                              "verify_after_rollback": verify_after, "verified_ok": ok_after})[:4000],
            finished_at=time.time(),
        )
    discord.post(embed=discord.build_embed(
        f"infractl: {row['kind']} manually rolled back", f"action_id={action_id}", level="warn",
        fields={"action_id": action_id, "restored": str(restored)},
    ))
    return {"restored_files": restored, "verify_ok": ok_after, "verify": verify_after}


async def _execute_locked(ledger: ledger_mod.Ledger, settings: Settings, action_id: str,
                           spec: ActionSpec, kind: str, payload: dict, dry_run: bool,
                           legion_ref: str | None = None) -> dict:
    bifrost_dir = settings.infractl_bifrost_dir

    if not spec.mutates_config:
        # T1 or non-config-mutating T2 (bifrost_restart) / T3 — no drift
        # guard or snapshot needed beyond what the handler itself does.
        if dry_run and spec.tier != "T1":
            ledger.update_action(action_id, status="succeeded_dry_run", finished_at=time.time())
            return {"dry_run": True, "note": "would run", "kind": kind}
        if kind == "bifrost_restart":
            result = restart.restart_bifrost(bifrost_dir, probe_base=settings.infractl_probe_base)
        elif spec.tier == "T3":
            result = _run_t3(kind, settings, payload)
        else:
            result = _run_t1(kind, settings, payload)
        ledger.update_action(action_id, status="succeeded", verify_json=str(result)[:4000],
                              finished_at=time.time())
        return result

    # ---- T2 config-mutating ladder ----
    check_drift(window_s=settings.infractl_drift_window_s)

    pre_sha = bifrost_config.sha256_of(bifrost_dir / "config.json")
    snap_dir = _snapshot_dir(settings.infractl_state_dir, action_id)
    snapshot_files = _snapshot_config(bifrost_dir, snap_dir)
    ledger.record_snapshot(action_id, str(snap_dir), snapshot_files)
    ledger.update_action(action_id, snapshot_dir=str(snap_dir), pre_config_sha256=pre_sha)

    if dry_run:
        # Apply to a scratch copy only — same pure functions, same code
        # path, just no os.replace onto the live files (NOTHING IS MOCKED:
        # this is the real apply function, only the final write target
        # differs).
        scratch = snap_dir / "dry_run_scratch"
        scratch.mkdir(exist_ok=True)
        for name in ("config.json", "disabled-providers.json"):
            src = bifrost_dir / name
            if src.exists():
                shutil.copy2(src, scratch / name)
        try:
            summary = _apply_config_mutation(kind, scratch, payload)
        finally:
            pass
        ledger.update_action(action_id, status="succeeded_dry_run",
                              verify_json=f"dry_run apply ok: {summary}", finished_at=time.time())
        return {"dry_run": True, "summary": summary}

    try:
        summary = _apply_config_mutation(kind, bifrost_dir, payload)
        deregister_result = None
        if kind == "provider_park":
            docker_client.stop("shared-bifrost")
            try:
                deregister_result = park.deregister_from_db(bifrost_dir / "config.db", payload["provider"])
            finally:
                docker_client.start("shared-bifrost")
        restart_result = restart.restart_bifrost(bifrost_dir, probe_base=settings.infractl_probe_base)
        ok, verify_detail = verify_gateway(settings)
        if not ok:
            raise ActionError(f"verify failed after {kind}: {verify_detail}", "verify_failed")

        post_sha = bifrost_config.sha256_of(bifrost_dir / "config.json")
        ledger.update_action(
            action_id, status="succeeded", post_config_sha256=post_sha,
            verify_json=str({"summary": summary, "restart": restart_result["healthy"],
                              "verify": verify_detail, "deregister": deregister_result})[:4000],
            finished_at=time.time(),
        )
        if legion_ref:
            legion.post_feature_note(
                "product_feature:shared-infra", "design_decision",
                f"infractl action {kind} succeeded ({action_id})",
                f"payload={payload} verify={verify_detail}", source_ref=f"infractl:action:{action_id}",
            )
        discord.post(embed=discord.build_embed(
            f"infractl: {kind} succeeded", summary, level="ok",
            fields={"action_id": action_id},
        ))
        return {"summary": summary, "verify": verify_detail}

    except Exception as exc:  # noqa: BLE001 — roll back on ANY failure in the apply/verify chain
        restored = _restore_config(bifrost_dir, snap_dir)
        try:
            vk_sync.run_vk_sync(bifrost_dir)
        except vk_sync.VkSyncError:
            pass
        rollback_restart = restart.restart_bifrost(bifrost_dir, probe_base=settings.infractl_probe_base)
        ok_after, verify_after = verify_gateway(settings)
        post_sha = bifrost_config.sha256_of(bifrost_dir / "config.json")
        ledger.update_action(
            action_id, status="rolled_back", post_config_sha256=post_sha,
            error=str(exc)[:2000],
            verify_json=str({"restored_files": restored, "rollback_healthy": rollback_restart["healthy"],
                              "verify_after_rollback": verify_after, "verified_ok": ok_after})[:4000],
            finished_at=time.time(),
        )
        discord.post(embed=discord.build_embed(
            f"infractl: {kind} ROLLED BACK", f"{exc}", level="error",
            fields={"action_id": action_id, "restored": str(restored)},
        ))
        raise ActionError(f"{kind} failed and was rolled back: {exc}", "rolled_back") from exc
