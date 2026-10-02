"""Every hostcron job runs git as a user that trusts our repos.

hostcron runs as NT AUTHORITY\\SYSTEM. SYSTEM's own global git config does not
trust repos owned by the user, so every git call in a job died with "detected
dubious ownership". Measured 2026-10-01: ada-feature-map-nightly read zero
tracked files, rewrote 175 feature maps with every derived section deleted
(6,221 lines), its commit step printed "nothing changed" and the job reported
rc=0, two nights running.

This gate would pass trivially if it only checked that the env key exists:
it also checks the config file exists, includes the owner's config and lists
C:/code/ADA under [safe], and that every job points at that same file.
"""

from __future__ import annotations

import json
from pathlib import Path

HOSTCRON = Path(__file__).resolve().parents[1] / "hostcron"
CONFIG = HOSTCRON / "gitconfig-hostcron"


def _jobs() -> list[dict]:
    return json.loads((HOSTCRON / "schedule.json").read_text(encoding="utf-8"))["jobs"]


EXPECTED_ENV_PATH = r"C:\code\shared-infra\scripts\hostcron\gitconfig-hostcron"


def _norm(path: str) -> str:
    return path.replace("\\", "/").lower()


def test_every_job_sets_git_config_global_to_the_hostcron_config():
    missing = [
        j["name"]
        for j in _jobs()
        if _norm(j.get("env", {}).get("GIT_CONFIG_GLOBAL", "")) != _norm(EXPECTED_ENV_PATH)
    ]
    assert missing == []
    assert CONFIG.is_file()


def test_hostcron_git_config_trusts_ada_and_includes_owner_config():
    text = CONFIG.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]
    assert "[include]" in lines
    assert "path = C:/Users/hadam/.gitconfig" in lines
    assert "[safe]" in lines
    for repo in ("C:/code/ADA", "C:/code/legion", "C:/code/shared-infra"):
        assert f"directory = {repo}" in lines
