# Claude Code usage -- weekly forensics

Rendered by `scripts/claude_usage_forensics.py` (Windows task "Claude Usage Forensics - Weekly", Sundays 08:00, via `scripts/run-usage-forensics.cmd`). Do not hand-edit; the next run overwrites it.

- **Host:** VENGEANCE
- **Generated:** 2026-09-27T12:00:03Z (UTC)
- **Window:** last 7 day(s), turns dated >= 2026-09-20
- **Transcripts scanned:** 1,790 files (0 unreadable), 123,516 assistant turns in window
- **Estimated list-price cost of tokens consumed:** $25,680.60
- **Raw report:** [latest.json](latest.json) -- history under [history/](history/)

## Targets (Enhancement-1001070)

| Target | This week | Limit | Status |
|---|---|---|---|
| Top-tier (Fable/Opus) share of spend | 66.5% | <= 30.0% | **OVER** |
| Avg cache-read tokens per top-tier turn | 281,961 | <= 200,000 | **OVER** |
| Subagent spend on top-tier | 31.3% | <= 5.0% | **OVER** |

## Spend by model family

| Family | Turns | Input | Output | Cache read | Cache write | Est. cost | Share |
|---|---|---|---|---|---|---|---|
| top | 29,156 | 84,774 | 23,635,602 | 8,220,865,539 | 158,784,105 | $17,082.44 | 66.5% |
| sonnet | 89,103 | 178,206 | 26,024,837 | 22,867,576,906 | 343,903,012 | $8,540.82 | 33.3% |
| haiku | 5,150 | 42,218 | 823,269 | 257,808,798 | 21,924,101 | $57.34 | 0.2% |
| other | 107 | 0 | 0 | 0 | 0 | $0.00 | 0.0% |

Main loop $13,233.24 (51.5%) vs subagents $12,447.36 (48.5%). Avg cache-read per top-tier turn: 281,961.

## Cost by day

| Day | Est. cost |
|---|---|
| 2026-09-20 | $0.00 |
| 2026-09-21 | $3,224.69 |
| 2026-09-22 | $4,643.69 |
| 2026-09-23 | $6,644.53 |
| 2026-09-24 | $1,461.60 |
| 2026-09-25 | $4,824.56 |
| 2026-09-26 | $3,280.97 |
| 2026-09-27 | $1,600.55 |

## Top sessions

| Project | Session | Est. cost | Turns | Model mix |
|---|---|---|---|---|
| c--code-ADA | `a20e3745` | $3,025.52 | 19,775 | sonnet 17252, top 2245, haiku 278 |
| c--code-ADA | `c3ac1d52` | $2,268.66 | 5,439 | top 3132, sonnet 2077, haiku 222, other 8 |
| c--code-ADA | `57cfe235` | $2,123.96 | 7,896 | sonnet 5186, top 2547, haiku 149, other 14 |
| c--code-ADA | `e8e2da59` | $1,731.35 | 10,278 | sonnet 8712, top 1343, haiku 216, other 7 |
| c--code-ADA | `1c2ec633` | $1,602.84 | 9,765 | sonnet 8268, top 1192, haiku 279, other 26 |
| c--code-ADA | `c891fccd` | $1,318.17 | 10,208 | sonnet 8970, top 739, haiku 499 |
| c--code-ADA | `d2c3e855` | $1,295.00 | 3,274 | top 2064, sonnet 977, haiku 232, other 1 |
| c--code-ADA | `811b8d5f` | $1,292.21 | 6,263 | sonnet 4507, top 1471, haiku 285 |
| c--code-customer-ops | `77c638f9` | $1,178.90 | 4,093 | top 2319, sonnet 1557, haiku 215, other 2 |
| c--code-ADA | `cec2a721` | $1,142.40 | 5,905 | sonnet 4496, top 1087, haiku 316, other 6 |

## Agent model gate

Decisions by `~/.claude/hooks/agent_model_gate.py` in the window:

| Reason | Spawns |
|---|---|
| explicit_cheap_tier_honoured | 206 |
| absent_to_sonnet | 122 |
| haiku_tier | 74 |
| opus_required_token | 15 |
| opus_required_token_honoured | 10 |

## Trend

| Report | Est. cost | Top-tier share | Avg top cache-read | Subagent top share |
|---|---|---|---|---|
| 2026-09-15 | $30,347.80 | 72.1% | 352,819 | 41.4% |
| 2026-09-20 | $12,971.29 | 73.8% | 292,467 | 39.8% |
| 2026-09-21 | $13,412.99 | 74.5% | 291,272 | 39.7% |
| 2026-09-27 | $25,680.60 | 66.5% | 281,961 | 31.3% |

## How to read this

- Costs are list-price equivalents (see the pricing table in the script), used for mix and trend, not billing.
- `top` = Fable + Opus; `other` = any model string that matches none of the families, priced at Sonnet rates.
- Cache-read tokens per top-tier turn is the context each expensive turn re-reads; compaction at 250K bounds it.
- The rules that move these numbers live in `~/.claude/CLAUDE.md` (TOKEN DISCIPLINE) and the plan `~/.claude/plans/this-week-has-used-toasty-metcalfe.md`.
