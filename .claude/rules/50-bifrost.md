---
paths: ["bifrost/**", "docker-compose.bifrost.yml", "bifrost-metrics-exporter/**"]
---

# 50 — Bifrost (path-scoped: bifrost/**, docker-compose.bifrost.yml, bifrost-metrics-exporter/**)

Full provider history and hygiene sweeps: `bifrost/README.md`. Architecture
decisions: `docs/DECISIONS.md`. This file is the operating rules, not the
history — append new history to those files, not here.

## Provider state lives in TWO places (the #1 recurring bug class)

Bifrost holds provider/key config in BOTH `bifrost/config.json`
(declarative seed, git-tracked) AND `bifrost/config.db` (SQLite runtime
mirror, gitignored). Editing the JSON does **not** automatically
deregister/reregister anything in the SQLite mirror.

**After ANY `config.json` provider/key/alias edit:**
```bash
python bifrost/sync_vk_allowlists.py
```
then restart via `bash scripts/bifrost_restart.sh` (see `30-docker.md` for
why a bare restart is gate-blocked). Skipping the sync leaves VKs 403'ing
against providers whose per-VK allowlist row in `config.db` never picked
up the JSON change, or leaves the discovery loop spamming `no valid keys
found for provider: X` every ~12s for a provider that still has a
`config_providers`/`config_keys` row.

To fully remove a provider, both tables need cleaning (`config_providers`
AND `config_keys` — a `zai` park on 2026-09-15 found deleting only the
provider row leaves per-VK provider-config rows dangling):
```bash
docker stop shared-bifrost
python3 -c "
import sqlite3
db = sqlite3.connect(r'bifrost/config.db')
db.execute(\"DELETE FROM config_keys WHERE provider IN ('NAME',...)\")
db.execute(\"DELETE FROM config_providers WHERE name IN ('NAME',...)\")
db.commit()"
docker start shared-bifrost
```
Never open `config.db` read-write from the host while the container is
running (see `30-docker.md` "never open SQLite from the host" — WAL
corruption has happened twice).

## Aliases must appear in the key's `models` list

Bifrost v2 selects a key with `key.Models.IsAllowed(<requested model>)`
BEFORE resolving `key.Aliases`. A key whose `models` list lacks an alias
name is never selected no matter how the alias is defined
(`docs/DECISIONS.md` 2026-09-15). Every key that defines `aliases` must
carry those alias names in its own `models` array too — this is the kind
of drift `scripts/gate.py`'s stub/config-drift checks exist to catch
early; if you add an alias, grep the same key's `models` list in the same
edit.

## Key/env passthrough rule

A new provider key goes: `.env` -> compose `environment:` passthrough on
`shared-bifrost` -> the provider block in `config.json` -> `sync_vk_
allowlists.py` -> restart -> smoke (`bifrost/smoke_all_lanes.py` or the
targeted probe in `scripts/bifrost_restart.sh`) -> add to
`bifrost-metrics-exporter`'s probe-model preferences if it should be
monitored -> add to the consuming project's model ladder. Skipping the
compose passthrough step is the most common way a "restored" provider
still 401s — the key exists in `.env` but the container process never saw
it until the next full recreate (`docker restart` does not re-read
`.env` — see `30-docker.md`).

## Provider tiering (informal, current as of 2026-09-25 — verify live before trusting)

`vllm-local` and `embed-local` are the only lanes carrying real volume
(92% of tokens on `vllm-local`, 24h ground truth). Every cloud lane is a
rate-limited free tier and MUST be treated as best-effort fallback, never
a dependency any consumer requires to function. `nvidia-nim` (3 keys,
40 RPM/key) is the deepest reliable cloud fallback; `groq`/`openrouter`/
`hf-router` are thinner; `mistral`/`gemini`/`zai`/`cerebras` cycle between
active and parked depending on key validity and free-tier terms — check
`bifrost/disabled-providers.json` for what's currently parked and why
before assuming a provider from README prose is live. `sealion` and
`aion` have no confirmed consumer as of 2026-09-25 ground truth (WS1
scope, other session) — do not build new dependencies on them without
checking `docs/DECISIONS.md` for their latest status first.

## Parked-recipe restore mechanics

Every parked provider's block is preserved verbatim in
`bifrost/disabled-providers.json` with a comment on why it was parked and
what re-enabling requires (usually: a fresh/rotated key, a rate-cap on
the VK that starved it, or an upstream fix). Restoring: copy the block
back into `config.json`, run the sync with Bifrost **stopped** (not
running — `sync_vk_allowlists.py`'s SQLite writes conflict with a live
container in some cases), then restart and smoke-test before declaring it
live.

## Auth self-heal

`bifrost-autoheal` parks any provider that returns repeated auth failures
(config.json -> disabled-providers.json + config.db deregister + sync +
restart). This is why `config.json`/`disabled-providers.json` are
expected-dirty files — see `20-git-workflow.md`.
