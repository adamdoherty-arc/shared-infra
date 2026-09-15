"""Sync Bifrost virtual-key provider configs with the active provider set.

WHY THIS EXISTS (2026-06-11): Bifrost mirrors per-VK model allowlists in
`governance_virtual_key_provider_configs.allowed_models` inside config.db.
Editing config.json updates the provider catalog but NOT the VK allowlists,
so newly added models 403/404 for every project until these rows are
refreshed. Run this after ANY provider/model change in config.json:

    docker stop shared-bifrost
    python c:/code/shared-infra/bifrost/sync_vk_allowlists.py
    docker start shared-bifrost

For every VK x every active provider it upserts a row whose allowed_models is
the UNION of every key's models_json for that provider PLUS every alias name
from aliases_json (allow_all_keys=1), and deletes PC rows for providers that
no longer exist.

ALIASES MUST BE IN allowed_models (2026-09-15): Bifrost v2.0.0 evaluates VK
governance BEFORE alias resolution -- the string the caller sent (e.g.
`nvidia-nim/nemotron-lightning`) is what gets checked against allowed_models,
and only afterwards is it rewritten to the target model id. Verified live via
the probe VK: every alias returned 403 `model_blocked` ("Model 'X' is not
allowed for this virtual key") while the full id it points at completed fine.
Until this fix, allowed_models carried only full ids, so NO alias had ever
been usable through a VK; every alias-based caller was silently falling to
the next lane in its ladder. The per-provider allowlist is therefore built
from models UNION alias-keys across all of the provider's keys (nvidia-nim
has three keys sharing one allowlist).

SECOND SYNC ADDED 2026-09-05 (Fix-1100000305-followup): Bifrost's own
config.json import does NOT refresh `config_keys.models_json`/`aliases_json`
for a provider key that already exists -- only a brand-new key gets its
models/aliases written. Verified live: editing an EXISTING key's `models`
array in config.json and restarting shared-bifrost (even twice, per the
documented sequence) left `config_keys.models_json` showing the SAME value
it had before 2026-09-01, four restarts later, on every provider touched
that session (groq/nvidia-nim/openrouter/freellmapi). Every prior "just
restart Bifrost" instruction for a MODEL LIST change in this repo's docs was
therefore incomplete. `sync_provider_models()` below closes that gap the
same way the VK sync closes the allowlist gap: it reads config.json directly
(the file this repo owns and edits) and writes `config_keys.models_json`/
`aliases_json` verbatim, so the VK sync that follows it operates on current
data instead of silently re-copying the stale value. Both syncs now run
together from `__main__` -- there is no scenario where you want one without
the other.

DB PATH (2026-07-08): defaults to the Windows host path so existing host
invocation is unchanged, but honours the BIFROST_CONFIG_DB env var so the
bifrost-autoheal sidecar (which sees the same file at /work/bifrost/config.db
inside its container) can reuse this exact sync logic during an auto-park.
"""
import json
import os
import sqlite3

DB_PATH = os.environ.get("BIFROST_CONFIG_DB", r"C:/code/shared-infra/bifrost/config.db")
CONFIG_JSON_PATH = os.environ.get("BIFROST_CONFIG_JSON", r"C:/code/shared-infra/bifrost/config.json")


def sync_provider_models(db: sqlite3.Connection, config_json_path: str = CONFIG_JSON_PATH) -> int:
    """Upsert `config_keys.models_json`/`aliases_json` from config.json for
    EVERY provider key, matched by (provider, name). Returns the count of
    rows actually changed (0 rows silently matched-but-identical are not
    counted as an error -- config.json legitimately has no drift most runs).
    A key present in config.json but absent from config_keys is logged and
    skipped -- inserting a brand-new key needs columns (value, key_id, ...)
    this function has no business fabricating; Bifrost's own import handles
    genuinely new keys correctly today, this function only closes the
    EXISTING-key gap.

    models_json is written as models UNION alias names. Bifrost v2 selects a
    key with `key.Models.IsAllowed(<requested model>)` BEFORE it resolves
    `key.Aliases` (core/bifrost.go, "key.Models ... must therefore be
    expressed in alias keys"), so an alias name absent from `models` fails
    with "no keys found that support model: <alias>" even when governance
    allows it. Measured 2026-09-15: zero alias completions had ever
    succeeded on this gateway for that reason."""
    with open(config_json_path, encoding="utf-8") as f:
        cfg = json.load(f)

    changed = 0
    for provider, prov_cfg in cfg.get("providers", {}).items():
        for key in prov_cfg.get("keys", []):
            models = list(key.get("models", []))
            models += [a for a in (key.get("aliases") or {}) if a not in models]
            models_json = json.dumps(models)
            aliases_json = json.dumps(key.get("aliases", {}))
            row = db.execute(
                "SELECT models_json, COALESCE(aliases_json, '{}') FROM config_keys "
                "WHERE provider=? AND name=?", (provider, key["name"]),
            ).fetchone()
            if row is None:
                print(f"[sync_provider_models] NOTE: {provider}/{key['name']} not in config_keys yet "
                      "-- brand-new key, Bifrost's own import handles this on next restart")
                continue
            try:
                same = (json.loads(row[0] or "[]") == models
                        and json.loads(row[1] or "{}") == key.get("aliases", {}))
            except ValueError:
                same = False
            if same:
                continue
            db.execute(
                "UPDATE config_keys SET models_json=?, aliases_json=? WHERE provider=? AND name=?",
                (models_json, aliases_json, provider, key["name"]),
            )
            changed += 1
            print(f"[sync_provider_models] updated {provider}/{key['name']}: "
                  f"{len(models)} models (incl. alias names)")
    db.commit()
    return changed

# Deliberate revocations this sync must NOT undo. Without this the loop below
# re-grants every (vk, provider) pair it finds, so a revocation silently comes
# back on the next run. openrouter is the only PAID provider on this gateway and
# its credits are the owner's, so only ada-prod may reach it.
REVOKED_VK_PROVIDERS = {
    ("legion-prod", "openrouter"),
    ("zero-prod", "openrouter"),
    ("fortressos-prod", "openrouter"),
    ("claude-code-local", "openrouter"),
    ("hermes-prod", "openrouter"),
}

db = sqlite3.connect(DB_PATH)

_changed = sync_provider_models(db)
print(f"synced config_keys.models_json/aliases_json from config.json: {_changed} key(s) changed")

def build_provider_allowlists(db: sqlite3.Connection) -> dict:
    """provider -> JSON array string of every model id AND every alias name
    across all config_keys rows for that provider (deduped, config order kept).
    Alias names are included because governance runs before alias resolution
    (see module docstring, 2026-09-15)."""
    out = {}
    for prov, models_json, aliases_json in db.execute(
            "SELECT provider, models_json, COALESCE(aliases_json, '{}') FROM config_keys ORDER BY id"):
        allow = out.setdefault(prov, [])
        for m in json.loads(models_json or "[]"):
            if m not in allow:
                allow.append(m)
        for alias in json.loads(aliases_json or "{}").keys():
            if alias not in allow:
                allow.append(alias)
    return {prov: json.dumps(allow) for prov, allow in out.items()}


providers = build_provider_allowlists(db)

vks = [(r[0], r[1]) for r in db.execute("SELECT id, name FROM governance_virtual_keys")]

gone = db.execute(
    "DELETE FROM governance_virtual_key_provider_configs WHERE provider NOT IN ({})".format(
        ",".join("?" * len(providers))), list(providers)).rowcount
print(f"deleted PC rows for retired providers: {gone}")

updated = inserted = revoked = 0
for vk, vk_name in vks:
    for prov, models in providers.items():
        if (vk_name, prov) in REVOKED_VK_PROVIDERS:
            db.execute(
                "DELETE FROM governance_virtual_key_provider_configs "
                "WHERE virtual_key_id=? AND provider=?", (vk, prov))
            revoked += 1
            continue
        cur = db.execute(
            "UPDATE governance_virtual_key_provider_configs "
            "SET allowed_models=?, allow_all_keys=1 WHERE virtual_key_id=? AND provider=?",
            (models, vk, prov))
        if cur.rowcount:
            updated += cur.rowcount
        else:
            db.execute(
                "INSERT INTO governance_virtual_key_provider_configs "
                "(virtual_key_id, provider, weight, allowed_models, allow_all_keys, rate_limit_id) "
                "VALUES (?,?,NULL,?,1,NULL)", (vk, prov, models))
            inserted += 1
db.commit()
print(f"updated={updated} inserted={inserted} revoked={revoked} across {len(vks)} VKs x {len(providers)} providers")
for row in db.execute(
        "SELECT provider, COUNT(*) FROM governance_virtual_key_provider_configs GROUP BY provider ORDER BY provider"):
    print("PC rows:", row)
