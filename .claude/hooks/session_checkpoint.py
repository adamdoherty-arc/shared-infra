#!/usr/bin/env python3
"""Session checkpoint: what this session was doing, written before every compaction
and re-injected after it (PreCompact + SessionStart hooks, `.claude/settings.json`).

Ported verbatim from ADA's `scripts/session_checkpoint.py` (WS7, shared-infra
operating-pattern mold, 2026-09-25) with one path fix: this copy lives at
`.claude/hooks/session_checkpoint.py` so the repo root is `parents[2]`, not
`parents[1]`.

Why (measured 2026-09-15..21): after an autocompact the harness re-injects every
instruction file but the SUMMARY is the only carrier of the working state, and the
owner reported sessions that "lost context and spent time figuring out what they were
doing". The sessions that survived compaction were the ones that had written their
state to disk (`.claude/state/bitcoin-lab/PASS*.md`, plan files). This makes that the
default: a small, deterministic, LLM-free snapshot of the ground truth -- HEAD, dirty
paths, the newest plan file, the last user asks -- is written to
`.claude/state/session-checkpoints/<session_id>.md` right before the compaction and
printed to stdout on the SessionStart(compact|resume) that follows, which the host
injects as context.

Stdlib only, fail-open: any error exits 0 with no output. Output is capped so the
checkpoint can never become the thing that refills the window.

This hook would pass trivially if PreCompact ran but SessionStart never read the file
back (the checkpoint would be written and nobody would see it), or if the transcript
parser silently returned no prompts (an empty "last asks" section reads like a quiet
session, not a broken parser). `backend/tests/test_hook_sabotage.py::TestSessionCheckpoint`
therefore round-trips a synthetic transcript through both events and asserts the user
asks come back out.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_CHARS = 3500          # hard cap on what SessionStart injects
MAX_PROMPTS = 6
PROMPT_CHARS = 280
ASSISTANT_CHARS = 500
DIRTY_PATHS = 25


def _project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]  # .claude/hooks/session_checkpoint.py -> repo root


def _state_dir(root: Path) -> Path:
    return root / ".claude" / "state" / "session-checkpoints"


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                             text=True, timeout=15, encoding="utf-8", errors="replace")
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _newest_plan() -> tuple[str, str]:
    """Newest file under ~/.claude/plans touched in the last 3 days -> (path, first heading)."""
    try:
        plans = glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "plans", "*.md"))
        if not plans:
            return "", ""
        newest = max(plans, key=os.path.getmtime)
        age_h = (datetime.now().timestamp() - os.path.getmtime(newest)) / 3600
        if age_h > 72:
            return "", ""
        heading = ""
        with open(newest, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#"):
                    heading = line.strip("# \n")[:120]
                    break
        return newest, heading
    except Exception:
        return "", ""


def _strip_reminders(text: str) -> str:
    """Drop injected <system-reminder> blocks, keep what the human typed."""
    open_tag, close_tag = "<system-reminder>", "</system-reminder>"
    while open_tag in text:
        start = text.find(open_tag)
        end = text.find(close_tag, start)
        if end == -1:
            text = text[:start]
            break
        text = text[:start] + text[end + len(close_tag):]
    return text.strip()


def _text_of(content) -> str:
    """User/assistant message content -> plain text, ignoring tool blocks."""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return ""
    return _strip_reminders("\n".join(p for p in parts if p))


def read_transcript(path: str) -> tuple[list[str], str]:
    """Return (last user prompts, last assistant text) from a Claude Code JSONL transcript.
    Skips tool_result turns, hook/system events and compact summaries."""
    prompts: list[str] = []
    last_assistant = ""
    if not path or not os.path.exists(path):
        return prompts, last_assistant
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("isCompactSummary") or d.get("isMeta"):
                continue
            m = d.get("message") or {}
            if d.get("type") == "user":
                content = m.get("content")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                t = _text_of(content)
                if t and not t.startswith("[Request interrupted"):
                    prompts.append(t[:PROMPT_CHARS])
            elif d.get("type") == "assistant":
                t = _text_of(m.get("content"))
                if t:
                    last_assistant = t[-ASSISTANT_CHARS:]
    return prompts[-MAX_PROMPTS:], last_assistant


def build_checkpoint(root: Path, session_id: str, transcript_path: str, trigger: str) -> str:
    head = _git(root, "rev-parse", "--short", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    dirty = [ln for ln in _git(root, "status", "--porcelain").splitlines() if ln.strip()]
    plan, heading = _newest_plan()
    prompts, last_assistant = read_transcript(transcript_path)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Session checkpoint ({trigger}) -- {stamp}",
        f"session: {session_id}  repo: {root}  HEAD: {head} ({branch})",
        f"dirty paths: {len(dirty)}" + (" -- first ones:" if dirty else ""),
    ]
    lines += [f"  {ln}" for ln in dirty[:DIRTY_PATHS]]
    if plan:
        lines.append(f"newest plan file: {plan}" + (f" -- {heading}" if heading else ""))
    lines.append(f"last {len(prompts)} user asks (oldest first):")
    lines += [f"  {i + 1}. {p.replace(chr(10), ' ')}" for i, p in enumerate(prompts)]
    if last_assistant:
        lines.append("last assistant text: " + last_assistant.replace("\n", " "))
    lines.append("Resume from THIS, not from the summary: re-read the plan file and "
                 "`git status` before acting.")
    return "\n".join(lines)[:MAX_CHARS]


def main(argv: list[str]) -> int:
    try:
        event = argv[1] if len(argv) > 1 else ""
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        root = _project_root()
        sid = str(payload.get("session_id") or "unknown")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:64]
        path = _state_dir(root) / f"{safe}.md"

        if event == "precompact":
            text = build_checkpoint(root, sid, str(payload.get("transcript_path") or ""),
                                    str(payload.get("trigger") or "auto"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return 0

        if event == "sessionstart":
            source = str(payload.get("source") or "")
            if source not in ("compact", "resume"):
                return 0
            if path.exists():
                sys.stdout.write(path.read_text(encoding="utf-8")[:MAX_CHARS] + "\n")
            return 0
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
