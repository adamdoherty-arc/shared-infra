"""The host-memory reclaim job never writes to the docker-desktop VM.

2026-10-06 08:41-09:27 UTC a page-cache drop plus memory compaction inside the
docker-desktop VM at low host memory cut every container off
host.docker.internal:5432 for 30 minutes (630 exceptions in 24h). On
2026-10-09 the System Hub item exceptions_24h (121, red at 100) showed the same
"timeout expired" / "No route to host" bursts, and the cause was still live:
scripts/wsl-memory-reclaim.ps1 ran that VM cache drop from the interactive
scheduled task "Shared Infra - WSL Memory Reclaim" on every 20-minute trigger
(14 of 31 runs on 2026-10-09). ADA's restart_gate.py only blocks agent Bash
commands, so it never saw the scheduled task.

This gate would pass trivially if it only grepped the whole file (the header
comment names the incident and would trip it, or be exempted wholesale), or if
it matched only the literal drop_caches word and not a wsl invocation or any
write under /proc/sys: it strips comments first, then rejects any wsl
invocation, any /proc/sys path and both VM knob names in executable code; the
sabotage tests prove each form is rejected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "wsl-memory-reclaim.ps1"

FORBIDDEN = (
    re.compile(r"(?i)(^|[\s;&|(])wsl(\.exe)?(\s|$)"),
    re.compile(r"/proc/sys"),
    re.compile(r"drop_caches"),
    re.compile(r"compact_memory"),
)


def executable_lines(text: str) -> list[str]:
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    lines = []
    for line in text.splitlines():
        code = line.split("#", 1)[0] if not re.search(r"""['"].*#.*['"]""", line) else line
        if code.strip():
            lines.append(code)
    return lines


def violations(text: str) -> list[str]:
    return [line.strip() for line in executable_lines(text) for rx in FORBIDDEN if rx.search(line)]


def test_reclaim_script_never_touches_the_vm():
    text = SCRIPT.read_text(encoding="utf-8")
    assert violations(text) == []


def test_reclaim_script_still_trims_host_working_sets():
    assert "EmptyWorkingSet" in "\n".join(executable_lines(SCRIPT.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "sabotage",
    [
        'wsl -d docker-desktop -- sh -c "sync"',
        "& wsl.exe -d docker-desktop -- sh -c 'x'",
        'sh -c "echo 1 > /proc/sys/vm/compact_memory"',
        "$k = 'drop_caches'",
        "Start-Process wsl -ArgumentList $a",
    ],
)
def test_sabotage_vm_write_forms_are_rejected(sabotage):
    assert violations(SCRIPT.read_text(encoding="utf-8") + "\n" + sabotage + "\n") != []


def test_control_comment_mentions_are_not_violations():
    text = "<#\nwsl -d docker-desktop drop_caches\n#>\n# compact_memory in a comment\nGet-Process\n"
    assert violations(text) == []
