#!/usr/bin/env python3
"""Render docs/claude-usage/README.md as a cross-host index.

Each machine that runs Claude Code writes its own page under
docs/claude-usage/hosts/<hostname>/ (README.md, latest.json, history/), because
transcripts under ~/.claude/projects are per machine and a single shared
latest.json would just be overwritten by whichever host committed last
(measured 2026-09-21: the workstation holding the settings showed <$400/day
while the week's real spend was on a second machine with no page at all).
This index reads every hosts/*/latest.json and writes one table so the owner
sees all machines on one page. Stdlib only, zero tokens.
"""
from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "claude-usage")


def _pct(v):
    return "%.1f%%" % (100 * v) if isinstance(v, (int, float)) else "n/a"


def _num(v, spec="", prefix=""):
    return prefix + format(v, spec) if isinstance(v, (int, float)) else "n/a"


def render() -> str:
    rows = []
    for latest in sorted(glob.glob(os.path.join(ROOT, "hosts", "*", "latest.json"))):
        host = os.path.basename(os.path.dirname(latest))
        try:
            with open(latest, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as exc:  # a broken page must show up as broken, not vanish
            rows.append(f"| `{host}` | unreadable latest.json ({exc.__class__.__name__}) | | | | | |")
            continue
        t = d.get("targets") or {}
        top = (t.get("top_tier_share") or {}).get("value")
        cr = (t.get("avg_top_cache_read") or {}).get("value")
        sub = (t.get("subagent_top_share") or {}).get("value")
        cost = d.get("total_est_cost")
        gen = str(d.get("generated_at", ""))[:16].replace("T", " ")
        flags = " ".join("OVER" if not (t.get(k) or {}).get("ok", True) else "ok"
                         for k in ("top_tier_share", "avg_top_cache_read", "subagent_top_share"))
        rows.append(
            f"| [`{host}`](hosts/{host}/README.md) | {gen} | {d.get('window_days', '?')}d | "
            f"{_num(cost, ',.0f', '$')} | {_pct(top)} | {_num(cr, ',.0f')} | {_pct(sub)} | {flags} |")
    if not rows:
        rows.append("| (no hosts have reported yet) | | | | | | | |")
    return "\n".join([
        "# Claude Code usage -- all machines",
        "",
        "One row per machine that runs Claude Code. Each host's weekly page lives under",
        "`hosts/<hostname>/` and is written by that machine's own scheduled run",
        "(`scripts/run-usage-forensics.cmd` on Windows, `scripts/run-usage-forensics.sh` on",
        "macOS/Linux -- install with the matching `scripts/install-usage-forensics-*`).",
        "Transcripts are per machine, so a host that never runs the job is a host whose",
        "spend is invisible here. Targets: top-tier share <= 30%, avg top-tier cache-read",
        "<= 200K tokens/turn, subagent spend on Opus/Fable <= 5%.",
        "",
        "| host | generated (UTC) | window | est. cost | top-tier share | avg top cache-read | subagent top share | targets |",
        "|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "_Rendered by `scripts/claude_usage_index.py`; re-run it after any host page changes._",
        "",
    ])


def main() -> int:
    out = os.path.join(ROOT, "README.md")
    text = render()
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    sys.stdout.write(f"wrote {out} ({text.count(chr(10))} lines)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
