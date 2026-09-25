#!/usr/bin/env python3
"""Stub detector — flags NEVER-SHIP-STUBS phrasings before they land.

`~/.claude/CLAUDE.md` "NEVER SHIP STUBS" bans a specific pattern: a
rule-based/heuristic/placeholder implementation shipped in place of a
defined production path, labeled to look like a design choice
("heuristic_v1", "TODO: swap for real X", "wire when the service is
stable", "for now" in a return value). This scans the shared-infra-owned
source trees for those phrasings and fails a ratchet if the count grows,
the same shape as ADA's `scripts/audits/*.py` category auditors
(`.audit-baselines/*.json` ratchets, `.claude/rules/00-critical.md`
"a finding with no hook/ratchet/test is a wish").

Scope: infractl/, bifrost/, bifrost-metrics-exporter/, scripts/,
vllm_wedge_monitor.py -- the code surfaces this repo actually owns.
`*.py`, `*.sh`, `*.yml` files.

Usage:
    python scripts/audits/stub_detector.py                  # human output
    python scripts/audits/stub_detector.py --json            # machine output
    python scripts/audits/stub_detector.py --baseline PATH   # ratchet check, exit 1 if any count grew
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_ROOTS = (
    "infractl",
    "bifrost",
    "bifrost-metrics-exporter",
    "scripts",
    "vllm_wedge_monitor.py",
)

EXCLUDE_DIR_NAMES = {"__pycache__", ".git", "node_modules", "tests"}
# The detector's own patterns list and this docstring quote the banned
# phrases verbatim to explain them -- exclude this file and the tests dir
# from self-matching so the ratchet doesn't chase its own tail.
EXCLUDE_FILES = {Path(__file__).resolve()}

SCAN_SUFFIXES = {".py", ".sh", ".yml", ".yaml"}

# (label, compiled regex). Word-boundary / anchored where sensible to avoid
# matching unrelated prose (e.g. "for now" is common English -- restricted to
# the "X for now" placeholder shape, not every occurrence of the two words).
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("heuristic_v1_or_versioned_stub", re.compile(r"heuristic_v[0-9]+\b", re.I)),
    ("todo_swap_for_real", re.compile(r"TODO:?\s*swap\s+for\s+real", re.I)),
    ("placeholder_until", re.compile(r"placeholder\s+until\b", re.I)),
    ("will_wire_in_followup", re.compile(r"will\s+wire\s+(?:the\s+)?real\b", re.I)),
    ("wire_when_stable", re.compile(r"wire\s+when\s+the\s+\S+\s+(?:is\s+)?stable\b", re.I)),
    ("stays_zero_until_sprint", re.compile(r"stays?\s+(?:0|zero|\[\]|none)\s+until\b", re.I)),
    ("for_now_placeholder", re.compile(r"(?:=\s*(?:\[\]|\{\}|None|0)\s*)?#.*\bfor\s+now\b", re.I)),
    ("source_heuristic_only", re.compile(r"source['\"]?\s*[:=]\s*['\"]heuristic_only['\"]", re.I)),
    ("heuristic_first_llm_second_scoping", re.compile(r"heuristic-first,?\s*llm-second", re.I)),
    (
        "next_sprint_wave_deferral",
        re.compile(r"\b(?:next|future)\s+(?:sprint|wave)\b.*\b(?:lands?|ships?|wires?)\b", re.I),
    ),
]


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for rel in SCAN_ROOTS:
        p = REPO_ROOT / rel
        if p.is_file():
            if p.suffix in SCAN_SUFFIXES and p.resolve() not in EXCLUDE_FILES:
                files.append(p)
            continue
        if not p.is_dir():
            continue
        for f in p.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix not in SCAN_SUFFIXES:
                continue
            if any(part in EXCLUDE_DIR_NAMES for part in f.parts):
                continue
            if f.resolve() in EXCLUDE_FILES:
                continue
            files.append(f)
    return files


def scan() -> dict:
    """Return {rule_label: count} plus a `hits` list of {rule, file, line, text}."""
    counts: dict[str, int] = {label: 0 for label, _ in PATTERNS}
    hits: list[dict] = []
    for f in _iter_files():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    counts[label] += 1
                    hits.append(
                        {
                            "rule": label,
                            "file": str(f.relative_to(REPO_ROOT)),
                            "line": lineno,
                            "text": line.strip()[:200],
                        }
                    )
    return {"counts": counts, "hits": hits, "total": sum(counts.values())}


def check_ratchet(result: dict, baseline_path: Path) -> tuple[bool, list[str]]:
    """Returns (ok, messages). ok=False if any rule's count grew vs baseline."""
    if not baseline_path.exists():
        return True, [f"no baseline at {baseline_path}, treating current counts as the new baseline"]
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"could not parse baseline {baseline_path}: {exc}"]
    baseline_counts = baseline.get("counts", {})
    messages = []
    ok = True
    for label, count in result["counts"].items():
        prior = baseline_counts.get(label, 0)
        if count > prior:
            ok = False
            messages.append(f"REGRESSION: {label} grew {prior} -> {count}")
    return ok, messages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--baseline", type=Path, default=REPO_ROOT / ".audit-baselines" / "stubs.json")
    parser.add_argument(
        "--write-baseline", action="store_true", help="write current counts as the new baseline and exit 0"
    )
    args = parser.parse_args()

    result = scan()

    if args.write_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps({"counts": result["counts"]}, indent=2) + "\n", encoding="utf-8")
        print(f"wrote baseline: {args.baseline}")
        return 0

    ok, messages = check_ratchet(result, args.baseline)

    if args.json:
        print(json.dumps({"ok": ok, "messages": messages, **result}, indent=2))
    else:
        print(f"stub_detector: {result['total']} total hits across {len(PATTERNS)} rules")
        for label, count in sorted(result["counts"].items(), key=lambda kv: -kv[1]):
            if count:
                print(f"  {label}: {count}")
        for h in result["hits"][:30]:
            print(f"    {h['file']}:{h['line']}: [{h['rule']}] {h['text']}")
        for m in messages:
            print(m)
        print("PASS" if ok else "FAIL")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
