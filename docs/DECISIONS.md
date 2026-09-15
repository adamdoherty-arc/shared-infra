# Shared-infra decisions (MADR-style, append-only)

Each entry: date, decision, evidence, consequences. Newest last. A decision that turns out
wrong is corrected by a NEW dated entry, never by editing the old one.

## 2026-09-15 — Dead-model prune in `bifrost/config.json`

**Decision.** Remove model ids that the upstream provider no longer serves, and the alias
that pointed at one of them, from the active provider blocks:

| Provider | Removed | Evidence (probe via Bifrost `:4445`, claude-code-local VK, 2026-09-15) |
|---|---|---|
| groq | `llama-3.1-8b-instant`, `llama-3.3-70b-versatile`, `qwen/qwen3.6-27b` | Groq answers HTTP 404 `model_not_found`; the replacement `qwen/qwen3.8-27b` answers 200 |
| groq | alias `qwen3.6-groq` -> `qwen/qwen3.6-27b` | alias target is the 404 model above; no consumer references the alias (grep across ADA, Legion, Zero, shared-infra) |
| gemini (recipe archive block only) | `gemini-2.5-flash`, `gemini-2.5-flash-lite` | provider is parked (no key in the container); the direct-Gemini lane lives in ADA `gemini_client.py`, never in Bifrost |

Bifrost enforces allowlists strictly (unknown model -> 403 `model_blocked`), so a stale id
in a consumer's ladder is a guaranteed wasted call plus a fallback hop. Consumer ladders were
updated the same day: ADA commit `8fdba65242` (bifrost_ladder, persona scoring, llm_pricing,
llm_router, `.env` model ids) and Legion commit `c306d9ea` (router policy, compose default,
telegram bot). `cerebras/*` rungs were replaced too: the provider is not registered in
Bifrost at all (HTTP 400 "failed to get config for provider cerebras: not found").

**Live-probed replacements (all 200 through the gateway):** `groq/openai/gpt-oss-120b`,
`groq/qwen/qwen3.8-27b`, `nvidia-nim/deepseek-ai/deepseek-v4-flash-0731`,
`nvidia-nim/nvidia/nemotron-3.5-lightning-30b-a3b`, `nvidia-nim/nvidia/nemotron-3-super-120b-a12b`,
`hf-router/moonshotai/Kimi-K2.6`, `freellmapi/openai/gpt-oss-120b`. Too slow to sit in a
ladder (curl exit 000 after ~90 s): `nvidia-nim/moonshotai/kimi-k3`,
`nvidia-nim/nvidia/nemotron-3-ultra-550b-a55b`. Forbidden on the probe VK:
`nvidia-nim/deepseek-ai/deepseek-v4-pro-0813` (403).

**Procedure used.** Snapshot to `state/snapshots/20260915-123319-dead-model-prune/`
(gitignored), edit `config.json`, stop `shared-bifrost`, `python bifrost/sync_vk_allowlists.py`,
start, `/v1/models` + one 4-token completion per VK.

**Consequences.** `bifrost-metrics-exporter` no longer probes a hand-written lane list; it
derives one representative lane per ACTIVE provider from `config.json` every tick and writes
`state/probe_lanes.json`, so a parked provider can never raise `BifrostFallbackLaneDown`
again (eight such alerts and one `BifrostCriticalLaneDown` for `nvidia-nim z-ai/glm-5.2`
had been firing since 2026-09-14 on lanes nobody could call). Local lanes get a 120 s probe
budget because the chat engine runs saturated and a slow answer is not a dead lane.
`disabled-providers.json` stays a recipe archive and is never read as "parked".

## 2026-09-15 — Never open Bifrost's `logs.db` from the Windows host while Bifrost runs

**Decision.** Every read of the live `bifrost/logs.db` (integrity checks, WAL checkpoints,
traffic queries) runs INSIDE a container on the same bind mount (`bifrost-logs-pruner` is
the standing home; `docker exec bifrost-logs-pruner python -c "import pruner; ..."`). A
host-side `sqlite3.connect()` of the live database is a defect, not a convenience.

**Evidence (measured 17:09-17:25 UTC).** Docker Desktop bind mounts are 9p/drvfs; SQLite's
fcntl locks are not shared between a Windows process and a container process. A host-side
`PRAGMA quick_check` first reported `database disk image is malformed` (torn pages read during
a Bifrost checkpoint), and on close, believing itself the last connection, checkpointed and
deleted `logs.db-wal` / `logs.db-shm`. Because Bifrost still held handles through the 9p
server, both names became delete-pending ghosts: absent from listings, `O_CREAT` -> ENOENT,
every new in-container open -> `SQLITE_CANTOPEN` (14, "unable to open database file"), a
second attempt -> "disk I/O error". Bifrost kept writing a WAL nobody could open and the
metrics exporter's cursor froze at 17:08:58. Recovery: stop `bifrost-metrics` and
`bifrost-logs-pruner`, `docker stop -t 90 shared-bifrost`, delete the host-created stale
`-wal`/`-shm`, `docker start shared-bifrost`, wait for `/health` 200, start the sidecars;
the exporter cursor advanced again from 17:19. Request-log frames written between Bifrost's
last auto-checkpoint and the stop were lost (request logs only, no config). The same
host-side check, run when it was not racing a checkpoint, returned `ok` in 109 s, so the
"malformed" report was an artifact of the lock gap and not corruption.

**Consequences.**
- `bifrost-logs-pruner` runs `PRAGMA quick_check` in-container after each nightly prune
  (`PRUNE_INTEGRITY_CHECK=1`) and alerts Discord on anything but `ok`. Its CPU cap moved
  0.05 -> 0.50 so the 3.1 GB check finishes in minutes, not the better part of an hour;
  9p read throughput (~13 MB/s measured) is the actual floor.
- ADA `scripts/bifrost_model_sync.py::preflight_check_wal` no longer opens `logs.db` on the
  host; it calls the pruner's `_mid_day_checkpoint()` through `docker exec` and only reports
  the WAL size when the pruner container is down. ADA's
  `scripts/maintenance/bifrost_logs_rotate.py` was audited: it only stats and renames files
  after `docker stop`, never opens the database, and is unchanged.
- The Wave 1 `infractl` `logsdb.py` module inherits this rule: all logs.db access from the
  control container's own mount, never from a host task.

## 2026-09-15 — Discord posts from the sidecars need an explicit User-Agent

**Decision.** `bifrost-logs-pruner` and `bifrost-autoheal` send
`User-Agent: shared-infra-<sidecar>/1.1 (+https://github.com/adamdoherty-arc/shared-infra)`
on every webhook POST.

**Evidence.** Both sidecars' Discord posts had returned HTTP 403 since they were written,
with body `error code: 1010` (a Cloudflare bot-fingerprint block on the default
`Python-urllib/3.x` User-Agent). The webhook itself was valid: a GET on the webhook URL
returned the `#shared-infra-control` channel, and a POST with a custom User-Agent returned
200. Every "logs.db-wal is N MB" and "provider parked" alert the sidecars believed they had
sent since deployment never reached a human.

**Consequences.** Alert delivery from the sidecars is verified live (test posts on
2026-09-15). Any new stdlib-only sidecar copies the header; `infractl/core/discord.py` will
set it centrally.

## 2026-09-15 — Tempo gets a 48-hour watch before it is deleted

**Decision.** `shared-tempo` is revived alongside `cadvisor`; if no consumer sends a trace by
2026-09-17 the container, its Grafana datasource and the otelcol traces pipeline are removed
and the removal is recorded here.

**Evidence.** Tempo and cadvisor had been exited for two weeks with no alert (no meta-alert
existed and no shared-infra alert reached Discord). Loki receives only app-pushed OTLP; no
consumer is configured to export traces today.

## 2026-09-15 — Claude Code usage forensics live in shared-infra, rendered to `docs/claude-usage/`

**Decision.** `scripts/claude_usage_forensics.py` (moved from `~/.claude/bin/`, which is not
git-tracked) is the one home of the weekly Claude Code token/cost forensics. Every run writes
`docs/claude-usage/latest.json`, `docs/claude-usage/history/<date>.json` and renders
`docs/claude-usage/README.md`; `scripts/run-usage-forensics.cmd` (the Windows task
"Claude Usage Forensics - Weekly", Sundays 08:00) commits those paths path-scoped and never
pushes. `~/.claude/bin/claude_usage_forensics.py` is now a shim that executes the repo copy, so
the references in `~/.claude/CLAUDE.md` keep working.

**Evidence.** Baseline week of 2026-09-08 (first tracked run, 2026-09-15): 90,254 assistant turns,
top-tier 72.0% of estimated spend (target <= 30%), 353,746 avg cache-read tokens per top-tier
turn (target <= 200,000), 41.4% of subagent spend on Fable/Opus (target <= 5%). The output was
sitting in `~/.claude/state/usage-weekly.json`, where nobody would open it.

**Why not Prometheus/Grafana.** The numbers change once a week and are already tabular; a page in
the repo is the right surface. The exporter lives in `docker-compose.bifrost.yml` and reads only
Bifrost's SQLite, so wiring a weekly JSON into it would add a bind mount and a compose change for
one scrape a week. Revisit only if the weekly cadence changes.

**Consequences.** `docs/claude-usage/` is written by a scheduled job; do not hand-edit the page.
The report carries no key material (project folder names, session ids, token counts, model
families, gate reasons), and the pre-commit secrets scan runs on every weekly commit.

## 2026-09-15 — Lane decision corrected with 24 h numbers; who was writing `config.json`

**Correction to the dead-model prune entry above.** The prune entry listed aion, hf-router and
sealion as "100 % dead". They are not. The 24 h `logs.db` breakdown (read only through the
pruner container, per the entry below it) is, per provider, ok / error:

| provider | ok | error | note |
|---|---|---|---|
| embed-local | 86,796 | 182 | |
| vllm-local | 19,329 | 9,737 | 120 s 504s at 100 % GPU |
| groq | 547 | 1,971 | |
| nvidia-nim | 858 | 1,534 | |
| openrouter | 1,011 | 1,354 | 402 on paid ids, 429 on `:free` |
| freellmapi | 1,307 | 594 | |
| zai | 182 | 1,438 | |
| hf-router | 56 | 686 | 667 of the errors are `/v1/models` probes |
| aion | 54 | 686 | same |
| sealion | 39 | 686 | same |
| bazaarlink | 0 | 634 | every completion failed |
| cerebras / siliconflow / moonshot | 0 | 589-623 | parked earlier |
| ovhcloud | 3 | 589 | parked earlier |
| mistral | 46 | 2 | |

The 686 "errors" on aion, hf-router and sealion are 667 `unsupported_operation`
(`list_models is not supported by <provider>`) from the metrics exporter fanning
`GET /v1/models` across every provider, plus 18 requests that arrived without a VK. Their
real completions succeed (aion-3.0 for legion-prod and ada-prod, gpt-oss-20b and Kimi-K2.6
via hf-router, three SEA-LION models). **aion, hf-router and sealion stay registered.**
**bazaarlink is deregistered** (0 successes across four model ids over 24 h; its
`config_providers` row 705 removed). The exporter no longer calls `/v1/models` on providers
that reject it, so those 667/day error rows stop at 15:00Z on 2026-09-15.

**The three writers.** Three processes were editing `bifrost/config.json` and `config.db`
with no lock: `bifrost-autoheal` (parks providers), ADA's `scripts/bifrost_model_sync.py`
(Windows task 07:30) and Legion's executor runner (`infra_actions.py`). The a-finance-prod
VK was being blamed for autoheal's own probe failures because the exporter attributed
no-VK rows to the alphabetically first VK; both the exporter fallback and autoheal's
attribution are fixed (0 mis-attributed rows after the recreate). Until `infractl` becomes
the single writer (plan wave 1), every edit to those files goes through the stop-sidecars,
stop-gateway, edit, `sync_vk_allowlists.py`, start, `/health`, probe ladder used here.

## 2026-09-15 — Bifrost aliases had never worked; alias names now live in `models` too

**Finding.** Zero alias completions had ever succeeded on this gateway. Two layers failed:

1. VK governance allowlists (`config_provider_configs.allowed_models`) held provider
   model ids only, so an alias name was refused with `403 model_blocked`. Fixed by
   `sync_vk_allowlists.py` writing models ∪ alias names per VK x provider (72 rows
   across 7 VKs x 11 providers, 5 openrouter rows revoked for the VKs that do not use it).
2. After that, the same requests failed with `no keys found that support model: <alias>`.
   Bifrost v2 selects a key with `key.Models.IsAllowed(<requested model>)` BEFORE it
   resolves `key.Aliases` (`core/bifrost.go`, comment: "key.Models and
   key.BlacklistedModels must therefore be expressed in alias keys"). A key whose
   `models` list lacks the alias name is never selected, however the alias is defined.

**Decision.** Every key that defines `aliases` carries the alias names in its `models`
list. `bifrost/config.json` was patched (nvidia-nim-primary +8, -secondary +4, -tertiary
+4, openrouter-primary +18, hf-router-primary +8, groq-primary +2) and
`sync_provider_models()` now writes `models_json` as models ∪ alias names so the rule
cannot drift. Verified 2026-09-15 ~20:13Z with 1-token completions: `nvidia-nim/
nemotron-lightning`, `nvidia-nim/nemotron-super-120b`, `nvidia-nim/deepseek-v4-flash`,
`groq/qwen36-groq`, `groq/qwen/qwen3.8-27b` all HTTP 200 with real `chatcmpl-` ids.

**NVIDIA NIM catalog vs callable.** The NIM `/v1/models` catalog lists ids that do not
answer: `moonshotai/kimi-k2.6` 404 on all three keys, `z-ai/glm-5.3-flash` 240 s timeout,
`z-ai/glm-5.2` 410 Gone. Removed from `config.json`; `kimi-k3` kept (200, 104 s on a
cold call), `deepseek-v4-flash-0731` kept (529 once, then 200 in 6 s),
`nemotron-3-super-120b` kept (200 in 0.4 s). The `ox-alpha` alias (GLM-5.3 Flash preview)
is retired with it. ADA and Legion callers that name the dead ids are being repointed.

**OpenRouter.** Paid ids return 402 (no credits) and every `:free` id returns 429 within
the day. Owner decision pending on credits; until then the openrouter lane is a
best-effort fallback only and no consumer may depend on it.

**Legion key rotation.** legion-prod's VK was rotated after being found hardcoded in
`smoke_all_lanes.py` / `verify_local_model.py`; both scripts now read `INFRA_PROBE_VK`.
Legion's sampler timed out against vllm-local during the GPU-saturated window; that is
capacity (9,737 local 504s/day at 100 % GPU), not a routing defect.
