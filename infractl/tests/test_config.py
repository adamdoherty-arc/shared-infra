from __future__ import annotations

import json

import pytest

from infractl.bifrost import config as bifrost_config


def _base_cfg():
    return {
        "providers": {
            "vllm-local": {"keys": [{"name": "k1", "models": ["qwen3-chat"]}]},
            "mistral": {"keys": [{"name": "k1", "models": ["mistral-large"]}]},
        }
    }


def test_lint_clean_config():
    assert bifrost_config.lint(_base_cfg()) == []


def test_lint_flags_empty_providers():
    problems = bifrost_config.lint({"providers": {}})
    assert any("empty" in p for p in problems)


def test_lint_flags_key_with_no_models():
    cfg = {"providers": {"p": {"keys": [{"name": "k1", "models": []}]}}}
    problems = bifrost_config.lint(cfg)
    assert any("no models" in p for p in problems)


def test_lint_flags_alias_not_in_models():
    cfg = {"providers": {"p": {"keys": [{"name": "k1", "models": ["m1"], "aliases": {"a1": "m1", "a2": "missing"}}]}}}
    problems = bifrost_config.lint(cfg)
    assert any("a2" in p and "not in models" in p for p in problems)


def test_park_and_unpark_round_trip():
    cfg = _base_cfg()
    disabled = {"providers": {}}
    new_cfg, new_disabled = bifrost_config.park_provider(cfg, disabled, "mistral")
    assert "mistral" not in new_cfg["providers"]
    assert "mistral" in new_disabled["providers"]

    restored_cfg, restored_disabled = bifrost_config.unpark_provider(new_cfg, new_disabled, "mistral")
    assert "mistral" in restored_cfg["providers"]
    assert "mistral" not in restored_disabled["providers"]
    # round trip is byte-identical to the original for the providers block
    assert restored_cfg["providers"]["mistral"] == cfg["providers"]["mistral"]


def test_park_protected_provider_raises():
    cfg = _base_cfg()
    with pytest.raises(bifrost_config.ConfigError):
        bifrost_config.park_provider(cfg, {"providers": {}}, "vllm-local")


def test_park_unknown_provider_raises():
    cfg = _base_cfg()
    with pytest.raises(bifrost_config.ConfigError):
        bifrost_config.park_provider(cfg, {"providers": {}}, "nonexistent")


def test_unpark_not_disabled_raises():
    cfg = _base_cfg()
    with pytest.raises(bifrost_config.ConfigError):
        bifrost_config.unpark_provider(cfg, {"providers": {}}, "mistral")


def test_add_and_remove_models():
    cfg = _base_cfg()
    cfg2 = bifrost_config.add_models(cfg, "mistral", "k1", ["mistral-medium"])
    assert "mistral-medium" in cfg2["providers"]["mistral"]["keys"][0]["models"]
    cfg3 = bifrost_config.remove_models(cfg2, "mistral", "k1", ["mistral-medium"])
    assert "mistral-medium" not in cfg3["providers"]["mistral"]["keys"][0]["models"]


def test_remove_models_refuses_when_aliased():
    cfg = _base_cfg()
    cfg2 = bifrost_config.set_alias(cfg, "mistral", "k1", "default", "mistral-large")
    with pytest.raises(bifrost_config.ConfigError):
        bifrost_config.remove_models(cfg2, "mistral", "k1", ["mistral-large"])


def test_alias_in_models_invariant():
    """DESIGN DECISION under test (Legion sprint 14975): set_alias() must
    proactively add the alias name into the key's `models` list, since
    Bifrost v2 checks key.Models.IsAllowed() BEFORE alias resolution — an
    alias absent from `models` silently 403s even when governance allows
    it. This is the single most important invariant in this module."""
    cfg = _base_cfg()
    cfg2 = bifrost_config.set_alias(cfg, "mistral", "k1", "default-model", "mistral-large")
    key = cfg2["providers"]["mistral"]["keys"][0]
    assert key["aliases"]["default-model"] == "mistral-large"
    assert "default-model" in key["models"], (
        "alias-in-models invariant violated: the alias name must be added to "
        "the key's models list or Bifrost v2 403s the alias before resolving it"
    )


def test_edits_are_pure_do_not_mutate_input():
    cfg = _base_cfg()
    original_providers = {**cfg["providers"]}
    bifrost_config.park_provider(cfg, {"providers": {}}, "mistral")
    assert cfg["providers"] == original_providers, "park_provider must not mutate its input dict"


def test_sha256_of_nonexistent_path_returns_empty(tmp_path):
    assert bifrost_config.sha256_of(tmp_path / "does-not-exist.json") == ""


def test_atomic_write_json_reencodes_unicode_as_escape_not_raw_utf8(tmp_path):
    """Live 2026-09-15 finding: a park immediately followed by an unpark
    left config.json non-byte-identical to HEAD because ensure_ascii=False
    re-serialized an existing `\\u2014` escape as a raw em dash character.
    Pinned here so the write-path proof's byte-identity requirement can't
    silently regress."""
    path = tmp_path / "config.json"
    original_bytes = json.dumps({"note": "— an em dash"}, indent=2).encode("utf-8") + b"\n"
    path.write_bytes(original_bytes)
    cfg = bifrost_config.read_config(path)
    bifrost_config.atomic_write_json(path, cfg)  # semantically-identical round trip
    assert path.read_bytes() == original_bytes, (
        "atomic_write_json must re-escape non-ASCII as \\uXXXX, matching json.dumps's "
        "own default, or a no-op round trip changes the file's bytes"
    )


def test_atomic_write_json_preserves_crlf_convention(tmp_path):
    """Live 2026-09-15 finding: this repo checks out with core.autocrlf=true
    (CRLF working tree, LF blob); writing pure LF made a semantically no-op
    round trip show up as `git status`-dirty purely from the line-ending
    flip, even though `git diff` (which normalizes) showed no change."""
    path = tmp_path / "config.json"
    original_bytes = b'{\r\n  "a": 1\r\n}\r\n'
    path.write_bytes(original_bytes)
    cfg = bifrost_config.read_config(path)
    bifrost_config.atomic_write_json(path, cfg)
    written = path.read_bytes()
    assert b"\r\n" in written
    assert written.count(b"\r\n") == written.count(b"\n"), "every LF must be paired with a CR"


def test_atomic_write_json_defaults_to_lf_for_new_file(tmp_path):
    path = tmp_path / "brand-new.json"
    bifrost_config.atomic_write_json(path, {"a": 1})
    written = path.read_bytes()
    assert b"\r\n" not in written


def test_atomic_write_json_round_trip(tmp_path):
    path = tmp_path / "config.json"
    bifrost_config.atomic_write_json(path, {"a": 1})
    assert bifrost_config.read_config(path) == {"a": 1}
    assert not (tmp_path / f"config.json.tmp-{__import__('os').getpid()}").exists()
