#!/usr/bin/env python3
"""PreToolUse gate: block unregistered restarts of the shared LLM stack.

Bifrost holds provider/key config in TWO places (`bifrost/config.json` +
`bifrost/config.db`), and `qwen38-chat` is the single GPU-bound chat engine
serving ADA/Legion/A-finance at 92% of all tokens (2026-09-25 ground truth,
`docs/DECISIONS.md`). A bare `docker restart shared-bifrost` skips the
binding restart sequence (stop bifrost-autoheal -> stop shared-bifrost ->
`sync_vk_allowlists.py` -> start -> health/smoke -> start bifrost-autoheal,
see `CLAUDE.md` "Provider state lives in TWO places" + WS7 rule
`.claude/rules/30-docker.md`) and can leave the SQLite mirror and JSON seed
out of sync, or leave bifrost-autoheal racing a manual restart. A bare
restart of `qwen38-chat` or `vllm-embed` interrupts every consumer with no
warning and no ledger row, the same class of invisible-restart problem
ADA's `restart_gate.py` fixes for its own containers.

The fix is not to forbid restarting these containers -- the fleet has to
recover from wedges and config pushes. It is to force every restart through
`scripts/bifrost_restart.sh` (the sequenced, verified path) or an explicit
operator opt-out (`INFRA_RESTART_OK=1` in the command's environment), so an
unsequenced restart becomes something that never reaches the containers.

Exit 0 = allow. Exit 2 = block with the stderr text shown to the agent.
Reads the PreToolUse Bash payload JSON on stdin: {"tool_name": "Bash",
"tool_input": {"command": "...", ...}}.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

GATED_CONTAINERS = ("shared-bifrost", "qwen38-chat", "vllm-embed")

# Bare `docker restart|stop|start NAME` (any flags before the verb, e.g.
# `docker -H ... restart`) and `docker compose ... up` (a recreate is a
# restart for a service whose env/image changed).
_RAW_LIFECYCLE = re.compile(r"^docker\s+(?:-\S+\s+)*(restart|stop|start)\b", re.I)
_COMPOSE_UP = re.compile(r"^docker\s+compose\b[^|;&\n]*\bup\b", re.I)

# A search for the phrase is not the phrase (same lesson ADA's restart_gate.py
# learned on its first live run: it blocked `grep -n "docker restart" *.md`).
# Only a statement whose own leader is `docker` (or a shell wrapper around
# one) counts -- split on shell separators and check each statement's leader.
_SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
_SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh", "cmd", "cmd.exe", "powershell", "pwsh"})
_PREFIX_TOKENS = frozenset({"sudo", "time", "nohup", "exec"})


def _statements(cmd: str) -> list[str]:
    return [s.strip() for s in _SEPARATORS.split(cmd) if s.strip()]


def _leader_is_docker(stmt: str) -> bool:
    try:
        tokens = shlex.split(stmt, posix=True)
    except ValueError:
        tokens = stmt.split()
    # Drop VAR=value env-prefix tokens and known wrappers/prefixes.
    i = 0
    while i < len(tokens) and ("=" in tokens[i].split()[0] if tokens[i] else False):
        i += 1
    while i < len(tokens) and tokens[i] in _PREFIX_TOKENS:
        i += 1
    if i < len(tokens) and tokens[i] in _SHELL_WRAPPERS:
        # e.g. `bash -c "docker restart X"` -- not our concern here, the
        # gate matches the literal statement text either way via regex below.
        return False
    return i < len(tokens) and tokens[i] == "docker"


# The sanctioned path and the deliberate operator override never get blocked.
_SANCTIONED_MARKERS = (
    "scripts/bifrost_restart.sh",
    "scripts\\bifrost_restart.sh",
    "INFRA_RESTART_OK=1",
)

GUIDANCE = (
    "[bifrost-restart-gate] BLOCKED: this command restarts/stops/starts "
    "{targets} outside the sequenced path.\n"
    "Bifrost's config.json/config.db can desync and bifrost-autoheal can "
    "race a manual restart (see CLAUDE.md 'Provider state lives in TWO "
    "places' and .claude/rules/30-docker.md).\n"
    "Use the binding restart sequence instead:\n"
    "  bash scripts/bifrost_restart.sh\n"
    "or, for a deliberate manual restart outside that script, opt out "
    "explicitly:\n"
    "  INFRA_RESTART_OK=1 <your command>\n"
)


def _command_from_stdin() -> str:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    cmd = tool_input.get("command") or ""
    return cmd if isinstance(cmd, str) else ""


def _targets_in(cmd: str) -> list[str]:
    return [c for c in GATED_CONTAINERS if c in cmd]


def is_blocked(cmd: str) -> tuple[bool, list[str]]:
    """Pure decision function -- unit-testable without stdin/JSON plumbing."""
    if not cmd:
        return False, []
    if any(marker in cmd for marker in _SANCTIONED_MARKERS):
        return False, []

    docker_statements = [s for s in _statements(cmd) if _leader_is_docker(s)]
    if not docker_statements:
        return False, []

    lifecycle_statements = [s for s in docker_statements if _RAW_LIFECYCLE.search(s) or _COMPOSE_UP.search(s)]
    if not lifecycle_statements:
        return False, []

    targets = sorted({t for s in lifecycle_statements for t in _targets_in(s)})
    if not targets:
        return False, []
    return True, targets


def main() -> int:
    cmd = _command_from_stdin()
    blocked, targets = is_blocked(cmd)
    if not blocked:
        return 0
    sys.stderr.write(GUIDANCE.format(targets=", ".join(targets)))
    return 2


if __name__ == "__main__":
    sys.exit(main())
