#!/usr/bin/env python3
"""PreToolUse gate: route every subagent spawn to the cheapest capable model.

`.claude/rules/00-critical.md` MODEL-TIERING has declared since 2026-09-15
("Enforced, not advisory", Enhancement-1001070) that a hook rewrites every
Agent spawn. That hook lived only at `~/.claude/hooks/agent_model_gate.py` on
one Windows workstation -- outside version control, so it travelled with
neither the repo nor the operator. Measured live 2026-09-19 on macOS: no
`agent_model_gate.py` anywhere under $HOME, no `~/.claude/state/`, and
`CLAUDE_CODE_SUBAGENT_MODEL` unset -- the rule the always-loaded instructions
call binding was advisory text on every machine but one. A rule whose only
enforcement artifact is un-versioned is a wish with good intentions, so the
gate now lives in the repo and travels with the checkout.

The policy it enforces is the one 00-critical.md already states:

    prompt contains `TIER: opus-required`  -> opus   (the deliberate opt-in)
    subagent_type == Explore               -> haiku  (search/inventory work)
    model absent / fable / opus            -> sonnet (the default tier)
    model explicitly sonnet or haiku       -> unchanged (already down-tiered)

FAIL-OPEN BY CONSTRUCTION. Any malformed payload, unwritable log, unexpected
schema or unhandled exception exits 0 with no output, which leaves the spawn
exactly as the caller wrote it. A model-routing optimiser must never be able
to block work; the worst case this gate can produce is today's behaviour.

This gate would pass trivially if the host ignored `updatedInput` on a
PreToolUse allow decision -- the rewrite would silently no-op and every spawn
would keep whatever model it asked for. That is why every decision, applied or
not, is appended to `.claude/state/agent-model-gate.jsonl`: the log is the
evidence that the gate is live, and a log whose entries are all
`action: "unchanged"` across a session with Opus-tier spawns means the host
dropped the rewrite, not that the fleet was already compliant.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT_TOOLS = frozenset({"Agent", "Task"})
OPUS_OPT_IN = "TIER: opus-required"
UPTIER_FROM = frozenset({"", "fable", "opus"})
ALREADY_CHEAP = frozenset({"sonnet", "haiku"})
DEFAULT_TIER = "sonnet"
EXPLORE_TIER = "haiku"


def decide(model: str, subagent_type: str, prompt: str) -> tuple[str, str]:
    """Return (target_model, reason). Pure -- unit-testable without a host."""
    current = (model or "").strip().lower()
    sub = (subagent_type or "").strip()

    if OPUS_OPT_IN in (prompt or ""):
        return "opus", "explicit TIER: opus-required opt-in"
    if sub == "Explore":
        return EXPLORE_TIER, "Explore subagent is search/inventory work"
    if current in ALREADY_CHEAP:
        return current, f"already at {current}, left alone"
    if current in UPTIER_FROM:
        label = current or "absent"
        return DEFAULT_TIER, f"model {label} -> default implementation tier"
    return current, "unrecognised model, left alone"


def _log(record: dict) -> None:
    try:
        root = os.environ.get("CLAUDE_PROJECT_DIR") or str(Path(__file__).resolve().parents[2])
        path = Path(root) / ".claude" / "state" / "agent-model-gate.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    try:
        if payload.get("tool_name") not in AGENT_TOOLS:
            return 0

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return 0

        current = tool_input.get("model") or ""
        sub = tool_input.get("subagent_type") or ""
        prompt = tool_input.get("prompt") or ""

        target, reason = decide(current, sub, prompt)
        changed = target != (current or "").strip().lower()

        _log({
            "ts": datetime.now(timezone.utc).isoformat(),
            "subagent_type": sub,
            "requested_model": current or None,
            "routed_model": target,
            "action": "rewritten" if changed else "unchanged",
            "reason": reason,
            "description": tool_input.get("description"),
        })

        if not changed:
            return 0

        updated = dict(tool_input)
        updated["model"] = target
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": f"model-tiering: {reason}",
                "updatedInput": updated,
            }
        }))
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
