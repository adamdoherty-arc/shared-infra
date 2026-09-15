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
