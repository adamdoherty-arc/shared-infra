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
import sys
from pathlib import Path

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


def apply_model_changes(cfg: dict, changes: dict) -> tuple[dict, dict]:
    """Provider-level model add/remove across EVERY key of the provider (the
    shape ADA's bifrost_model_sync.py always used: nvidia-nim's three keys
    share one model list). `changes` is `{provider: {"add": [...],
    "remove": [...]}}`. Returns (new_cfg, diff) where diff is
    `{provider: {"added", "removed", "already_present", "not_present"}}` --
    no-op entries are reported, not raised, so an automated caller racing a
    human edit gets a truthful diff instead of an error. Raises ConfigError
    for a malformed request, a provider that is not active, a model named in
    both add and remove, or a removal that would orphan an alias."""
    if not isinstance(changes, dict) or not changes:
        raise ConfigError("changes must be a non-empty {provider: {add: [...], remove: [...]}} object")
    cfg = copy.deepcopy(cfg)
    providers = cfg.get("providers", {})
    diff: dict[str, dict[str, list[str]]] = {}
    for provider, change in changes.items():
        if provider not in providers:
            raise ConfigError(f"provider '{provider}' is not active in config.json")
        if not isinstance(change, dict) or set(change) - {"add", "remove", "cascade_aliases"}:
            raise ConfigError(
                f"{provider}: change must be an object with only 'add'/'remove' lists "
                "and an optional boolean 'cascade_aliases'"
            )
        cascade = bool(change.get("cascade_aliases", False))
        add = change.get("add") or []
        remove = change.get("remove") or []
        for name, lst in (("add", add), ("remove", remove)):
            if not isinstance(lst, list) or not all(isinstance(m, str) and m.strip() for m in lst):
                raise ConfigError(f"{provider}.{name} must be a list of non-empty model id strings")
        both = sorted(set(add) & set(remove))
        if both:
            raise ConfigError(f"{provider}: {both} named in both add and remove")
        keys = providers[provider].get("keys", [])
        if not keys:
            raise ConfigError(f"provider '{provider}' has no keys to carry models")

        present = {m for key in keys for m in key.get("models", [])}
        alias_targets = {t: a for key in keys for a, t in (key.get("aliases") or {}).items()}
        entry = {
            "added": [m for m in dict.fromkeys(add) if m not in present],
            "already_present": [m for m in dict.fromkeys(add) if m in present],
            "removed": [m for m in dict.fromkeys(remove) if m in present],
            "not_present": [m for m in dict.fromkeys(remove) if m not in present],
        }
        orphaned = [m for m in entry["removed"] if m in alias_targets]
        if orphaned and cascade:
            dead_aliases = sorted(alias_targets[m] for m in orphaned)
            for key in keys:
                live = key.get("aliases") or {}
                for a in [a for a, t in live.items() if t in orphaned]:
                    live.pop(a)
                if "aliases" in key and not key["aliases"]:
                    key.pop("aliases")
                key["models"] = [m for m in key.get("models", []) if m not in dead_aliases]
            entry["aliases_removed"] = dead_aliases
            orphaned = []
        if orphaned:
            raise ConfigError(
                f"{provider}: cannot remove {orphaned}: aliased by "
                f"{[alias_targets[m] for m in orphaned]} -- remove those aliases first"
            )
        for key in keys:
            models = [m for m in key.get("models", []) if m not in entry["removed"]]
            for m in entry["added"]:
                if m not in models:
                    models.append(m)
            key["models"] = models
        diff[provider] = entry
    return cfg, diff


def model_changes_are_noop(diff: dict) -> bool:
    return not any(entry["added"] or entry["removed"] for entry in diff.values())


def operator_violations(cfg: dict, bifrost_dir: Path) -> list[str]:
    """Delegates to `operator_violations()` in bifrost/sync_vk_allowlists.py
    -- the SAME function the sync uses to refuse a non-compliant config.json
    -- loaded from the bind-mounted bifrost dir, so infractl can never
    disagree with the sync about what the operator has turned off."""
    import importlib.util

    script = bifrost_dir / "sync_vk_allowlists.py"
    if not script.exists():
        raise ConfigError(f"{script} not found; cannot check bifrost/operator-disabled.json")
    spec = importlib.util.spec_from_file_location("_infractl_sync_vk_allowlists", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.operator_violations(cfg, str(bifrost_dir / "operator-disabled.json"))


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
    snap = render_redacted_snapshot(bifrost_dir)
    out = bifrost_dir / "config.snapshot.redacted.json"
    text = json.dumps(snap, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out
