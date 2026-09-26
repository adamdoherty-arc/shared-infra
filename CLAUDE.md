# CLAUDE.md — shared-infra

Rules and operational notes for Claude Code working in
`c:\code\shared-infra\`. This stack hosts the LLM gateway (Bifrost),
local model containers (vllm-chat + vllm-embed), and other
shared services used by Zero, ADA, and Legion.

## NO DEFERRING — FIX IT NOW (MANDATORY)

When the user asks for work, every issue that surfaces during that work
is part of the work. Do not defer, do not flag for follow-up, do not
write a migration plan or punch-list for "future sessions" unless the
user specifically asks for a plan rather than a fix.

Banned phrases / behaviors in this repo:
- "out of scope for today" / "out of scope here"
- "follow-up" / "next session" / "future session" / "deferred"
- "leave as a known issue" / "documented for later"
- "TODO: fix in a follow-up"
- Writing a `*_MIGRATION.md` / `*_PUNCHLIST.md` / `*_FOLLOWUPS.md` and
  handing it back instead of doing the work
- Marking a task "complete" with a "things I didn't finish" section

The rule: if you can see a bug, a regression, a stale config, a wrong
default, a known-broken probe, a 401, a 5xx, a dead service — **fix it
in this turn**. Diagnose, fix, verify, then continue with what the user
originally asked for. Side issues you spot while doing something else
get fixed in-flight.

The only legitimate stops:
- Hard external blocker (vendor API doesn't exist, hardware unplugged,
  paid SaaS with no key the user must provide)
- Genuinely irreversible destructive action (force-push to main,
  dropping a prod table) — confirm before doing those

Everything else: fix it. Don't ask. Don't write a plan. Don't hand back
a checklist with pending items. Finish the job 100%.

## Operational quick-reference

### Bifrost gateway (`shared-bifrost`)

- Host port `4445` → container port `8080`.
- OpenAI-compatible: `POST /v1/chat/completions`, `POST /v1/embeddings`.
- Model field must be `provider/model`, e.g. `vllm-local/qwen3-chat`,
  `nvidia-nim/moonshotai/kimi-k3`, `embed-local/Qwen/Qwen3-Embedding-0.6B`.
- Auth ENFORCED (`enforce_auth_on_inference: true`). Callers must
  send `Authorization: Bearer sk-bf-...` OR `x-bf-vk: sk-bf-...`
  with a valid virtual key. Never call `/v1/models` unauthenticated: Bifrost
  still fans it out and logs one error per provider (2026-09-25).
- Image `maximhq/bifrost:v2.2.3` (2026-09-25; native Prometheus `/metrics`
  on the gateway port); request log store is native-host Postgres
  (`bifrost_logs`), not SQLite, since 2026-09-22.
- **Every published port binds `127.0.0.1`** except Grafana (own login).
  Bifrost's `/api/*` admin routes are unauthenticated and returned every
  provider key and VK in plaintext while 4445 was on 0.0.0.0 (fixed
  2026-09-25). Containers still reach loopback ports via
  `host.docker.internal`. `scripts/gate.py` fails on any non-loopback port.
- **Operator-disabled list: `bifrost/operator-disabled.json`** (operator
  order 2026-09-25: Kimi + Mistral removed everywhere; a provider with no
  working key stays off until the operator re-enables it). The VK sync
  refuses to run and the gate fails if `config.json` contains a listed
  provider or model pattern; ADA's model sync never adds them;
  `scripts/apply_operator_disabled_freellmapi.py` (hostcron, daily) applies
  it inside freellmapi. Never re-enable anything on that list yourself.
  Check keys with `python scripts/probe_provider_keys.py` (status codes only).
- **Active and parked providers: `docs/PROVIDERS.md`** (GENERATED from
  `bifrost/config.json`; the gate fails if it is stale). Do not list
  providers in prose anywhere else -- that is how three docs drifted.
  ALL-FREE policy: no paid provider is active.
- **infractl is the single writer of `bifrost/config.json` +
  `bifrost/disabled-providers.json`** (2026-09-25). Model changes:
  `docker exec shared-infra-control infractl models apply --changes
  '{"<provider>": {"add": [...], "remove": [...]}}' --reason ...` (dry-run by
  default, `--apply` to write); parks: `infractl park|unpark <p>`. It checks
  operator-disabled.json, snapshots, writes, runs the binding restart
  (sync_vk_allowlists.py with Bifrost stopped: config.db mirrors VK
  allowlists/key models and deletes rows for absent providers) and rolls
  back on failure. ADA's model sync and bifrost-autoheal call the same API;
  `.claude/hooks/config_write_gate.py` blocks direct edits
  (`INFRA_CONFIG_WRITE_OK=1` to override); `scripts/config_autocommit.py`
  (hostcron) commits the result with the ledger rows as the message.
  A bare restart with no config change: `bash scripts/bifrost_restart.sh`.
- See `bifrost/README.md` for the full runbook.

### Provider state lives in TWO places

Bifrost holds provider/key config in BOTH `bifrost/config.json` (JSON
seed) AND `bifrost/config.db` (SQLite mirror). Bifrost's own import never
deregisters a provider. To remove one: `infractl park <provider> --reason
... --apply` (moves the block to `disabled-providers.json`, then the
restart's sync step deletes its config.db rows via
`deregister_absent_providers`).

Leave `governance_model_pricing` / `governance_model_parameters` alone
— those are Bifrost's built-in datasheet for ~400 known models, not
active registrations.

### Virtual keys

Seven virtual keys exist; the three original project keys are:

| Project | VK name      | Project env var that holds it |
|---------|--------------|-------------------------------|
| ADA     | `ada-prod`   | `BIFROST_GATEWAY_KEY` in `C:\code\ADA\.env` |
| Zero    | `zero-prod`  | `VLLM_API_KEY` + `ZERO_BIFROST_API_KEY` in `C:\code\zero\.env` |
| Legion  | `legion-prod`| `BIFROST_API_KEY` in `C:\code\Legion\.env` (+ `Legion\backend\.env`) |

The PUT endpoint on `/api/governance/virtual-keys/{id}` silently drops
the `allow_all_keys` field. To grant a virtual key access to a
provider's pool of keys, update `config.db` directly:
```
UPDATE governance_virtual_key_provider_configs SET allow_all_keys=1
  WHERE virtual_key_id='<uuid>';
```
(Stop bifrost first, restart after the update.)

### Local chat backend (`qwen38-chat`) — Pass-9, 2026-08-31

- Host port `18801` → container port `18020` (18801 kept across the engine
  swap because ~15 Legion/Zero consumers target it; the stack's native 18020
  is inside a Windows WinNAT excluded range and cannot be host-bound).
- Model: **Qwen3.8-27B** (`dbirks/Qwen3.8-27B-W4A16-AutoRound`) on the
  syv-ai/qwen38-27b-rtx3090 patched vLLM `0.27.1` stack, batch profile,
  ctx 65,536, MAX_SEQS=32, GPU_UTIL=0.78 (coexists with vllm-embed).
- Serves ONE name `qwen3.8-27b`; ALL legacy aliases (incl. `qwen3-chat`,
  `Qwen3.5-35B-A3B`) are remapped in Bifrost's `vllm-local` key aliases dict.
- Previous engine (`vllm-chat`, Nemotron-3.5-Lightning NVFP4, host port
  18801 → 8000) is intact behind the `nemotron-rollback` compose profile.
  (`llama-cpp-chat` at 18800 was retired 2026-05-17.)

### Local embed backend (`vllm-embed`) — CPU since 2026-09-22 (Fix-1100000610)

- Host port `8001` → container port `8001`.
- Model: Qwen3-Embedding-0.6B, served by `ghcr.io/ggml-org/llama.cpp:server`
  (CPU, NOT vLLM, NOT the GPU) over the official
  `Qwen/Qwen3-Embedding-0.6B-GGUF` f16 build. Moved off the GPU because two
  vLLM processes time-slicing one RTX 5090 measurably starved `qwen38-chat`
  (2.4 tok/s at 30 running while embed was busy; a co-tenant restart alone
  recovered 354->631 tok/s). Same container name + port as the retired GPU
  service, so Bifrost's `embed-local` provider needed no change. GPU version
  kept for rollback behind compose profile `embed-gpu-rollback`
  (`vllm-embed-gpu-rollback`). Full writeup: `docs/engine-refresh-2026-09-21.md`.
- `--cache-ram 0` is load-bearing (2026-09-25): llama.cpp's prompt cache made
  batch-32 take 26.7s; off, 0.7-0.9s. GPU co-tenancy re-tested the same day
  and rejected again (chat 585 -> 50 tok/s under embed load).

### Local reranker (`qwen3-rerank`) — 2026-09-25

- Host port `127.0.0.1:8002`, **GPU** (`llama.cpp:server-cuda-b8953`, `-ngl 99`),
  model: the OFFICIAL `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` (community GGUFs
  strip `cls.output.weight` and score every doc ~1e-23). Fetched once by the
  profile-gated `qwen3-rerank-prepare` into `shared-hf-cache`.
- Measured: 10 docs p50 1.05 s on an idle GPU, +1.6 GB VRAM (31.0/32.6 GB).
  Under chat load it time-slices with qwen38-chat: 6 running chat requests gave
  2.4-2.7 s for 10 docs of 800 chars, 3.3-4.5 s for 15 of 1500; a per-doc floor
  of ~0.2-0.3 s, so `--parallel 4` made it slower (3-8.7 s), not faster. CPU was
  3.6 s at `-t 8`, 8-9 s at `-t 4`, >120 s at `-t 16` (cores shared).
- **Chat cost scales with rerank call rate** (continuous calls cut chat 265 ->
  76 tok/s). Consumers therefore enforce `RERANK_MAX_PER_MIN` (default 10 per
  process), trim docs to `RERANK_DOC_MAX_CHARS` (800), over-fetch 2x, wait up to
  `RERANK_TIMEOUT_S` (6 s, sized for p95 under chat load) and fall back to vector
  order on any failure. Consumers: ADA `stock_knowledge_enrichment_service`
  (chat RAG hits), Legion `rag_service.retrieve_context` and
  `/knowledge/semantic-search`. Do not add a high-volume caller without
  re-measuring chat throughput.
- Healthcheck is a real 2-doc rerank over bash `/dev/tcp` (the CUDA image has no
  python/curl/wget, and `/bin/sh` is dash). `--cache-ram 0`, `--metrics`.

### Weekly lane-quality eval (`scripts/lane_eval.py`) — WS7 follow-on, 2026-09-25

Decides with data which free lane serves which purpose instead of
hand-picked ADA/Legion ladders. Discovers every active, consumer-pinned
provider/model from `bifrost/config.json` (skips `embed-local`, respects
`bifrost/operator-disabled.json` and the probe VK's governance allowlist —
`openrouter` is `ada-prod`-only and is correctly skipped), runs a fixed
12-task suite (`scripts/lane_eval_suite.json`, all deterministic/
programmatic grading, no LLM judge) sequentially per free-tier lane, and
publishes `state/lane_eval/latest.json` + `state/lane_eval/runs.jsonl` +
generated `docs/LANE_QUALITY.md`. `bifrost-metrics` reads `latest.json`
(read-only bind mount) and republishes `bifrost_lane_eval_pass_ratio{...}`
/ `bifrost_lane_eval_age_seconds`; `observability/prometheus/rules/
lane_eval_alerts.yml` fires when a pinned lane scores below 0.5 or the run
is older than 9 days. Run manually: `python scripts/lane_eval.py`
(`--dry-run` for discovery only, no gateway calls). Scheduled weekly via
hostcron (Sunday 05:00) — see `scripts/hostcron/schedule.json`'s
`lane-eval-weekly` entry.

### Project operating rules

`.claude/rules/*.md` (docker, bifrost, engines, observability, infractl +
hostcron, git, Legion), `.claude/settings.json` + hooks (Bifrost restart gate,
agent model gate, session checkpoints). Restart Bifrost ONLY via
`bash scripts/bifrost_restart.sh`. Quality gate: `python scripts/gate.py`
(pre-commit hook via `.githooks/`). Accepted failure modes:
`docs/ACCEPTED_FAILURE_MODES.md`; incident log: `LESSONS_LEARNED.md`.

### Bifrost Prometheus metrics (`bifrost-metrics`)

- Host port `9102` → container port `9100`.
- Bifrost v2.2.3 also exposes native `/metrics` on the gateway port
  (`bifrost_upstream_requests_total`, `bifrost_error_requests_total{error_type}`,
  `bifrost_provider_key_up`, ...). This sidecar predates that and still reads Bifrost's request log store (Postgres since
  2026-09-22; `bifrost/logs.db` under the `sqlite-rollback` profile) (which
  Bifrost writes natively) and re-exports as Prometheus metrics on
  `/metrics`. Source in `bifrost-metrics-exporter/`.
- Both `ada-prometheus` and `legion-prometheus` scrape via
  `host.docker.internal:9102` (their compose blocks have
  `extra_hosts: host.docker.internal:host-gateway`). The bifrost job is
  visible in each Prometheus UI at `/targets`.
- Exposed metrics:
  - `bifrost_requests_total{provider, model, status, request_type}`
  - `bifrost_request_latency_ms_bucket{provider, model, request_type}`
  - `bifrost_prompt_tokens_total` / `bifrost_completion_tokens_total`
  - `bifrost_cost_usd_total` (computed from Bifrost's pricing datasheet)
  - `bifrost_active_providers`, `bifrost_active_virtual_keys` (gauges)
  - `bifrost_logs_db_bytes`, `bifrost_exporter_*` (self-meta)
- The exporter persists its cursor by `timestamp` (VACUUM-safe, Fix-1000157)
  and bootstraps it to MAX(timestamp) on first start, so
  historical rows don't pollute Prometheus rate() calculations.
- Sidecar uses ~50 MB RAM, ~0.05 CPU. Re-builds in ~30 s.

### Retired

- `shared-litellm` at port 4444 — retired 2026-05-14. Service block
  deleted from `docker-compose.vllm.yml`, config archived to
  `.deleted-2026-05-14/litellm_config.yaml`. If you ever need an
  emergency rollback, the last known-good block is in git history
  prior to 2026-05-14.
