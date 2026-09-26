"""Unit tests for `.claude/hooks/config_write_gate.py` (infractl is the single
writer of bifrost/config.json + bifrost/disabled-providers.json).

Run: python -m pytest .claude/hooks/tests/test_config_write_gate.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_write_gate as gate  # noqa: E402

HOOK_PATH = Path(__file__).resolve().parent.parent / "config_write_gate.py"
REPO = r"C:\code\shared-infra"


def _bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": REPO}


def _file(tool: str, path: str) -> dict:
    return {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": REPO}


NO_ENV: dict = {}

# ---- blocked ---------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    _file("Edit", r"c:\code\shared-infra\bifrost\config.json"),
    _file("Write", "C:/code/shared-infra/bifrost/disabled-providers.json"),
    _file("MultiEdit", "bifrost/config.json"),
    {"tool_name": "mcp__serena__replace_content", "tool_input": {"relative_path": "bifrost/config.json"},
     "cwd": REPO},
])
def test_file_tools_on_protected_files_are_blocked(payload):
    blocked, msg = gate.decide(payload, NO_ENV)
    assert blocked is True
    assert "infractl" in msg


@pytest.mark.parametrize("cmd", [
    "echo '{}' > bifrost/config.json",
    "jq . x.json >> bifrost/disabled-providers.json",
    "cat new.json | tee bifrost/config.json",
    "sed -i 's/a/b/' bifrost/config.json",
    "cp /tmp/config.json bifrost/config.json",
    "mv bifrost/config.json.bak.1 C:/code/shared-infra/bifrost/config.json",
    "dd if=x of=bifrost/config.json",
    "python -c \"import json; c=json.load(open('bifrost/config.json')); "
    "json.dump(c, open('bifrost/config.json','w'))\"",
    "python - <<'EOF'\nfrom pathlib import Path\nPath('bifrost/config.json').write_text('{}')\nEOF",
])
def test_shell_writes_are_blocked(cmd):
    blocked, _ = gate.decide(_bash(cmd), NO_ENV)
    assert blocked is True, cmd


def test_powershell_set_content_is_blocked():
    payload = {"tool_name": "PowerShell",
               "tool_input": {"command": "Get-Content x | Set-Content bifrost\\config.json"}, "cwd": REPO}
    assert gate.decide(payload, NO_ENV)[0] is True


# ---- allowed ---------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    _file("Edit", r"c:\code\shared-infra\bifrost\operator-disabled.json"),
    _file("Write", r"c:\code\shared-infra\bifrost\config.json.bak.20260925"),
    _file("Edit", r"c:\code\shared-infra\infractl\core\actions.py"),
    {"tool_name": "Read", "tool_input": {"file_path": r"c:\code\shared-infra\bifrost\config.json"}, "cwd": REPO},
])
def test_other_files_and_reads_are_allowed(payload):
    assert gate.decide(payload, NO_ENV) == (False, "")


@pytest.mark.parametrize("cmd", [
    "cat bifrost/config.json",
    "python -m json.tool bifrost/config.json > /tmp/pretty.json",
    "cp bifrost/config.json /tmp/config.json.review",
    "git diff bifrost/config.json",
    "grep -n nvidia bifrost/config.json 2>&1",
    "python -c \"import json; print(json.dumps(json.load(open('bifrost/config.json'))))\"",
    "docker exec shared-infra-control infractl models apply --changes '{}' --reason x",
])
def test_reads_and_infractl_calls_are_allowed(cmd):
    assert gate.decide(_bash(cmd), NO_ENV) == (False, ""), cmd


def test_override_in_command_allows():
    assert gate.decide(_bash("INFRA_CONFIG_WRITE_OK=1 sed -i 's/a/b/' bifrost/config.json"), NO_ENV)[0] is False


def test_override_in_environment_allows_file_tools():
    env = {"INFRA_CONFIG_WRITE_OK": "1"}
    assert gate.decide(_file("Edit", r"c:\code\shared-infra\bifrost\config.json"), env)[0] is False


# ---- stdin -> exit code contract -------------------------------------------

def _run(payload: dict, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "INFRA_CONFIG_WRITE_OK"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(HOOK_PATH)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=10, env=env)


def test_hook_exit_2_with_guidance_on_block():
    proc = _run(_file("Write", r"c:\code\shared-infra\bifrost\config.json"))
    assert proc.returncode == 2
    assert "config-write-gate" in proc.stderr and "infractl models apply" in proc.stderr


def test_hook_exit_0_on_allow_and_on_env_override():
    assert _run(_bash("cat bifrost/config.json")).returncode == 0
    assert _run(_file("Write", r"c:\code\shared-infra\bifrost\config.json"),
                {"INFRA_CONFIG_WRITE_OK": "1"}).returncode == 0


def test_hook_exit_0_on_garbage_stdin():
    proc = subprocess.run([sys.executable, str(HOOK_PATH)], input="not json",
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0


@pytest.mark.parametrize("cmd", [
    # Prose mentioning the config next to an unrelated write (2026-09-25 false positive).
    "python - <<'EOF'\nimport json\njob = {'description': 'commits bifrost/config.json drift'}\n"
    "json.dump(job, open('scripts/hostcron/schedule.json', 'w'))\nEOF",
    "python x.py; docker compose -f docker-compose.bifrost.yml up -d bifrost-autoheal",
])
def test_prose_mentions_are_not_writes(cmd):
    blocked, _ = gate.decide(_bash(cmd), NO_ENV)
    assert blocked is False, cmd


def test_windows_path_literal_write_is_blocked():
    cmd = r"""python -c "open(r'C:\code\shared-infra\bifrost\config.json', 'w').write('{}')" """
    blocked, _ = gate.decide(_bash(cmd), NO_ENV)
    assert blocked is True
