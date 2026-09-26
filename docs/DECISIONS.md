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

---

## 2026-09-15 — zai parked: ZAI rate-limits ada-prod's volume, successes take a minute

**Decision.** `zai` moves from `config.json` to `disabled-providers.json`. Active set is now
ten providers: vllm-local, embed-local, nvidia-nim, openrouter, hf-router, groq, freellmapi,
sealion, aion, mistral.

**Evidence (logs.db, 24 h to 2026-09-15 20:00Z).** `zai/glm-4.5-flash` from ada-prod:
763 errors at 11.1 s average, 178 successes at ~61 s average; 733 of the errors were
HTTP 429 `{"code":"1302","message":"Rate limit reached for requests"}` from ZAI's side,
47 were Bifrost 504s. `list_models is not supported by zai provider` accounted for a
further 759 rows of pure noise. Manual 1-token probes succeeded 3/3 at 5-8 s, so the key is
valid; the lane simply cannot carry ADA's request rate. A direct call to ZAI's
`/api/paas/v4/models` on the same key lists glm-4.5, glm-4.5-air, glm-4.6, glm-4.7, glm-5,
glm-5-turbo, glm-5.1, glm-5.2, glm-5.3 and glm-5.3-flash (glm-4.5-flash is not listed but
still answers).

**Re-enable rule.** Copy the block back (consider `glm-5.3-flash`), delete nothing else,
run the sync with Bifrost stopped, restart. Only after the weekly confirm-probe passes AND a
per-VK rate cap exists so ada-prod cannot 429 the lane for every other consumer.

**Mechanics learned while parking.** `bifrost/sync_vk_allowlists.py` derives its set of
retired providers from `config_keys` rows in `config.db`, not from `config.json`. Deleting
only the `config_providers` row leaves the seven per-VK provider-config rows in place; the
key row (`zai-primary`) has to go first, then the sync deletes them ("deleted PC rows for
retired providers: 7"). The script has no `--help`; any invocation runs the sync. The
exporter's `_DEFAULT_PROBE_MODEL_PREFS` lost its `zai` line and `bifrost-metrics` was
rebuilt (its image is built from `bifrost-metrics-exporter/`, so an edit needs
`docker compose -f docker-compose.bifrost.yml build bifrost-metrics` plus a force-recreate);
after the restart `bifrost_lane_up` lists nine lanes, all 1.0, none zai. `openrouter` is
skipped by the prober as `vk_not_allowed` because the probe VK (claude-code-local) does not
carry openrouter; that lane is on 402 (no credits) and is an owner sign-off item anyway.

## 2026-09-15 — Who was probing Bifrost, and with which key

Four separate probers were attributed while chasing "who is hammering dead lanes":

- **bifrost-metrics exporter**: its auto-picked probe model was hitting a-finance's
  allowlist; fixed earlier today, now probes with `BIFROST_PROBE_VK` = claude-code-local.
- **Zero**: an httpx prober on zero-prod. Zero is intentionally OFF; the prober stopped with it.
- **Legion `stack_monitor`**: the hourly `legion-llm-ops/1.0` sweep sends no VK at all; it is
  the source of the `virtual_key_required` 401 rows on every provider. It moves into
  `infractl` with the llm_ops role.
- **Manual curl tests** from this session, all under `INFRA_PROBE_VK`.

Legion's `VLLM_API_KEY` was stale (pointed at a revoked VK); fixed in Legion's `.env` and both
containers force-recreated (`docker restart` does not re-read `.env`). Stray VK files under
`C:\tmp` were deleted. The host-side env var for the probe key is `INFRA_PROBE_VK`;
`BIFROST_PROBE_VK` is only the exporter container's own variable name.

## 2026-09-25 — Deep review: live fixes, lane truth, ADA operating pattern

**Embeddings: prompt cache off, not GPU.** `vllm-embed` batch latency had drifted to batch-16 7.4 s /
batch-32 26.7 s (gateway p95 8.3 s, 5-6 autoheal restarts/day). Side-container A/B, same image and host
load, flags only: `--cache-ram 0` gives batch-32 0.88 s at the existing `-t 8 --parallel 4`; thread count
was not the lever. Deployed: batch-32 0.69 s. `--metrics` added (the `vllm-embed` scrape job had been DOWN
since the 09-22 CPU cutover; ServiceDown critical fired for 3 days). GPU co-tenancy re-tested with
llama.cpp CUDA: batch-32 0.15 s, but chat fell 585 -> 50 tok/s under sustained embed load. Rejected again.

**vllm-autoheal double restarts.** `CURL_TIMEOUT` (30 s) < `AUTOHEAL_DEFAULT_STOP_TIMEOUT` (60 s): the
restart API call aborted mid-stop, was logged "failed", and the next tick restarted again. Now 120 s.

**Wedge monitor counter reset.** After an engine restart the first poll computed gen_rate -2.8e6 tok/s and
counted stall + starved samples. `counters_reset()` drops the baseline when counters go backwards.

**Unauthenticated `/v1/models` is the noisy call now.** Re-measured on v2.0.0: authenticated listing 0.15 s
(the 2026-09-15 30 s hang was v1.5); unauthenticated returns 401 fast but logs one "virtual key is required"
error per provider (~2,600 rows/day). infractl probes now authenticate with INFRA_PROBE_VK; the restart
poll uses `/health`.

**Lane corrections (all probed directly and through the gateway).** NIM `deepseek-v4-flash(-0731)` is EOL
(410); its `deepseek-v4*` aliases are removed. NIM `kimi-k2.6` still 404; `kimi-k3` is live. The daily
model sync had removed live-but-rate-limited models (sealion Gemma-v4-27B, Qwen-v4.5-27B; aion 3.0 /
3.0-mini; groq qwen3.8-27b) and added a nonexistent `aion-2.5`; restored / removed, and
Nemotron-SEA-LION-v4.8-120B added. Rule for the sync (fixed in ADA): 429 / timeout never marks a model
dead; only 404 / 410 / unknown-model does, and adoption requires a 200.

**Lane prober retries cloud lanes once.** nemotron-3.5-lightning (NIM's busiest model here) read DOWN on
every 10-minute probe while serving traffic: NIM scales idle functions to zero, first call 50 s, next 0.6 s.
A cloud lane is DOWN only after a second failure; local lanes are never retried.

**Providers are documented only by generation.** `docs/PROVIDERS.md` is rendered from config.json and
checked by `scripts/gate.py`. Three prose lists (CLAUDE.md, README.md, bifrost/README.md) had drifted.

**Alert routing.** Every critical `ecosystem_project="shared"` alert goes to Discord + Legion. Parked
projects (Zero) are excluded from heartbeat alerts by name.

**Repo operating pattern (ADA-style).** `.claude/` rules/hooks/settings, `scripts/gate.py` (compose config,
tests incl. infractl in a disposable container, ruff ratchet that fails closed, stub detector, providers
doc), `.githooks/` pre-commit, `scripts/bifrost_restart.sh` as the only Bifrost restart path.

## 2026-09-25 (evening) -- Loopback-only ports, Bifrost v2.2.3, operator-disabled list

**Security: every unauthenticated port now binds 127.0.0.1.** Bifrost's `/api/*` admin routes
(`/api/keys`, `/api/providers`, `/api/governance/virtual-keys`) need no auth and returned every provider
key and virtual key in plaintext. Port 4445 was published on 0.0.0.0, and the Windows firewall rule
"Docker Desktop Backend" allows any port on the Public profile. Raw vLLM, freellmapi's admin UI,
Prometheus, Alertmanager, Loki, otelcol and SearXNG were exposed the same way. All are now
`127.0.0.1:` except Grafana (own login). Verified: Legion reaches `host.docker.internal:4445`, ADA
reaches `shared-bifrost:8080`, all Prometheus targets up. Guard: `scripts/gate.py` stage
`loopback_ports` checks every compose file across all profiles.

**Bifrost v2.0.0 -> v2.2.3.** Off-hours; backup in `bifrost/pre-v2-2.2.3-*/` (config.db, config.json,
disabled-providers.json, log-store schema). Migrations ran clean, gateway healthy in 33 s, every lane
re-probed. Gains: upstream cancellation on client disconnect, bounded streaming header waits,
`error_type` on errors, native `/metrics`.

**Operator order: Kimi and Mistral removed everywhere; providers without a working key stay off until
the operator re-enables them.** Single source: `bifrost/operator-disabled.json` (model patterns,
providers, freellmapi platforms/models). Enforcement, each with a test in
`bifrost/tests/test_operator_disabled.py`:
- `sync_vk_allowlists.py` refuses to run on a non-compliant config.json, and now deletes config.db rows
  for providers absent from config.json (Bifrost's import never did; this replaces the manual procedure).
- The gate runs that test, so a commit re-adding a banned model fails.
- ADA `scripts/bifrost_model_sync.py` filters proposals against the list.
- `scripts/apply_operator_disabled_freellmapi.py` (hostcron daily 07:45) disables matching keys and
  fallback entries inside freellmapi, which has its own vault and would otherwise keep routing to them.
Removed from Bifrost: 16 Kimi/Mistral model entries and aliases on nvidia-nim, openrouter and hf-router.
Parked: `aion` (key valid but "Daily token limit exceeded" on every probe, 60% error over 24 h).
`scripts/probe_provider_keys.py` found every other active key working. In freellmapi: github (GitHub
Models retired 2026-07-30), llm7 and pollinations (erroring) keys disabled; Mistral, Cloudflare,
Kimi and the 404ing SambaNova `DeepSeek-V3.1-cb` entries disabled. `freellmapi/gpt-oss-120b` went from
502 to 200.

**Consumers repointed.** NIM `nemotron-3-super-120b-a12b` sends `Deprecation: 2026-10-03`, so it is not a
replacement. Reasoning/judge -> `nvidia-nim/nvidia/nemotron-3-ultra-550b-a55b`; code ->
`hf-router/Qwen/Qwen3-Coder-480B-A35B-Instruct`; long-context/general HF -> `hf-router/zai-org/GLM-5.1`;
fast -> `nemotron-3.5-lightning` / `groq/openai/gpt-oss-120b`. ADA, Legion and Zero updated.

## 2026-09-25 (late) -- Error sweep follow-through

**Grafana default password.** Grafana is the only LAN-reachable port and accepted `admin/admin`. Reset
to a generated password in `.env` (`GRAFANA_ADMIN_PASSWORD`, user `admin`); compose requires the
variable; the gate fails on a default admin password for any LAN-exposed service.

**Bifrost admin API stays without its own login (decision).** With 4445 bound to 127.0.0.1 the admin
routes are reachable only from this host and from containers, which can already read the same keys
from `shared-infra/.env` and the bind-mounted `bifrost/` directory. An admin account would add a
credential that Legion (`/api/keys`, `/api/providers`), infractl and the disk-compaction script must
all carry, without narrowing who can read the keys. Revisit if any port is ever exposed beyond
loopback; the gate's `loopback_ports` stage is what keeps this decision valid.

**infractl synthetic probe.** It probed openrouter with a VK revoked for openrouter (403 every 15 min)
and the embed lane with a chat request; both counted as "responsive". Lanes are now filtered by the
VK's governance rows (read-only config.db) and embed lanes must return a vector.

**Abandoned local-engine work (measured).** Over 3 minutes vLLM completed 101 requests while Bifrost
returned 37 successes and 58 client cancels (499); vLLM's abort counter stayed 0. Bifrost v2.2.3 does
not abort the upstream request when the client disconnects, so every cancelled call still runs to
completion (1,529 such cancels in 3 h in the log store). Source: ADA's analyst-desk case-capsule
narrator (`case_capsule_producer._narrate`), whose 5 s "cloud" rung often resolves to vllm-local
(engine p95 ~14 s). The decisions-digest and market-regime budgets were suspected first but make no
LLM calls. Fixed in ADA `f7838408d`: the call runs behind `asyncio.shield`, one in-flight task per
prompt key, 120 s hard wall, and its result lands in the semantic cache the next request reads.

**Alert noise.** `CacheHitRateLow` fired on caches with 2-3 lookups per window; it now needs >= 1
lookup/min. Legion `ImprovementEffectivenessDropping` fired on an idle loop (nothing verified since
2026-05-23): the gauge now reads NaN with no verified rows.

## 2026-09-25 (late) -- Weekly lane-quality eval harness (WS7 follow-on)

**Decision.** Stop hand-picking which free lane serves which purpose in ADA/Legion's ladders and
decide with data instead. Added `scripts/lane_eval.py`: discovers every active, consumer-pinned
provider/model from `bifrost/config.json` (harvested by grepping ADA's `bifrost_ladder.py` /
`llm_router.py` and Legion's `llm_router_policy.py` for `"provider/model"` literals that actually
exist in that provider's model list), skips anything `bifrost/operator-disabled.json` bans and
anything the probe VK's `config.db` governance rows don't allow (same read-only query as
`infractl/probes/lanes.py`'s `vk_allowed_providers`), then runs a fixed 12-task suite
(`scripts/lane_eval_suite.json`) through the real gateway sequentially per lane. Every grader is
programmatic (JSON-Schema validation, exact-answer regex, tool-call arg comparison, subprocess-executed
code-fix asserts, keyword/refusal checks for summarization) -- no LLM judge anywhere.

**First real run (2026-09-25T21:05 -> 2026-09-26T01:21 ET, 990s, 11 lanes discovered, `openrouter`
correctly skipped `vk_not_allowed`, `embed-local` correctly skipped as non-chat):**

| Purpose | Best lane (this run) | Pass ratio |
|---|---|---|
| json | `groq/qwen/qwen3.8-27b` | 1.00 |
| tools | `groq/qwen/qwen3.8-27b` | 1.00 |
| reasoning | `freellmapi/deepseek-ai/deepseek-v4-pro` | 1.00 |
| long_context | `groq/qwen/qwen3.8-27b` | 1.00 |
| code | `groq/qwen/qwen3.8-27b` | 1.00 |

The production default `vllm-local/qwen3-chat` scored 0.92 overall (missed only the arithmetic task
-- it answered 3672 instead of 3772 -- and one 429 on the needle task), close to the two `freellmapi`
lanes that scored a clean 1.00/12. `hf-router` scored 0.33 (`GLM-5.1`) and errored 402 on every task
past the second (`Qwen3-Coder-480B` 402'd on all 12) -- the account's ~100k/month free credits appear
depleted, matching `docs/PROVIDERS.md`'s tier note; this is a real, actionable finding the eval
surfaced on its first run, not a harness bug. `nvidia-nim/nemotron-3.5-lightning-30b-a3b` timed out
3/12 tasks (90s cloud budget) and produced un-fenced chain-of-thought that the code/json graders
correctly failed. This is exactly the kind of signal ladders picked by hand never surface.

**Wiring (not a dead report).** `bifrost-metrics`'s `exporter.py` now has `_scrape_lane_eval()`,
reading a read-only bind mount of `state/lane_eval` (`docker-compose.bifrost.yml`) and exposing
`bifrost_lane_eval_pass_ratio{provider,model,category}` / `bifrost_lane_eval_age_seconds`.
`observability/prometheus/rules/lane_eval_alerts.yml` (validated via `promtool check rules`,
hot-reloaded into `shared-prometheus`) fires `LaneEvalPinnedLaneScoringLow` (<0.5) and
`LaneEvalStale` (>9 days old or never run). The exporter image was NOT rebuilt this session (the
code change needs `docker compose -f docker-compose.bifrost.yml build bifrost-metrics` +
`bash scripts/bifrost_restart.sh` to take effect) -- the gauges will read zero/absent until that
rebuild happens.

**Scheduling.** Not added to `scripts/hostcron/schedule.json` directly (another session owns that
file this pass); the exact `lane-eval-weekly` job block (Sunday 05:00, `timeout_s: 3600`,
`PYTHONPATH` set the same way as `shared-infra-gate`) is written to
`state/lane_eval/hostcron_job.json` for that session to merge in.

## 2026-09-25 (later) -- New service: qwen3-rerank CPU reranker

**Decision.** Added `qwen3-rerank` (`docker-compose.vllm.yml`, port 8002) alongside `vllm-embed`,
same `ghcr.io/ggml-org/llama.cpp:server` CPU family, serving the OFFICIAL
`ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` via `--reranking --pooling rank`. Community requantizations
of this model were checked and rejected: they strip `cls.output.weight` (the classifier head
`--pooling rank` reads) and return near-zero relevance scores for every document regardless of
query -- only the ggml-org build, purpose-built for llama.cpp reranking, carries that head. Weights
fetched once into the shared `shared-hf-cache` volume via a profile-gated `qwen3-rerank-prepare`
one-shot (mirrors `qwen38-prepare`'s pattern). `-t 4` / `cpus: "3.0"` chosen to leave headroom
alongside `vllm-embed` (`-t 8`, cpus 8.0) and `qwen38-chat` (cpus 4.0) on the 32-core host --
`--parallel 1` with more threads was tried and measured WORSE under load (single-slot serialization);
the default multi-slot behavior at `-t 4` was kept. `--cache-ram 0` carried over from `vllm-embed`
for the same prompt-cache reason.

**Measured.** 3-query / 10-document relevance fixture (capital of France / photosynthesis / cold
symptoms, each with 1 clearly-relevant + 2-3 clearly-irrelevant documents): PASS on all 3 queries --
relevant-document scores 0.993-0.998, irrelevant-document scores 1e-5-3e-4, a >3-order-of-magnitude
separation. Healthcheck asserts the same ranking invariant on a live 2-document call (not a bare
`/health` liveness probe), same rationale as `vllm-embed`'s real-embedding healthcheck.

p50 latency for a 10-document `/v1/rerank` call, measured with `qwen38-chat` actively serving traffic
at ~220% CPU: **9.4s** (10-run sample: 8.7-12.6s). This is high for a 0.6B cross-encoder and is a
genuine CPU-contention finding, not a config bug -- confirmed by testing `--parallel 1` with `-t 6`
and `cpus: 8.0` (both raised, to isolate the variable) which measured WORSE (15-37s) under the same
host load, because forcing single-slot serialization removed the only parallelism the default
4-slot behavior was providing. The host's chat engine is the dominant consumer of CPU headroom right
now; revisit this service's thread/cpu budget if `qwen38-chat`'s own CPU footprint changes, and
re-measure before changing `-t`/`cpus`/`--parallel` in either direction (same discipline as
`vllm-embed`'s tuning history in `.claude/rules/55-engines.md`).

**Observability.** Prometheus scrape job `qwen3-rerank` added (`observability/prometheus/prometheus.yml`,
`--metrics` exposes the same llama.cpp `/metrics` surface as `vllm-embed`); `ServiceDown` (generic,
already fires on any `up==0`) covers it, plus a `Qwen3RerankTargetMissing` `absent()` guard added to
`observability/prometheus/rules/service_down.yml` matching the `AdaBackendTargetMissing` pattern,
tagged `ecosystem_project: shared`. Confirmed `up{job="qwen3-rerank"}` reporting healthy in
Prometheus's `/api/v1/targets` after a `-/reload`.

**Tests.** `bifrost/tests/test_qwen3_rerank.py`: one pure unit test of the `/v1/rerank` payload
builder (no network), one `@pytest.mark.live` test replaying the same 10-document fixture against
the running container and asserting relevant docs outrank irrelevant ones. Both pass.

**Consumer contract (not this session's scope to wire).** ADA and Legion should call `POST
http://host.docker.internal:8002/v1/rerank` with `{"model", "query", "documents"}`, reading back
`{"results": [{"index", "relevance_score"}, ...]}`.

## 2026-09-25 (night) -- infractl is the single writer of the Bifrost config

**Problem.** `bifrost/config.json` and `bifrost/disabled-providers.json` had three uncoordinated
writers: ADA `scripts/bifrost_model_sync.py --apply` (wrote config.json and ran its own restart:
stop -> start -> sync against the RUNNING gateway -> restart, never touching bifrost-autoheal),
`bifrost/auth_autoheal.py` (moved blocks and deleted config.db rows itself), and humans. Git never
reflected live routing, and none of them ran the binding restart sequence.

**Decision.** Every write goes through infractl's action ladder: ADA posts `bifrost_models_apply`
(`POST /api/bifrost/models/apply`, `{changes: {provider: {add, remove}}}`), bifrost-autoheal posts
`bifrost_provider_park` (renamed from `provider_park`), humans use `infractl models apply` /
`park` / `unpark`. The ladder plans in memory first (provider active, and the resulting config
checked with `sync_vk_allowlists.operator_violations()`, the sync's own function), so a bad request
is a 400/422 with no write and no restart; then snapshot, atomic write, the binding restart (same
steps as `scripts/bifrost_restart.sh`, in-container, both containers always restarted in
`finally`), verify, and on any failure (sync refusal, unhealthy, 1-token probe not 200) restore
config.json + disabled-providers.json and restart again. The dead local write / backup / restart /
WAL-preflight code in ADA's script and the park/deregister/sync code in autoheal were deleted.

**Fixes found on the way.** (1) infractl's own restart ladder was the non-binding one above, and
its rollback copied the snapshot's config.db back over the live file under a running gateway;
rollback now restores only the two JSON files and the sync rebuilds config.db with Bifrost
stopped. (2) The heal rule for config-parity drift ran `vk_resync` (a config.db write) against the
running gateway; `vk_resync` is now the binding restart (T2) and heal rules force dry-run on T2
kinds, as the heal module's docstring always claimed. (3) An unparked provider has no
`config_keys` rows until Bifrost imports it, so the sync could not grant VK allowlists for it;
the ladder runs a second stop/sync/start cycle in that case. (4) Blocking restarts ran on the
event loop; they now run in a worker thread. (5) `model_scanner` pruned dead models one action
(one restart) per model; it now sends one batched `bifrost_models_apply`.

**Git truth.** The container has no .git, so `scripts/config_autocommit.py` (hostcron, 15 min)
commits the two files plus `config.snapshot.redacted.json` (infractl refreshes it after each
verified change) with `git commit -- <paths>`, a `config:` message from the ledger rows since the
last config commit, the pre-commit gate running normally, and skips while a human has them staged.
**Guard.** `.claude/hooks/config_write_gate.py` blocks agent Edit/Write/Serena writes and shell
redirects/`tee`/`sed -i`/`cp`/inline-Python writes into either file unless
`INFRA_CONFIG_WRITE_OK=1`. Tests: `infractl/tests/test_bifrost_models_apply.py`,
`bifrost/tests/test_auth_autoheal_park.py`, `bifrost/tests/test_config_autocommit.py`,
`.claude/hooks/tests/test_config_write_gate.py`, ADA
`backend/tests/test_bifrost_model_sync_infractl_apply.py`.

**Rollout state.** infractl rebuilt and live (dry-run exercised against the live config;
operator-disabled refusal verified). bifrost-autoheal must be force-recreated
(`docker compose -f docker-compose.bifrost.yml up -d --force-recreate bifrost-autoheal`) to load
the new script AND the new `INFRACTL_TOKEN` passthrough; a plain stop/start (which every infractl
restart ladder does) loads the new script without the token, and parks then fail closed (logged,
alerted, nothing written) until the recreate. ADA's hostcron job needs no change.

## 2026-09-25 (night) -- qwen3-rerank moves to the GPU (supersedes the CPU entry above)

The CPU build measured 9.4 s p50 for 10 docs, so every consumer call (3 s timeout) would have timed out and
fallen back: a service nobody benefits from. Re-measured: CPU `-t 8 -b/-ub 4096 --parallel 1` 3.6 s;
`-t 16` >120 s (oversubscribed; cores shared with qwen38-chat host threads and vllm-embed). Embeddings stay
fast on CPU (10 docs 0.7 s) because the reranker template triples tokens per doc and Q8 runs slower than
f16 on this AVX2-only CPU. GPU (`server-cuda-b8953`, `-ngl 99`, `-c 4096`): p50 1.05 s, +1.6 GB VRAM.
Chat impact measured directly: 60 s baseline 265 tok/s; 60 s with continuous reranking (81 calls/min)
76 tok/s. Impact scales with duty cycle, and today's consumer paths are human-triggered (chat RAG
preamble, Legion RAG search; single-digit calls per hour observed), so the GPU is acceptable only with a
hard per-process cap: `RERANK_MAX_PER_MIN=10` in ADA and Legion (worst case ~17% duty, typical ~0).
Consumers trim docs to 1500 chars, over-fetch 3x, and fall back to vector order on failure or a spent
budget. Legion's calls were also moved from blocking `httpx.post` to `httpx.AsyncClient`.

## 2026-09-25 (night) -- Gateway policy signals, and the quarantine re-probe that never could pass

Bifrost's native `/metrics` (scrape job `bifrost-native`) splits errors by `error_type`. The first
look showed 459 `policy_model_blocked` series from `ada-prod`: ADA's `probe_quarantined_rungs` pinged
every rung ever quarantined in `provider_health` every 6 h, ~475 OpenRouter ids that are no longer in
the gateway's allowlist, so Bifrost rejected every one at the VK policy. ADA now skips rungs
`rung_unprovisioned_reason` flags and re-probes them once the gateway lists them again (ADA
27c52fb9a). A VK rate cap surfaces as `error_type="policy_rate_limited"` (measured by tripping a
2/min cap on the probe VK, then restoring it). Both are alerted in `rules/bifrost_policy.yml`.
The `config_write_gate` hook blocked a hostcron `schedule.json` edit because the inline Python
mentioned `bifrost/config.json` in prose next to an unrelated `json.dump`; inline Python now needs
the protected path as a string literal of its own (regression tests added).
