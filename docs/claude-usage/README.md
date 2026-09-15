# Claude Code usage -- weekly forensics

Rendered by `scripts/claude_usage_forensics.py` (Windows task "Claude Usage Forensics - Weekly", Sundays 08:00, via `scripts/run-usage-forensics.cmd`). Do not hand-edit; the next run overwrites it.

- **Generated:** 2026-09-15T17:49:25Z (UTC)
- **Window:** last 7 day(s), turns dated >= 2026-09-08
- **Transcripts scanned:** 1,846 files (0 unreadable), 90,365 assistant turns in window
- **Estimated list-price cost of tokens consumed:** $30,332.75
- **Raw report:** [latest.json](latest.json) -- history under [history/](history/)

## Targets (Enhancement-1001070)

| Target | This week | Limit | Status |
|---|---|---|---|
| Top-tier (Fable/Opus) share of spend | 72.1% | <= 30.0% | **OVER** |
| Avg cache-read tokens per top-tier turn | 353,033 | <= 200,000 | **OVER** |
| Subagent spend on top-tier | 41.4% | <= 5.0% | **OVER** |

## Spend by model family

| Family | Turns | Input | Output | Cache read | Cache write | Est. cost | Share |
|---|---|---|---|---|---|---|---|
| top | 27,644 | 200,801 | 27,564,644 | 9,759,256,007 | 275,122,792 | $21,867.80 | 72.1% |
| sonnet | 62,534 | 125,068 | 16,499,917 | 23,646,298,048 | 298,881,518 | $8,462.57 | 27.9% |
| haiku | 110 | 910 | 16,527 | 7,200,400 | 1,267,739 | $2.39 | 0.0% |
| other | 77 | 0 | 0 | 0 | 0 | $0.00 | 0.0% |

Main loop $16,120.91 (53.1%) vs subagents $14,211.84 (46.9%). Avg cache-read per top-tier turn: 353,033.

## Cost by day

| Day | Est. cost |
|---|---|
| 2026-09-08 | $8,330.92 |
| 2026-09-09 | $3,949.65 |
| 2026-09-10 | $3,908.22 |
| 2026-09-11 | $2,883.75 |
| 2026-09-12 | $0.00 |
| 2026-09-13 | $0.00 |
| 2026-09-14 | $5,248.42 |
| 2026-09-15 | $6,011.79 |

## Top sessions

| Project | Session | Est. cost | Turns | Model mix |
|---|---|---|---|---|
| c--code-ADA | `d5eb0b27` | $3,781.69 | 15,585 | sonnet 13590, top 1995 |
| c--code-erpnext | `fd19579a` | $3,055.04 | 4,063 | top 4060, other 3 |
| c--code-ADA | `7b39a28f` | $2,596.37 | 7,987 | sonnet 5774, top 2213 |
| c--code-ADA | `bd3eb007` | $2,130.91 | 8,704 | sonnet 8135, top 569 |
| c--code-ADA | `4c34cb26` | $1,726.36 | 5,085 | sonnet 4186, top 878, other 21 |
| c--code-ADA | `e6f55a63` | $1,721.10 | 5,256 | sonnet 4183, top 1067, other 6 |
| c--code-ADA | `fe9c26a4` | $1,718.96 | 5,586 | sonnet 4450, top 1136 |
| c--code-ADA | `802d7e67` | $1,710.80 | 8,933 | sonnet 8230, top 693, other 10 |
| c--code-ADA | `7270f6c2` | $1,328.98 | 3,340 | sonnet 2357, top 983 |
| c--code-ADA | `345616d2` | $1,166.37 | 1,452 | top 1452 |

## Agent model gate

Decisions by `~/.claude/hooks/agent_model_gate.py` in the window:

| Reason | Spawns |
|---|---|
| explicit_cheap_tier_honoured | 4 |
| top_tier_to_sonnet | 2 |
| opus_required_token | 2 |
| haiku_tier | 1 |
| opus_required_token_capped_fable | 1 |
| absent_to_sonnet | 1 |

## Trend

| Report | Est. cost | Top-tier share | Avg top cache-read | Subagent top share |
|---|---|---|---|---|
| 2026-09-15 | $30,332.75 | 72.1% | 353,033 | 41.4% |

## How to read this

- Costs are list-price equivalents (see the pricing table in the script), used for mix and trend, not billing.
- `top` = Fable + Opus; `other` = any model string that matches none of the families, priced at Sonnet rates.
- Cache-read tokens per top-tier turn is the context each expensive turn re-reads; compaction at 250K bounds it.
- The rules that move these numbers live in `~/.claude/CLAUDE.md` (TOKEN DISCIPLINE) and the plan `~/.claude/plans/this-week-has-used-toasty-metcalfe.md`.
