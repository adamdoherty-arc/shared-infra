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
a verbatim copy of the provider's config_keys.models_json (allow_all_keys=1),
and deletes PC rows for providers that no longer exist.

DB PATH (2026-07-08): defaults to the Windows host path so existing host
invocation is unchanged, but honours the BIFROST_CONFIG_DB env var so the
bifrost-autoheal sidecar (which sees the same file at /work/bifrost/config.db
inside its container) can reuse this exact sync logic during an auto-park.
"""
import os
import sqlite3

DB_PATH = os.environ.get("BIFROST_CONFIG_DB", r"C:/code/shared-infra/bifrost/config.db")

db = sqlite3.connect(DB_PATH)

providers = {}
for prov, models in db.execute("SELECT provider, models_json FROM config_keys"):
    providers[prov] = models  # already a JSON array string

vks = [r[0] for r in db.execute("SELECT id FROM governance_virtual_keys")]

gone = db.execute(
    "DELETE FROM governance_virtual_key_provider_configs WHERE provider NOT IN ({})".format(
        ",".join("?" * len(providers))), list(providers)).rowcount
print(f"deleted PC rows for retired providers: {gone}")

updated = inserted = 0
for vk in vks:
    for prov, models in providers.items():
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
print(f"updated={updated} inserted={inserted} across {len(vks)} VKs x {len(providers)} providers")
for row in db.execute(
        "SELECT provider, COUNT(*) FROM governance_virtual_key_provider_configs GROUP BY provider ORDER BY provider"):
    print("PC rows:", row)
