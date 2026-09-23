"""Loader for infractl/consumers/consumers.yaml — the source of truth for
`/api/consumers`, `docs/CONSUMERS.md` (rendered by `infractl docs render`),
and the consumers health probe. VK ids and sha256 prefixes only, never a raw
key value (none is ever stored in this file to begin with)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from infractl.settings import Settings

REQUIRED_CONSUMER_FIELDS = {
    "name", "vk_name", "vk_id", "sha256_prefix", "repo", "legion_project_id",
    "env_var", "containers", "health_url", "daily_request_budget",
    "per_minute_cap", "vllm_local_share", "notes",
}


class ConsumersSchemaError(ValueError):
    pass


def load_raw(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConsumersSchemaError(f"{path}: top level must be a mapping")
    return data


def validate(data: dict) -> list[str]:
    """Pure validation — returns a list of problems (empty = clean)."""
    problems: list[str] = []
    if "version" not in data:
        problems.append("missing top-level 'version'")
    consumers = data.get("consumers")
    if not isinstance(consumers, list) or not consumers:
        problems.append("'consumers' must be a non-empty list")
        return problems
    seen_names = set()
    seen_vk_ids = set()
    for i, c in enumerate(consumers):
        if not isinstance(c, dict):
            problems.append(f"consumers[{i}] is not a mapping")
            continue
        missing = REQUIRED_CONSUMER_FIELDS - c.keys()
        if missing:
            problems.append(f"consumers[{i}] ({c.get('name', '?')}) missing fields: {sorted(missing)}")
        name = c.get("name")
        if name in seen_names:
            problems.append(f"duplicate consumer name '{name}'")
        seen_names.add(name)
        vk_id = c.get("vk_id")
        if vk_id in seen_vk_ids:
            problems.append(f"duplicate vk_id '{vk_id}' (consumer '{name}')")
        seen_vk_ids.add(vk_id)
        prefix = c.get("sha256_prefix", "")
        if prefix and (len(prefix) != 16 or any(ch not in "0123456789abcdef" for ch in prefix)):
            problems.append(f"consumers[{i}] ({name}): sha256_prefix must be 16 lowercase hex chars")
    return problems


def list_consumers(settings: Settings) -> list[dict[str, Any]]:
    data = load_raw(settings.infractl_consumers_yaml)
    problems = validate(data)
    if problems:
        raise ConsumersSchemaError("; ".join(problems))
    return data["consumers"]


def load(settings: Settings) -> dict[str, Any]:
    data = load_raw(settings.infractl_consumers_yaml)
    problems = validate(data)
    if problems:
        raise ConsumersSchemaError("; ".join(problems))
    return data
