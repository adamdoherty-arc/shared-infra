"""Load / validate / lint bifrost/config.json. Pure-function edits (park,
unpark, add/remove models, set alias) that take a dict and return a new
dict — no I/O — so they're trivially unit-testable; the only I/O is
`read_config()`/`atomic_write_config()` and the snapshot export, which
reuses `scripts/export_config_snapshot.py` (vendored into the image at
build time, see infractl/Dockerfile) rather than re-implementing its
redaction logic.

DESIGN DECISION — alias-in-models invariant (filed to Legion sprint 14975):
`sync_vk_allowlists.py`'s module docstring (2026-09-15 entry) documents that
Bifrost v2 evaluates `key.Models.IsAllowed(<requested>)` BEFORE alias
resolution, so an alias name absent from `models` silently 403s even when
governance allows it — every alias-based caller was failing for exactly
this reason until the VK-sync step started unioning models+aliases into
config.db's mirror. infractl's `set_alias()` closes the gap one layer
earlier, in config.json itself: setting an alias always ensures the alias
name is also present in the owning key's `models` list, so the sync step's
union becomes a redundant safety net instead of the only thing standing
between an alias and a silent 403. `add_models()`/`remove_models()` do NOT
touch aliases; `remove_models()` refuses to remove a model name that is
also an alias key's target, since that would leave a dangling alias.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

_VENDORED_SCRIPTS_DIR = os.environ.get("INFRACTL_VENDORED_SCRIPTS_DIR", "/app/vendor/scripts")


class ConfigError(ValueError):
    pass


def read_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def atomic_write_json(path: Path, data: dict) -> None:
    """temp-file + os.replace so a crash mid-write never leaves a truncated
    config.json for Bifrost to read on its next boot.

    `ensure_ascii=True` (json.dumps's own default — NOT passed explicitly
    as False) is load-bearing, not cosmetic: found live 2026-09-15 running
    the Wave 1 park/unpark write-path proof against the real config.json,
    whose `_comment_list_models` field contains an em dash written as the
    escape sequence `\\u2014`. `json.load()` decodes that escape into the
    actual Unicode character in memory; re-serializing with
    `ensure_ascii=False` then writes the raw UTF-8 character back out
    instead of re-escaping it, so a park immediately followed by an unpark
    — net content identical — left a 1-line diff and a different sha256
    purely from the encoding style changing. `ensure_ascii=True` re-escapes
    on every write, matching whatever encoding style produced the file
    originally, so round trips that touch a provider containing non-ASCII
    text land byte-identical to HEAD, not just JSON-equivalent to it."""
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    text = json.dumps(data, indent=2) + "\n"

    # Preserve the EXISTING file's line-ending convention rather than
    # forcing LF. Found live 2026-09-15, immediately after the
    # ensure_ascii fix above: this repo checks out on Windows with
    # `core.autocrlf=true`, so the tracked blob is LF but the working-tree
    # file is CRLF; this process runs inside a Linux container where
    # os.linesep is always "\n" and gives no signal either way. Writing
    # pure LF made `git diff` show zero content change (git's own compare
    # already normalizes line endings) while `git status --porcelain`
    # still reported the file modified, purely from the line-ending flip
    # — a semantically no-op park+unpark round trip must leave `git
    # status` clean, not just `git diff` clean, so the convention is
    # sniffed from the file being replaced (CRLF if it contains any
    # "\r\n"; LF otherwise, e.g. a brand-new file in a test fixture) and
    # applied to the new content before it's written.
    newline = "\r\n" if path.exists() and b"\r\n" in path.read_bytes() else "\n"
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def lint(cfg: dict) -> list[str]:
    """Pure validation — returns a list of problems (empty = clean). Never
    raises; callers decide whether findings are fatal."""
    problems: list[str] = []
    providers = cfg.get("providers")
    if not isinstance(providers, dict) or not providers:
        problems.append("providers block missing or empty")
        return problems
    for name, block in providers.items():
        keys = block.get("keys", [])
        if not keys:
            problems.append(f"{name}: no keys configured")
            continue
        for key in keys:
            key_name = key.get("name", "<unnamed>")
            models = key.get("models", [])
            aliases = key.get("aliases") or {}
            if not models and not aliases:
                problems.append(f"{name}/{key_name}: no models and no aliases")
            for alias in aliases:
                if alias not in models:
                    problems.append(
                        f"{name}/{key_name}: alias '{alias}' not in models "
                        f"(will 403 until vk_sync unions it — see alias-in-models invariant)"
                    )
    return problems


# ---- pure-function edits ----

def park_provider(cfg: dict, disabled_cfg: dict, provider: str) -> tuple[dict, dict]:
    """Move providers[provider] -> disabled_cfg['providers'][provider].
    Returns (new_cfg, new_disabled_cfg). Raises ConfigError if the provider
    doesn't exist or is a protected local lane."""
    protected = {"vllm-local", "embed-local"}
    if provider in protected:
        raise ConfigError(f"{provider} is a protected local lane, never parked")
    cfg = copy.deepcopy(cfg)
    disabled_cfg = copy.deepcopy(disabled_cfg) if disabled_cfg else {"providers": {}}
    disabled_cfg.setdefault("providers", {})
    providers = cfg.get("providers", {})
    if provider not in providers:
        raise ConfigError(f"provider '{provider}' not active in config.json")
    block = providers.pop(provider)
    disabled_cfg["providers"][provider] = block
    return cfg, disabled_cfg


def unpark_provider(cfg: dict, disabled_cfg: dict, provider: str) -> tuple[dict, dict]:
    cfg = copy.deepcopy(cfg)
    disabled_cfg = copy.deepcopy(disabled_cfg) if disabled_cfg else {"providers": {}}
    disabled = disabled_cfg.get("providers", {})
    if provider not in disabled:
        raise ConfigError(f"provider '{provider}' not in disabled-providers.json")
    if provider in cfg.get("providers", {}):
        raise ConfigError(f"provider '{provider}' is already active")
    block = disabled.pop(provider)
    cfg.setdefault("providers", {})[provider] = block
    return cfg, disabled_cfg


def add_models(cfg: dict, provider: str, key_name: str, models: list[str]) -> dict:
    cfg = copy.deepcopy(cfg)
    key = _find_key(cfg, provider, key_name)
    existing = key.setdefault("models", [])
    for m in models:
        if m not in existing:
            existing.append(m)
    return cfg


def remove_models(cfg: dict, provider: str, key_name: str, models: list[str]) -> dict:
    cfg = copy.deepcopy(cfg)
    key = _find_key(cfg, provider, key_name)
    aliases = key.get("aliases") or {}
    for m in models:
        if m in aliases.values():
            targets = [a for a, t in aliases.items() if t == m]
            raise ConfigError(
                f"cannot remove model '{m}': aliased by {targets} — remove those aliases first"
            )
    key["models"] = [m for m in key.get("models", []) if m not in models]
    return cfg


def set_alias(cfg: dict, provider: str, key_name: str, alias: str, target: str) -> dict:
    cfg = copy.deepcopy(cfg)
    key = _find_key(cfg, provider, key_name)
    aliases = key.setdefault("aliases", {})
    aliases[alias] = target
    models = key.setdefault("models", [])
    if alias not in models:  # alias-in-models invariant, see module docstring
        models.append(alias)
    return cfg


def _find_key(cfg: dict, provider: str, key_name: str) -> dict:
    block = cfg.get("providers", {}).get(provider)
    if block is None:
        raise ConfigError(f"provider '{provider}' not found")
    for key in block.get("keys", []):
        if key.get("name") == key_name:
            return key
    raise ConfigError(f"key '{key_name}' not found under provider '{provider}'")


# ---- redacted snapshot (delegates to scripts/export_config_snapshot.py) ----

def _load_vendored_snapshot_module():
    if _VENDORED_SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _VENDORED_SCRIPTS_DIR)
    import export_config_snapshot as _snap  # type: ignore
    return _snap


def render_redacted_snapshot(bifrost_dir: Path) -> dict:
    """Reuses export_config_snapshot.py's `_key_ref`/`render()` verbatim —
    repoints its module-level path constants at infractl's mounted bifrost
    dir instead of duplicating the redaction logic."""
    snap_mod = _load_vendored_snapshot_module()
    snap_mod.CONFIG_JSON = bifrost_dir / "config.json"
    snap_mod.CONFIG_DB = bifrost_dir / "config.db"
    snap_mod.DISABLED_JSON = bifrost_dir / "disabled-providers.json"
    snap_mod.BIFROST = bifrost_dir
    return snap_mod.render()


def write_redacted_snapshot(bifrost_dir: Path) -> Path:
    snap_mod = _load_vendored_snapshot_module()
    snap = render_redacted_snapshot(bifrost_dir)
    out = bifrost_dir / "config.snapshot.redacted.json"
    text = json.dumps(snap, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out
