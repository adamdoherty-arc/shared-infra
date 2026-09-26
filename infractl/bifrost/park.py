"""Park / unpark a provider as pure planning functions. Lifted from
bifrost/auth_autoheal.py's former `_move_block_to_disabled()` (that sidecar
now asks infractl to park instead of writing the files itself), generalized
to the reverse (unpark). Each function takes the current config.json +
disabled-providers.json dicts and returns the new pair; core/actions.py owns
the snapshot -> write -> binding restart -> verify -> rollback ladder, and
the config.db cleanup is done by sync_vk_allowlists.py's
`deregister_absent_providers()` inside that restart (it deletes config.db
rows for any provider absent from config.json, with Bifrost stopped)."""
from __future__ import annotations

import time

from infractl.bifrost import config as bifrost_config


class ParkError(RuntimeError):
    pass


PROTECTED_PROVIDERS = {"vllm-local", "embed-local"}


def plan_park(cfg: dict, disabled: dict, provider: str, reason: str,
              requested_by: str = "infractl") -> tuple[dict, dict]:
    if provider in PROTECTED_PROVIDERS:
        raise ParkError(f"{provider} is a protected local lane, never parked")
    try:
        new_cfg, new_disabled = bifrost_config.park_provider(cfg, disabled, provider)
    except bifrost_config.ConfigError as exc:
        raise ParkError(str(exc)) from exc
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    block = new_disabled["providers"][provider]
    if isinstance(block.get("_comment"), str):
        block["_prev_comment"] = block["_comment"]
    block["_comment"] = (
        f"parked {provider} {stamp} by {requested_by} via infractl: {reason}. TO RE-ENABLE: fix the "
        f"cause, then `infractl unpark {provider}` (restores this block, syncs config.db, restarts)."
    )
    return new_cfg, new_disabled


def plan_unpark(cfg: dict, disabled: dict, provider: str) -> tuple[dict, dict]:
    try:
        new_cfg, new_disabled = bifrost_config.unpark_provider(cfg, disabled, provider)
    except bifrost_config.ConfigError as exc:
        raise ParkError(str(exc)) from exc
    block = new_cfg["providers"][provider]
    block.pop("_comment", None)
    if isinstance(block.get("_prev_comment"), str):
        block["_comment"] = block.pop("_prev_comment")
    return new_cfg, new_disabled
