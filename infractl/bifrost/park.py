"""Park / unpark a provider. Lifted from bifrost/auth_autoheal.py's
`_move_block_to_disabled()`/park sequence, generalized to also support the
reverse (unpark) which autoheal never needed. Snapshotting is handled by the
caller (core/actions.py) BEFORE these are invoked, per the action ladder
(snapshot -> apply -> vk_sync -> restart -> verify -> rollback-on-failure);
these functions only perform the file-level move + config.db deregister."""
from __future__ import annotations

import json
import time
from pathlib import Path

from infractl.bifrost import config as bifrost_config
from infractl.bifrost import configdb


class ParkError(RuntimeError):
    pass


PROTECTED_PROVIDERS = {"vllm-local", "embed-local"}


def park_provider_files(bifrost_dir: Path, provider: str, reason: str) -> None:
    if provider in PROTECTED_PROVIDERS:
        raise ParkError(f"{provider} is a protected local lane, never parked")
    config_json = bifrost_dir / "config.json"
    disabled_json = bifrost_dir / "disabled-providers.json"

    cfg = bifrost_config.read_config(config_json)
    disabled = bifrost_config.read_config(disabled_json) if disabled_json.exists() else {"providers": {}}

    new_cfg, new_disabled = bifrost_config.park_provider(cfg, disabled, provider)

    note = f"parked {provider} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} by infractl: {reason}"
    block = new_disabled["providers"][provider]
    if isinstance(block.get("_comment"), str):
        block["_prev_comment"] = block["_comment"]
    block["_comment"] = note

    # write disabled first (recipe never lost), then config — matches
    # auth_autoheal.py's ordering.
    bifrost_config.atomic_write_json(disabled_json, new_disabled)
    bifrost_config.atomic_write_json(config_json, new_cfg)


def unpark_provider_files(bifrost_dir: Path, provider: str) -> None:
    config_json = bifrost_dir / "config.json"
    disabled_json = bifrost_dir / "disabled-providers.json"

    cfg = bifrost_config.read_config(config_json)
    disabled = bifrost_config.read_config(disabled_json) if disabled_json.exists() else {"providers": {}}

    new_cfg, new_disabled = bifrost_config.unpark_provider(cfg, disabled, provider)
    block = new_cfg["providers"][provider]
    block.pop("_comment", None)
    if isinstance(block.get("_prev_comment"), str):
        block["_comment"] = block.pop("_prev_comment")

    bifrost_config.atomic_write_json(disabled_json, new_disabled)
    bifrost_config.atomic_write_json(config_json, new_cfg)


def deregister_from_db(config_db_path: Path, provider: str, purge_pricing: bool = False) -> dict:
    return configdb.deregister_provider(config_db_path, provider, purge_pricing=purge_pricing)
