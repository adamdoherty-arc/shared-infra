# Claude Code usage -- weekly forensics

Rendered by `scripts/claude_usage_forensics.py` (Windows task "Claude Usage Forensics - Weekly", Sundays 08:00, via `scripts/run-usage-forensics.cmd`). Do not hand-edit; the next run overwrites it.

- **Generated:** 2026-09-20T12:00:40Z (UTC)
- **Window:** last 7 day(s), turns dated >= 2026-09-13
- **Transcripts scanned:** 1,609 files (0 unreadable), 40,100 assistant turns in window
- **Estimated list-price cost of tokens consumed:** $12,971.29
- **Raw report:** [latest.json](latest.json) -- history under [history/](history/)

## Targets (Enhancement-1001070)

| Target | This week | Limit | Status |
|---|---|---|---|
| Top-tier (Fable/Opus) share of spend | 73.8% | <= 30.0% | **OVER** |
| Avg cache-read tokens per top-tier turn | 292,467 | <= 200,000 | **OVER** |
| Subagent spend on top-tier | 39.8% | <= 5.0% | **OVER** |

## Spend by model family

| Family | Turns | Input | Output | Cache read | Cache write | Est. cost | Share |
|---|---|---|---|---|---|---|---|
| top | 12,608 | 106,310 | 14,793,799 | 3,687,425,773 | 156,169,757 | $9,570.45 | 73.8% |
| sonnet | 27,160 | 54,320 | 9,714,400 | 9,231,270,703 | 128,428,585 | $3,396.87 | 26.2% |
| haiku | 249 | 2,038 | 34,299 | 15,247,369 | 1,817,696 | $3.97 | 0.0% |
| other | 83 | 0 | 0 | 0 | 0 | $0.00 | 0.0% |

Main loop $7,435.34 (57.3%) vs subagents $5,535.95 (42.7%). Avg cache-read per top-tier turn: 292,467.

## Cost by day

| Day | Est. cost |
|---|---|
| 2026-09-13 | $0.00 |
| 2026-09-14 | $5,248.42 |
| 2026-09-15 | $7,576.87 |
| 2026-09-16 | $35.70 |
| 2026-09-17 | $76.08 |
| 2026-09-18 | $19.55 |
| 2026-09-19 | $14.66 |
| 2026-09-20 | $0.00 |

## Top sessions

| Project | Session | Est. cost | Turns | Model mix |
|---|---|---|---|---|
| c--code-erpnext | `fd19579a` | $3,055.04 | 4,063 | top 4060, other 3 |
| c--code-ADA | `e6f55a63` | $2,268.70 | 8,048 | sonnet 6191, top 1712, haiku 139, other 6 |
| c--code-ADA | `4c34cb26` | $1,950.09 | 5,558 | sonnet 4186, top 1351, other 21 |
| c--code-ADA | `802d7e67` | $1,710.80 | 8,933 | sonnet 8230, top 693, other 10 |
| c--code-ADA | `475ffaea` | $1,188.03 | 3,438 | sonnet 2066, top 1371, other 1 |
| c--code-ADA | `7c3115ae` | $685.12 | 3,445 | sonnet 2971, top 378, haiku 85, other 11 |
| c--code-erpnext | `c05b0775` | $579.01 | 792 | top 792 |
| c--code-ADA | `aaddd25f` | $414.62 | 1,179 | sonnet 634, top 519, haiku 25, other 1 |
| c--code-ADA | `7e306299` | $208.23 | 325 | top 324, other 1 |
| c--code-erpnext | `d2ac8ad5` | $169.06 | 309 | top 309 |

## Agent model gate

Decisions by `~/.claude/hooks/agent_model_gate.py` in the window:

| Reason | Spawns |
|---|---|
| explicit_cheap_tier_honoured | 20 |
| absent_to_sonnet | 5 |
| haiku_tier | 3 |
| top_tier_to_sonnet | 2 |
| opus_required_token | 2 |
| opus_required_token_capped_fable | 1 |

## Trend

| Report | Est. cost | Top-tier share | Avg top cache-read | Subagent top share |
|---|---|---|---|---|
| 2026-09-15 | $30,347.80 | 72.1% | 352,819 | 41.4% |
| 2026-09-20 | $12,971.29 | 73.8% | 292,467 | 39.8% |

## How to read this

- Costs are list-price equivalents (see the pricing table in the script), used for mix and trend, not billing.
- `top` = Fable + Opus; `other` = any model string that matches none of the families, priced at Sonnet rates.
- Cache-read tokens per top-tier turn is the context each expensive turn re-reads; compaction at 250K bounds it.
- The rules that move these numbers live in `~/.claude/CLAUDE.md` (TOKEN DISCIPLINE) and the plan `~/.claude/plans/this-week-has-used-toasty-metcalfe.md`.
