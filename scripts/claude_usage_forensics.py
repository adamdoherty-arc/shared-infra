#!/usr/bin/env python3
"""claude_usage_forensics.py -- stdlib-only token/cost forensics over Claude Code
transcript JSONL files. Lives in shared-infra so the weekly numbers have a
tracked, viewable home: docs/claude-usage/README.md (rendered by --markdown),
docs/claude-usage/latest.json (--json) and docs/claude-usage/history/<date>.json
(--history-dir). The Windows task "Claude Usage Forensics - Weekly" runs
scripts/run-usage-forensics.cmd every Sunday 08:00.

Phase F1 of the token-optimization plan (Legion sprint 14968, task 19856;
Enhancement-1001070). This gate would pass trivially if it only counted files
that exist and never verified the numbers inside them -- so it streams every
line, classifies by model family via substring match on message.model, and
prices each turn with the per-family USD/1M-token table below. Nothing here
calls an LLM; the only cost is CPU time walking JSONL.

Walks every <CLAUDE_HOME>/projects/**/*.jsonl (CLAUDE_HOME defaults to
~/.claude; override the roots with CLAUDE_USAGE_PROJECTS_ROOT /
CLAUDE_USAGE_GATE_LOG or --root):
  - main session transcripts:      <project>/<session>.jsonl
  - subagent transcripts:          <project>/<session>/subagents/agent-*.jsonl

Each line is one JSON object. Assistant turns carry type == "assistant" with
message.model and message.usage (input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens). Bucketed by the
timestamp field (ISO 8601, UTC 'Z').

Model family classification (substring match on message.model, in this
priority order): 'fable' -> top, 'opus' -> top, 'sonnet' -> sonnet,
'haiku' -> haiku, else -> other.

Pricing (USD per 1,000,000 tokens): input / cache-write / cache-read / output
  top:    15    / 18.75 / 1.50 / 75
  sonnet: 3     / 3.75  / 0.30 / 15
  haiku:  1     / 1.25  / 0.10 / 5
  other:  sonnet rates

Weighted cost per turn = sum(tokens_of_kind * rate_for_kind), rates applied
per 1e6 tokens. The dollar figures are list-price equivalents of the tokens
consumed, not the Max-plan invoice -- they are for trend and mix, not billing.

The report carries no secrets: project folder names, session ids, token
counts, model families, and the agent-model-gate decision reasons only.
"""

import argparse
import glob
import json
import os
import socket
import sys
import urllib.request
import urllib.error
from collections import defaultdict

CLAUDE_HOME = os.environ.get("CLAUDE_HOME") or os.path.join(os.path.expanduser("~"), ".claude")
PROJECTS_ROOT = os.environ.get("CLAUDE_USAGE_PROJECTS_ROOT") or os.path.join(CLAUDE_HOME, "projects")
GATE_LOG = os.environ.get("CLAUDE_USAGE_GATE_LOG") or os.path.join(CLAUDE_HOME, "state", "agent-model-gate.jsonl")

# USD per 1,000,000 tokens: (input, cache_write, cache_read, output)
PRICING = {
    "top":    (15.0, 18.75, 1.50, 75.0),
    "sonnet": (3.0,  3.75,  0.30, 15.0),
    "haiku":  (1.0,  1.25,  0.10, 5.0),
    "other":  (3.0,  3.75,  0.30, 15.0),  # other = sonnet rates
}

TARGETS = {
    "top_tier_share": 0.30,
    "avg_top_cache_read": 200000,
    "subagent_top_share": 0.05,
}

TARGET_LABELS = {
    "top_tier_share": ("Top-tier (Fable/Opus) share of spend", "pct"),
    "avg_top_cache_read": ("Avg cache-read tokens per top-tier turn", "int"),
    "subagent_top_share": ("Subagent spend on top-tier", "pct"),
}


def classify_family(model_name):
    if not model_name:
        return "other"
    m = model_name.lower()
    if "fable" in m:
        return "top"
    if "opus" in m:
        return "top"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "other"


def turn_cost(family, input_tokens, cache_write, cache_read, output_tokens):
    rin, rcw, rcr, rout = PRICING[family]
    return (
        (input_tokens or 0) * rin
        + (cache_write or 0) * rcw
        + (cache_read or 0) * rcr
        + (output_tokens or 0) * rout
    ) / 1_000_000.0


def iter_transcript_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


def is_subagent_path(path):
    return os.sep + "subagents" + os.sep in path


def session_key_of(path, root):
    """Return (project_dir, session_id) grouping key for a transcript file.

    Main transcripts:    <project>/<session>.jsonl               -> session_id = <session>
    Subagent transcripts: <project>/<session>/subagents/agent-*.jsonl -> session_id = <session>
    """
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    project = parts[0] if parts else "unknown"
    if len(parts) >= 4 and parts[-2] == "subagents":
        session_id = parts[-3]
    else:
        session_id = os.path.splitext(parts[-1])[0] if len(parts) >= 2 else os.path.splitext(rel)[0]
    return project, session_id


def parse_ts_date(ts):
    if not ts:
        return None
    try:
        s = ts.replace("Z", "")
        return s[:10]  # YYYY-MM-DD
    except Exception:
        return None


def within_window(ts, cutoff_date_str):
    d = parse_ts_date(ts)
    if d is None:
        return False
    return d >= cutoff_date_str


def fmt_target_value(key, value):
    kind = TARGET_LABELS[key][1]
    if kind == "pct":
        return "%.1f%%" % (float(value) * 100)
    return "{:,}".format(int(round(float(value))))


def load_history(history_dir):
    """Every dated report in history_dir, oldest first. Unreadable files are skipped."""
    rows = []
    if not history_dir or not os.path.isdir(history_dir):
        return rows
    for path in sorted(glob.glob(os.path.join(history_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rep = json.load(fh)
        except Exception:
            continue
        if not isinstance(rep, dict) or "targets" not in rep:
            continue
        rows.append((os.path.splitext(os.path.basename(path))[0], rep))
    return rows


def render_markdown(report, history_rows):
    """Render the report as the page people open. Pure function of its inputs."""
    fam = report["families"]
    tg = report["targets"]
    lines = []
    lines.append("# Claude Code usage -- weekly forensics")
    lines.append("")
    lines.append("Rendered by `scripts/claude_usage_forensics.py` (Windows task \"Claude Usage Forensics - Weekly\", "
                 "Sundays 08:00, via `scripts/run-usage-forensics.cmd`). Do not hand-edit; the next run overwrites it.")
    lines.append("")
    lines.append("- **Host:** %s" % report.get("host", "unknown"))
    lines.append("- **Generated:** %s (UTC)" % report["generated_at"])
    lines.append("- **Window:** last %d day(s), turns dated >= %s" % (report["window_days"], report["cutoff_date"]))
    lines.append("- **Transcripts scanned:** %s files (%s unreadable), %s assistant turns in window" % (
        "{:,}".format(report["files_scanned"]), report["files_failed"], "{:,}".format(report["assistant_turns_in_window"])))
    lines.append("- **Estimated list-price cost of tokens consumed:** $%s" % "{:,.2f}".format(report["total_est_cost"]))
    lines.append("- **Raw report:** [latest.json](latest.json) -- history under [history/](history/)")
    lines.append("")
    lines.append("## Targets (Enhancement-1001070)")
    lines.append("")
    lines.append("| Target | This week | Limit | Status |")
    lines.append("|---|---|---|---|")
    for key in ("top_tier_share", "avg_top_cache_read", "subagent_top_share"):
        row = tg[key]
        lines.append("| %s | %s | <= %s | %s |" % (
            TARGET_LABELS[key][0], fmt_target_value(key, row["value"]), fmt_target_value(key, row["target"]),
            "OK" if row["ok"] else "**OVER**"))
    lines.append("")
    lines.append("## Spend by model family")
    lines.append("")
    lines.append("| Family | Turns | Input | Output | Cache read | Cache write | Est. cost | Share |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for f in ("top", "sonnet", "haiku", "other"):
        fr = fam[f]
        t = fr["tokens"]
        lines.append("| %s | %s | %s | %s | %s | %s | $%s | %.1f%% |" % (
            f, "{:,}".format(fr["turns"]), "{:,}".format(t["input"]), "{:,}".format(t["output"]),
            "{:,}".format(t["cache_read"]), "{:,}".format(t["cache_write"]),
            "{:,.2f}".format(fr["est_cost"]), fr["share_of_cost"] * 100))
    mvs = report["main_vs_subagent"]
    lines.append("")
    lines.append("Main loop $%s (%.1f%%) vs subagents $%s (%.1f%%). Avg cache-read per top-tier turn: %s." % (
        "{:,.2f}".format(mvs["main_est_cost"]), mvs["main_share"] * 100,
        "{:,.2f}".format(mvs["subagent_est_cost"]), mvs["subagent_share"] * 100,
        "{:,}".format(int(round(report["avg_top_cache_read_per_turn"])))))
    lines.append("")
    lines.append("## Cost by day")
    lines.append("")
    lines.append("| Day | Est. cost |")
    lines.append("|---|---|")
    for d, c in report["cost_by_day"].items():
        lines.append("| %s | $%s |" % (d, "{:,.2f}".format(c)))
    lines.append("")
    lines.append("## Top sessions")
    lines.append("")
    lines.append("| Project | Session | Est. cost | Turns | Model mix |")
    lines.append("|---|---|---|---|---|")
    for row in report["top_sessions"]:
        mix = ", ".join("%s %d" % (k, v) for k, v in sorted(row["model_mix"].items(), key=lambda kv: -kv[1]))
        lines.append("| %s | `%s` | $%s | %s | %s |" % (
            row["project"], row["session_id"][:8], "{:,.2f}".format(row["est_cost"]), "{:,}".format(row["turns"]), mix))
    lines.append("")
    lines.append("## Agent model gate")
    lines.append("")
    gate = report["gate"]
    if gate["present"] and gate["counts_by_reason"]:
        lines.append("Decisions by `~/.claude/hooks/agent_model_gate.py` in the window:")
        lines.append("")
        lines.append("| Reason | Spawns |")
        lines.append("|---|---|")
        for reason, n in sorted(gate["counts_by_reason"].items(), key=lambda kv: -kv[1]):
            lines.append("| %s | %d |" % (reason, n))
    elif gate["present"]:
        lines.append("Gate log present, no decisions in the window.")
    else:
        lines.append("Gate log not found -- the PreToolUse hook is not writing `agent-model-gate.jsonl`.")
    lines.append("")
    lines.append("## Trend")
    lines.append("")
    if history_rows:
        lines.append("| Report | Est. cost | Top-tier share | Avg top cache-read | Subagent top share |")
        lines.append("|---|---|---|---|---|")
        for name, rep in history_rows:
            t = rep["targets"]
            lines.append("| %s | $%s | %s | %s | %s |" % (
                name, "{:,.2f}".format(rep.get("total_est_cost", 0.0)),
                fmt_target_value("top_tier_share", t["top_tier_share"]["value"]),
                fmt_target_value("avg_top_cache_read", t["avg_top_cache_read"]["value"]),
                fmt_target_value("subagent_top_share", t["subagent_top_share"]["value"])))
    else:
        lines.append("No history yet.")
    lines.append("")
    lines.append("## How to read this")
    lines.append("")
    lines.append("- Costs are list-price equivalents (see the pricing table in the script), used for mix and trend, not billing.")
    lines.append("- `top` = Fable + Opus; `other` = any model string that matches none of the families, priced at Sonnet rates.")
    lines.append("- Cache-read tokens per top-tier turn is the context each expensive turn re-reads; compaction at 250K bounds it.")
    lines.append("- The rules that move these numbers live in `~/.claude/CLAUDE.md` (TOKEN DISCIPLINE) and the plan "
                 "`~/.claude/plans/this-week-has-used-toasty-metcalfe.md`.")
    lines.append("")
    return "\n".join(lines)


def write_text(path, text):
    out_dir = os.path.dirname(path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


WEBHOOK_ENV_NAMES = ("CLAUDE_USAGE_DISCORD_WEBHOOK", "DISCORD_INFRA_WEBHOOK", "AUTOHEAL_DISCORD_WEBHOOK")
REPO_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def resolve_webhook():
    """Process env first, then shared-infra/.env (the Windows task has no env of its own).
    The value is returned for the POST only and is never printed."""
    for name in WEBHOOK_ENV_NAMES:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    if not os.path.exists(REPO_ENV_FILE):
        return ""
    found = {}
    with open(REPO_ENV_FILE, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if k in WEBHOOK_ENV_NAMES:
                found[k] = v.strip().strip('"').strip("'")
    for name in WEBHOOK_ENV_NAMES:
        if found.get(name):
            return found[name]
    return ""


def main():
    ap = argparse.ArgumentParser(description="Claude Code usage/cost forensics (stdlib only)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", type=str, default=None, help="write JSON report to this path")
    ap.add_argument("--markdown", type=str, default=None, help="render the report as Markdown to this path")
    ap.add_argument("--history-dir", type=str, default=None,
                    help="also write <history-dir>/<YYYY-MM-DD>.json and include every file there in the trend table")
    ap.add_argument("--discord", action="store_true", help="post one summary line to CLAUDE_USAGE_DISCORD_WEBHOOK")
    ap.add_argument("--baseline", type=str, default=None, help="compare against a prior report JSON")
    ap.add_argument("--root", type=str, default=PROJECTS_ROOT, help="override projects root (testing)")
    args = ap.parse_args()

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.days)
    cutoff_date_str = cutoff.strftime("%Y-%m-%d")

    family_turns = defaultdict(int)
    family_tokens = defaultdict(lambda: defaultdict(int))  # family -> kind -> tokens
    family_cost = defaultdict(float)
    main_cost = 0.0
    sub_cost = 0.0
    sub_top_cost = 0.0
    sub_total_cost = 0.0
    top_cache_read_sum = 0
    top_turns_for_cache = 0
    day_cost = defaultdict(float)
    sessions = defaultdict(lambda: {"cost": 0.0, "turns": 0, "families": defaultdict(int), "project": ""})

    total_lines = 0
    parsed_lines = 0
    files_scanned = 0
    files_failed = 0

    root = args.root
    if not os.path.isdir(root):
        print("ERROR: projects root not found: %s" % root, file=sys.stderr)
        sys.exit(2)

    for path in iter_transcript_files(root):
        files_scanned += 1
        subagent = is_subagent_path(path)
        project, session_id = session_key_of(path, root)
        skey = (project, session_id)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    total_lines += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    msg = obj.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    ts = obj.get("timestamp")
                    if not within_window(ts, cutoff_date_str):
                        continue
                    parsed_lines += 1

                    model_name = msg.get("model")
                    family = classify_family(model_name)

                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_write = usage.get("cache_creation_input_tokens", 0) or 0

                    cost = turn_cost(family, input_tokens, cache_write, cache_read, output_tokens)

                    family_turns[family] += 1
                    family_tokens[family]["input"] += input_tokens
                    family_tokens[family]["output"] += output_tokens
                    family_tokens[family]["cache_read"] += cache_read
                    family_tokens[family]["cache_write"] += cache_write
                    family_cost[family] += cost

                    if family == "top":
                        top_cache_read_sum += cache_read
                        top_turns_for_cache += 1

                    if subagent:
                        sub_cost += cost
                        sub_total_cost += cost
                        if family == "top":
                            sub_top_cost += cost
                    else:
                        main_cost += cost

                    day = parse_ts_date(ts)
                    if day:
                        day_cost[day] += cost

                    s = sessions[skey]
                    s["cost"] += cost
                    s["turns"] += 1
                    s["families"][family] += 1
                    s["project"] = project
        except Exception:
            files_failed += 1
            continue

    total_cost = sum(family_cost.values())

    families_report = {}
    for fam in ("top", "sonnet", "haiku", "other"):
        toks = family_tokens[fam]
        families_report[fam] = {
            "turns": family_turns[fam],
            "tokens": {
                "input": toks["input"],
                "output": toks["output"],
                "cache_read": toks["cache_read"],
                "cache_write": toks["cache_write"],
            },
            "est_cost": round(family_cost[fam], 4),
            "share_of_cost": round((family_cost[fam] / total_cost) if total_cost else 0.0, 4),
        }

    avg_top_cache_read = (top_cache_read_sum / top_turns_for_cache) if top_turns_for_cache else 0.0

    top_sessions = sorted(sessions.items(), key=lambda kv: kv[1]["cost"], reverse=True)[:10]
    top_sessions_report = []
    for (project, session_id), s in top_sessions:
        model_mix = {k: v for k, v in s["families"].items() if v > 0}
        top_sessions_report.append({
            "project": project,
            "session_id": session_id,
            "est_cost": round(s["cost"], 4),
            "turns": s["turns"],
            "model_mix": model_mix,
        })

    subagent_top_share = (sub_top_cost / sub_total_cost) if sub_total_cost else 0.0

    targets_block = {
        "top_tier_share": {
            "value": round(families_report["top"]["share_of_cost"], 4),
            "target": TARGETS["top_tier_share"],
            "ok": families_report["top"]["share_of_cost"] <= TARGETS["top_tier_share"],
        },
        "avg_top_cache_read": {
            "value": round(avg_top_cache_read, 1),
            "target": TARGETS["avg_top_cache_read"],
            "ok": avg_top_cache_read <= TARGETS["avg_top_cache_read"],
        },
        "subagent_top_share": {
            "value": round(subagent_top_share, 4),
            "target": TARGETS["subagent_top_share"],
            "ok": subagent_top_share <= TARGETS["subagent_top_share"],
        },
    }

    gate_block = {"present": False, "counts_by_reason": {}}
    if os.path.isfile(GATE_LOG):
        gate_block["present"] = True
        counts = defaultdict(int)
        try:
            with open(GATE_LOG, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    ts = obj.get("ts")
                    if ts and ts[:10] < cutoff_date_str:
                        continue
                    reason = obj.get("reason", "unknown")
                    counts[reason] += 1
        except Exception:
            pass
        gate_block["counts_by_reason"] = dict(counts)

    report = {
        "host": socket.gethostname(),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": args.days,
        "cutoff_date": cutoff_date_str,
        "files_scanned": files_scanned,
        "files_failed": files_failed,
        "lines_scanned": total_lines,
        "assistant_turns_in_window": parsed_lines,
        "families": families_report,
        "total_est_cost": round(total_cost, 4),
        "main_vs_subagent": {
            "main_est_cost": round(main_cost, 4),
            "subagent_est_cost": round(sub_cost, 4),
            "main_share": round((main_cost / total_cost) if total_cost else 0.0, 4),
            "subagent_share": round((sub_cost / total_cost) if total_cost else 0.0, 4),
        },
        "avg_top_cache_read_per_turn": round(avg_top_cache_read, 1),
        "cost_by_day": {k: round(v, 4) for k, v in sorted(day_cost.items())},
        "top_sessions": top_sessions_report,
        "subagent_top_share_of_subagent_spend": round(subagent_top_share, 4),
        "targets": targets_block,
        "gate": gate_block,
    }

    if args.baseline and os.path.isfile(args.baseline):
        try:
            with open(args.baseline, "r", encoding="utf-8") as fh:
                baseline = json.load(fh)
            report["baseline_compare"] = {
                "baseline_generated_at": baseline.get("generated_at"),
                "baseline_total_est_cost": baseline.get("total_est_cost"),
                "delta_total_est_cost": round(report["total_est_cost"] - (baseline.get("total_est_cost") or 0.0), 4),
                "baseline_top_tier_share": (baseline.get("families", {}).get("top", {}) or {}).get("share_of_cost"),
                "delta_top_tier_share": round(
                    families_report["top"]["share_of_cost"] - ((baseline.get("families", {}).get("top", {}) or {}).get("share_of_cost") or 0.0),
                    4,
                ),
            }
        except Exception as e:
            report["baseline_compare_error"] = str(e)

    print("Claude Usage Forensics -- window: last %d day(s), cutoff %s" % (args.days, cutoff_date_str))
    print("Files scanned: %d (failed: %d), lines scanned: %d, assistant turns in window: %d" % (
        files_scanned, files_failed, total_lines, parsed_lines))
    print("")
    print("%-8s %8s %14s %14s %14s %14s %12s %8s" % (
        "family", "turns", "input", "output", "cache_read", "cache_write", "est_cost", "share"))
    for fam in ("top", "sonnet", "haiku", "other"):
        fr = families_report[fam]
        print("%-8s %8d %14d %14d %14d %14d %12.2f %7.1f%%" % (
            fam, fr["turns"], fr["tokens"]["input"], fr["tokens"]["output"],
            fr["tokens"]["cache_read"], fr["tokens"]["cache_write"], fr["est_cost"],
            fr["share_of_cost"] * 100))
    print("")
    print("TOTAL est_cost: $%.2f" % report["total_est_cost"])
    print("main vs subagent: main=$%.2f (%.1f%%)  subagent=$%.2f (%.1f%%)" % (
        report["main_vs_subagent"]["main_est_cost"], report["main_vs_subagent"]["main_share"] * 100,
        report["main_vs_subagent"]["subagent_est_cost"], report["main_vs_subagent"]["subagent_share"] * 100))
    print("avg cache_read per top-tier turn: %.0f" % avg_top_cache_read)
    print("subagent spend on top-tier as %% of subagent spend: %.1f%%" % (subagent_top_share * 100))
    print("")
    print("Cost by day:")
    for d, c in sorted(day_cost.items()):
        print("  %s  $%.2f" % (d, c))
    print("")
    print("Top 10 sessions by est_cost:")
    print("%-40s %10s %8s %s" % ("project", "est_cost", "turns", "model_mix"))
    for row in top_sessions_report:
        print("%-40s %10.2f %8d %s" % (row["project"][:40], row["est_cost"], row["turns"], row["model_mix"]))
    print("")
    print("Targets:")
    for k, v in targets_block.items():
        status = "OK" if v["ok"] else "FAIL"
        print("  %-22s value=%s target=%s [%s]" % (k, v["value"], v["target"], status))
    print("")
    if gate_block["present"]:
        print("Agent-model-gate counts by reason (window): %s" % gate_block["counts_by_reason"])
    else:
        print("Agent-model-gate log not found: %s" % GATE_LOG)

    if "baseline_compare" in report:
        print("")
        print("Baseline compare: delta_total_est_cost=$%.2f  delta_top_tier_share=%.4f" % (
            report["baseline_compare"]["delta_total_est_cost"], report["baseline_compare"]["delta_top_tier_share"]))

    if args.json:
        write_text(args.json, json.dumps(report, indent=2) + "\n")
        print("")
        print("Report written to: %s" % args.json)

    history_rows = []
    if args.history_dir:
        dated = os.path.join(args.history_dir, now.strftime("%Y-%m-%d") + ".json")
        write_text(dated, json.dumps(report, indent=2) + "\n")
        print("History written to: %s" % dated)
        history_rows = load_history(args.history_dir)

    if args.markdown:
        write_text(args.markdown, render_markdown(report, history_rows))
        print("Markdown written to: %s" % args.markdown)

    if args.discord:
        line = ("Claude Usage (last %dd): total=$%.2f top-tier=%.1f%% (target<=30%%) "
                "avg_top_cache_read=%.0f (target<=200000) subagent_top_share=%.1f%% (target<=5%%)") % (
            args.days, report["total_est_cost"], families_report["top"]["share_of_cost"] * 100,
            avg_top_cache_read, subagent_top_share * 100)
        webhook = resolve_webhook()
        if not webhook:
            print("")
            print("No Discord webhook (CLAUDE_USAGE_DISCORD_WEBHOOK / DISCORD_INFRA_WEBHOOK / AUTOHEAL_DISCORD_WEBHOOK); summary line:")
            print(line)
        else:
            payload = json.dumps({"content": line}).encode("utf-8")
            req = urllib.request.Request(webhook, data=payload, headers={
                "Content-Type": "application/json",
                # Cloudflare returns 1010 for urllib's default User-Agent.
                "User-Agent": "shared-infra-claude-usage-forensics/1.0",
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read()
                print("")
                print("Posted to Discord webhook.")
            except urllib.error.URLError as e:
                print("")
                print("Discord post FAILED: %s" % e)
                print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
