"""Unit tests for `.claude/hooks/bifrost_restart_gate.py`.

Run: python -m pytest .claude/hooks/tests/test_bifrost_restart_gate.py -q

Exercises both the pure `is_blocked()` decision function and the stdin ->
exit-code contract the PreToolUse hook actually runs under, since a gate
that decides correctly but never reads its payload right is as useless as
one with an inverted regex.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bifrost_restart_gate as gate  # noqa: E402

HOOK_PATH = Path(__file__).resolve().parent.parent / "bifrost_restart_gate.py"


def _run(cmd: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )


# ---- pure decision function ------------------------------------------------

def test_bare_docker_restart_bifrost_blocked():
    blocked, targets = gate.is_blocked("docker restart shared-bifrost")
    assert blocked is True
    assert targets == ["shared-bifrost"]


def test_bare_docker_restart_qwen_blocked():
    blocked, targets = gate.is_blocked("docker restart qwen38-chat")
    assert blocked is True
    assert "qwen38-chat" in targets


def test_bare_docker_stop_vllm_embed_blocked():
    blocked, targets = gate.is_blocked("docker stop vllm-embed")
    assert blocked is True
    assert "vllm-embed" in targets


def test_compose_up_recreate_blocked():
    blocked, targets = gate.is_blocked(
        "docker compose -f docker-compose.bifrost.yml up -d shared-bifrost"
    )
    assert blocked is True
    assert "shared-bifrost" in targets


def test_sanctioned_script_allowed():
    blocked, _ = gate.is_blocked("bash scripts/bifrost_restart.sh")
    assert blocked is False


def test_operator_env_override_allowed():
    blocked, _ = gate.is_blocked("INFRA_RESTART_OK=1 docker restart shared-bifrost")
    assert blocked is False


def test_unrelated_container_not_blocked():
    blocked, _ = gate.is_blocked("docker restart legion-backend")
    assert blocked is False


def test_unrelated_command_not_blocked():
    blocked, _ = gate.is_blocked("docker ps --format '{{.Names}}'")
    assert blocked is False


def test_empty_command_not_blocked():
    blocked, targets = gate.is_blocked("")
    assert blocked is False
    assert targets == []


def test_grep_mentioning_docker_restart_not_blocked():
    """Reading ABOUT the restart command must not trip the gate (same lesson
    ADA's restart_gate.py learned the hard way on its first live run)."""
    blocked, _ = gate.is_blocked('grep -n "docker restart shared-bifrost" CLAUDE.md')
    assert blocked is False


# ---- process-level contract (stdin JSON -> exit code) ----------------------

def test_process_exit_2_on_blocked_command():
    result = _run("docker restart shared-bifrost")
    assert result.returncode == 2
    assert "shared-bifrost" in result.stderr
    assert "scripts/bifrost_restart.sh" in result.stderr


def test_process_exit_0_on_sanctioned_script():
    result = _run("bash scripts/bifrost_restart.sh")
    assert result.returncode == 0
    assert result.stderr == ""


def test_process_exit_0_on_env_override():
    result = _run("INFRA_RESTART_OK=1 docker stop qwen38-chat")
    assert result.returncode == 0


def test_process_exit_0_on_unrelated_command():
    result = _run("docker ps")
    assert result.returncode == 0
    assert result.stderr == ""


def test_process_malformed_stdin_fails_open():
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0


def test_process_non_bash_tool_ignored():
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"command": "docker restart shared-bifrost"}})
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
