#!/usr/bin/env python3
"""PreToolUse gate: bifrost/config.json and bifrost/disabled-providers.json
have ONE writer, infractl (2026-09-25).

Before this, three writers edited those files with no coordination (ADA's
bifrost_model_sync.py, bifrost-autoheal, and humans/agents), each running
its own restart sequence, and git never reflected live routing. Now every
change goes through infractl (`infractl models apply`, `infractl park`,
`infractl unpark`, or POST /api/bifrost/models/apply), which validates
against bifrost/operator-disabled.json, snapshots, runs the binding restart
and rolls back on failure, and scripts/config_autocommit.py commits the
result with the ledger rows as the message.

This hook blocks the ways an agent session would bypass that:
  - Edit / Write / MultiEdit / NotebookEdit / Serena write tools whose target
    is one of the two files;
  - Bash / PowerShell commands that write into them: shell redirects
    (`>`, `>>`), `tee`, `sed -i` / `perl -i`, `cp` / `mv` / `install` onto
    them, `dd of=`, `Set-Content` / `Out-File` / `Add-Content`, and inline
    Python that opens them for writing.
Reading them is never blocked.

Escape hatch (deliberate operator edit, e.g. repairing infractl itself):
`INFRA_CONFIG_WRITE_OK=1` in the command, or in the environment Claude Code
runs the hook with.

Exit 0 = allow. Exit 2 = block, with the reason on stderr.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

PROTECTED = ("config.json", "disabled-providers.json")
_PROTECTED_RE = re.compile(r"(?:^|[\\/])bifrost[\\/](?:config\.json|disabled-providers\.json)$", re.I)
OVERRIDE = "INFRA_CONFIG_WRITE_OK=1"

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path", "relative_path", "path")
SHELL_TOOLS = {"Bash", "PowerShell"}

_SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
_REDIRECT = re.compile(r"(?:^|[^0-9&<>])(?:\d?>>?|>\|)\s*(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")
_PATH_TOKEN = re.compile(r"[^\s\"'`=(),]*bifrost[\\/](?:config\.json|disabled-providers\.json)(?![\w.\-])", re.I)
_PY_PATH_LITERAL = re.compile(
    r"[\"']([^\"'\s]*bifrost[\\/]+(?:config\.json|disabled-providers\.json))[\"']", re.I
)
_PY_WRITE = re.compile(
    r"open\([^)]*['\"][wax]\+?b?['\"]|write_text|write_bytes|json\.dump\(|os\.replace|shutil\.(copy|move)",
    re.I,
)

GUIDANCE = (
    "[config-write-gate] BLOCKED: {what} writes {target}.\n"
    "bifrost/config.json and bifrost/disabled-providers.json have ONE writer: infractl.\n"
    "Use it instead (dry-run first; it validates operator-disabled.json, snapshots,\n"
    "runs the binding restart and rolls back on failure):\n"
    "  docker exec shared-infra-control infractl models apply --changes '{{\"<provider>\": {{\"add\": [...], "
    "\"remove\": [...]}}}}' --reason '...' [--apply]\n"
    "  docker exec shared-infra-control infractl park <provider> --reason '...' --apply\n"
    "  docker exec shared-infra-control infractl unpark <provider> --apply\n"
    "For a deliberate manual edit, opt out explicitly: INFRA_CONFIG_WRITE_OK=1\n"
)


def is_protected_path(path: str, cwd: str = "") -> bool:
    if not path:
        return False
    p = path.strip().strip("\"'")
    if cwd and not os.path.isabs(p) and not re.match(r"^[A-Za-z]:[\\/]", p):
        p = os.path.join(cwd, p)
    return bool(_PROTECTED_RE.search(p.replace("\\", "/")))


def _tokens(stmt: str) -> list[str]:
    try:
        return shlex.split(stmt, posix=True)
    except ValueError:
        return stmt.split()


def shell_write_target(cmd: str, cwd: str = "") -> str | None:
    """Return the protected path a shell command writes, or None."""
    if not cmd or ("config.json" not in cmd and "disabled-providers" not in cmd):
        return None
    # Inline Python spans shell separators (`python -c "a; b"`, heredocs), so
    # it is judged on the whole command: a protected path AND a write call.
    if re.search(r"\b(?:python[\d.]*|py)(?:\.exe)?\b", cmd, re.I):
        # The path must be a string literal of its own (how code names a file).
        # Prose that merely mentions config.json -- a docstring, a job
        # description -- next to an unrelated json.dump is not a write
        # (false positive 2026-09-25: a hostcron schedule.json edit was blocked).
        m = _PY_PATH_LITERAL.search(cmd)
        if m and _PY_WRITE.search(cmd):
            return m.group(1)
    for stmt in (s.strip() for s in _SEPARATORS.split(cmd)):
        if not stmt:
            continue
        for m in _REDIRECT.finditer(stmt):
            if is_protected_path(m.group(1), cwd):
                return m.group(1).strip("\"'")
        toks = _tokens(stmt)
        while toks and "=" in toks[0] and not toks[0].startswith("-"):
            toks = toks[1:]  # VAR=value prefixes
        if not toks:
            continue
        verb = os.path.basename(toks[0]).lower()
        args = [t for t in toks[1:] if not t.startswith("-")]
        if verb == "tee":
            hit = next((a for a in args if is_protected_path(a, cwd)), None)
            if hit:
                return hit
        if verb in ("sed", "perl") and any(t == "-i" or t.startswith("-i") for t in toks[1:]):
            hit = next((a for a in args if is_protected_path(a, cwd)), None)
            if hit:
                return hit
        if verb in ("cp", "mv", "install", "rsync", "copy", "move", "copy-item", "move-item") and args:
            if is_protected_path(args[-1], cwd):
                return args[-1]
        if verb == "dd":
            hit = next((t[3:] for t in toks[1:] if t.startswith("of=") and is_protected_path(t[3:], cwd)), None)
            if hit:
                return hit
        if verb in ("set-content", "out-file", "add-content") or re.search(
                r"\b(set-content|out-file|add-content)\b", stmt, re.I):
            m = _PATH_TOKEN.search(stmt)
            if m:
                return m.group(0)
    return None


def decide(payload: dict, env: dict | None = None) -> tuple[bool, str]:
    """Pure decision: (blocked, message)."""
    env = os.environ if env is None else env
    if env.get("INFRA_CONFIG_WRITE_OK") == "1":
        return False, ""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return False, ""
    cwd = payload.get("cwd") or ""

    is_file_tool = tool in FILE_TOOLS or (tool.startswith("mcp__serena__") and any(
        w in tool for w in ("replace", "create", "insert", "delete", "rename", "write")))
    if is_file_tool:
        for key in PATH_KEYS:
            path = tool_input.get(key)
            if isinstance(path, str) and is_protected_path(path, cwd):
                return True, GUIDANCE.format(what=f"{tool}", target=path)
        return False, ""

    if tool in SHELL_TOOLS:
        cmd = tool_input.get("command") or ""
        if not isinstance(cmd, str) or OVERRIDE in cmd:
            return False, ""
        target = shell_write_target(cmd, cwd)
        if target:
            return True, GUIDANCE.format(what=f"this {tool} command", target=target)
    return False, ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 -- an unreadable payload is not ours to block
        return 0
    if not isinstance(payload, dict):
        return 0
    blocked, message = decide(payload)
    if blocked:
        sys.stderr.write(message)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
