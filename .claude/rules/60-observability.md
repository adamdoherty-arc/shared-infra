# 60 — Observability (always-loaded)

Stack: Prometheus (9099 external / 9090 internal), Alertmanager (9095),
Grafana (3050), Loki, Tempo, otelcol, cadvisor, dcgm-exporter —
`docker-compose.observability.yml`.

## Scrape jobs (`observability/prometheus/prometheus.yml`)

| Job | Target | Owns |
|---|---|---|
| `prometheus` | self | meta |
| `legion-backend` | `legion-backend:8005` | Legion app metrics |
| `ada-backend` | `ada-backend:8003` | ADA app metrics |
| `ada-postgres-exporter` | `host.docker.internal:9187` | native Postgres |
| `bifrost-metrics` | `bifrost-metrics:9100` | Bifrost usage/cost/latency (see below) |
| `vllm-chat` | `qwen38-chat:18020` | local chat engine `/metrics` |
| `vllm-embed` | `vllm-embed:8001` | local embed engine `/metrics` — this
target went DOWN 2026-09-22 when the CPU llama.cpp swap started without
`--metrics`; if you ever touch this service's command line, verify
`up{job="vllm-embed"}==1` afterward, don't assume the flag carried over |
| `dcgm-exporter` | `dcgm-exporter:9400` | GPU telemetry |
| `cadvisor` | `cadvisor:8080` | container-level resource metrics |

`ada-prometheus` and `legion-prometheus` (each project's own Prometheus)
scrape `bifrost-metrics` too, via `host.docker.internal:9102` with
`extra_hosts: host.docker.internal:host-gateway` in their compose blocks —
so a bifrost-metrics outage shows up in three places, not one.

## Exporter/prober contract (`bifrost-metrics-exporter/`)

Bifrost's vendor image doesn't ship the upstream Prometheus plugin, so
this sidecar reads `bifrost/logs.db`-equivalent (Postgres log store since
2026-09-22) and re-exports `/metrics` on port 9100 (external 9102).
Exposes `bifrost_requests_total{provider,model,status,request_type}`,
`bifrost_request_latency_ms_bucket{...}`, `bifrost_prompt_tokens_total`,
`bifrost_completion_tokens_total`, `bifrost_cost_usd_total`,
`bifrost_active_providers`, `bifrost_active_virtual_keys`,
`bifrost_logs_db_bytes`. The exporter's probe-model preference list
(`exporter.py`) is a hardcoded set of "which model to probe per provider"
— when a model is removed from `config.json`, remove it from this list in
the same edit or the prober logs errors against a name that no longer
exists (a `bifrost_config_duplicate_keys`-class drift, same shape as
ADA's `scripts/audits/config_drift.py` catches for its own config).
Cursor bootstraps to the log store's newest row on first start so
historical rows don't pollute `rate()` calculations.

## Alert ownership

Alertmanager routes everything except `service-down-redundant` (logged
only) through `legion-webhook` -> `http://legion-backend:8005/api/
webhooks/alertmanager`, which fans out to Discord/Telegram from Legion's
side. **There is currently no dedicated shared-infra Discord receiver** —
a `severity=critical, ecosystem_project=shared` alert rides the same
webhook as everything else, which means it competes for attention with
ADA/Legion app alerts rather than being distinguishable at a glance. If
you add a new shared-infra alert rule, tag it `ecosystem_project="shared"`
consistently (existing rules already do) so a future dedicated route can
filter on it without a rule rewrite.

An alert firing for multiple days unattended (ServiceDown on vllm-embed
3+ days, 2026-09-22 ground truth) is itself a finding — check `docs/
ACCEPTED_FAILURE_MODES.md` first: if it's not on that list with a
threshold and a watcher, it's a live bug, not noise, and gets fixed this
turn (NO DEFERRING).

## Self-test

`infractl`'s `lanes`/`health` probes (see `70-infractl-hostcron.md`) are
meant to catch an exporter/scrape outage before Alertmanager does — if
you're adding a new metric surface, wire a corresponding infractl probe
in the same change rather than relying on Prometheus's own staleness
alerting alone.
