from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from infractl import consumers as consumers_pkg

REAL_CONSUMERS_YAML = Path(__file__).resolve().parents[1] / "consumers" / "consumers.yaml"


def test_real_consumers_yaml_is_valid():
    data = consumers_pkg.load_raw(REAL_CONSUMERS_YAML)
    problems = consumers_pkg.validate(data)
    assert problems == [], f"consumers.yaml has schema problems: {problems}"


def test_real_consumers_yaml_has_no_raw_secrets():
    text = REAL_CONSUMERS_YAML.read_text(encoding="utf-8")
    assert "sk-bf-" not in text
    for prefix in ("nvapi-", "gsk_", "sk-or-", "AIza", "AQ."):
        assert prefix not in text, f"possible raw secret prefix '{prefix}' found in consumers.yaml"


def test_validate_rejects_missing_required_fields():
    data = {"version": 1, "consumers": [{"name": "x"}]}
    problems = consumers_pkg.validate(data)
    assert problems
    assert any("missing fields" in p for p in problems)


def test_validate_rejects_duplicate_names():
    base_consumer = {
        "name": "dup", "vk_name": "v", "vk_id": "id1", "sha256_prefix": "a" * 16,
        "repo": None, "legion_project_id": None, "env_var": None, "containers": [],
        "health_url": None, "daily_request_budget": 1, "per_minute_cap": 1,
        "vllm_local_share": 0.0, "notes": "",
    }
    other = {**base_consumer, "vk_id": "id2"}
    data = {"version": 1, "consumers": [base_consumer, other]}
    problems = consumers_pkg.validate(data)
    assert any("duplicate consumer name" in p for p in problems)


def test_validate_rejects_duplicate_vk_ids():
    consumer_a = {
        "name": "a", "vk_name": "v", "vk_id": "same-id", "sha256_prefix": "a" * 16,
        "repo": None, "legion_project_id": None, "env_var": None, "containers": [],
        "health_url": None, "daily_request_budget": 1, "per_minute_cap": 1,
        "vllm_local_share": 0.0, "notes": "",
    }
    consumer_b = {**consumer_a, "name": "b"}
    data = {"version": 1, "consumers": [consumer_a, consumer_b]}
    problems = consumers_pkg.validate(data)
    assert any("duplicate vk_id" in p for p in problems)


def test_validate_rejects_bad_sha256_prefix():
    consumer = {
        "name": "a", "vk_name": "v", "vk_id": "id1", "sha256_prefix": "not-hex!!",
        "repo": None, "legion_project_id": None, "env_var": None, "containers": [],
        "health_url": None, "daily_request_budget": 1, "per_minute_cap": 1,
        "vllm_local_share": 0.0, "notes": "",
    }
    problems = consumers_pkg.validate({"version": 1, "consumers": [consumer]})
    assert any("sha256_prefix" in p for p in problems)


def test_validate_rejects_empty_consumers_list():
    problems = consumers_pkg.validate({"version": 1, "consumers": []})
    assert problems


def test_list_consumers_returns_real_data(settings):
    import shutil
    shutil.copy2(REAL_CONSUMERS_YAML, settings.infractl_consumers_yaml)
    consumers = consumers_pkg.list_consumers(settings)
    names = {c["name"] for c in consumers}
    assert "ada" in names
    assert "claude-code-local" in names
