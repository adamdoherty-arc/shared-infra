# Claude Code usage -- all machines

One row per machine that runs Claude Code. Each host's weekly page lives under
`hosts/<hostname>/` and is written by that machine's own scheduled run
(`scripts/run-usage-forensics.cmd` on Windows, `scripts/run-usage-forensics.sh` on
macOS/Linux -- install with the matching `scripts/install-usage-forensics-*`).
Transcripts are per machine, so a host that never runs the job is a host whose
spend is invisible here. Targets: top-tier share <= 30%, avg top-tier cache-read
<= 200K tokens/turn, subagent spend on Opus/Fable <= 5%.

| host | generated (UTC) | window | est. cost | top-tier share | avg top cache-read | subagent top share | targets |
|---|---|---|---|---|---|---|---|
| [`VENGEANCE`](hosts/VENGEANCE/README.md) | 2026-09-27 12:00 | 7d | $25,681 | 66.5% | 281,961 | 31.3% | OVER OVER OVER |

_Rendered by `scripts/claude_usage_index.py`; re-run it after any host page changes._
