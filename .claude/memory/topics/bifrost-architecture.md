# Bifrost architecture — dated facts

Last verified: 2026-09-25 (WS7 ground truth pass; cross-check against
`bifrost/README.md` and `docs/DECISIONS.md` before trusting a number here
more than a few weeks old).

## Topology

3 active consumers (ADA ~130k req/day cap, Legion, A-finance; Zero
intentionally parked) -> `shared-bifrost` (host `4445` -> container
`8080`, image `maximhq/bifrost:v2.0.0` as of 2026-09-25 — a v2.2.3 upgrade
is planned in WS2, not yet applied) -> `vllm-local` (`qwen38-chat`) +
`embed-local` (`vllm-embed`) + cloud lanes (`nvidia-nim` x3 keys,
`openrouter`, `hf-router`, `groq`, `freellmapi`, `sealion`, `aion`, plus
whichever of `mistral`/`gemini`/`zai`/`cerebras` are currently un-parked —
check `bifrost/disabled-providers.json` live, this list churns).

## Virtual keys (per-project, one each)

| Project | VK name | Env var holding it |
|---|---|---|
| ADA | `ada-prod` | `BIFROST_GATEWAY_KEY` in `C:\code\ADA\.env` |
| Zero | `zero-prod` | `VLLM_API_KEY` + `ZERO_BIFROST_API_KEY` in `C:\code\zero\.env` |
| Legion | `legion-prod` | `BIFROST_API_KEY` in `C:\code\Legion\.env` |
| probes | `claude-code-local` / `INFRA_PROBE_VK` | `.env` in this repo |

The PUT endpoint on `/api/governance/virtual-keys/{id}` silently drops
`allow_all_keys` — granting a VK access to a provider's full key pool
requires a direct `config.db` UPDATE with Bifrost stopped (see
`bifrost/README.md` for the exact statement).

## Dual-state mechanics (see `.claude/rules/50-bifrost.md` for the operating rule)

`config.json` (declarative, git-tracked) + `config.db` (SQLite runtime
mirror, gitignored) must be kept in sync via `bifrost/
sync_vk_allowlists.py` after every JSON edit. Three writers touch these
files: ADA's `bifrost_model_sync.py` (hostcron daily 07:30), `bifrost-
autoheal` (auto-parks failing providers), and humans. This is why the
working tree on this repo shows these two files modified between almost
every session — expected, not drift.

## 92% of tokens are local

24h ground truth (2026-09-25): `vllm-local` 21.2k ok / 602 err (2.8%
error), 21.6M prompt + 2.3M completion tokens = 92% of all tokens across
every lane. Cloud lanes combined are under 1k req/day. Cost is $0 — the
entire stack is all-free by design; any nonzero `bifrost_cost_usd_total`
is a misroute, not a feature, and should alert.
