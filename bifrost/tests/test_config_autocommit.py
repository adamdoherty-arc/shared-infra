"""scripts/config_autocommit.py against a throwaway git repo: commits ONLY the
Bifrost config files, with the infractl ledger as the message, and never
when a human has them staged or a write is still settling.

Run: python -m pytest bifrost/tests/test_config_autocommit.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "config_autocommit.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "bifrost").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "tester")
    _git(r, "config", "user.email", "tester@example.invalid")
    _git(r, "config", "core.hooksPath", "no-hooks")
    for name in ("config.json", "disabled-providers.json", "config.snapshot.redacted.json"):
        (r / "bifrost" / name).write_text(json.dumps({"providers": {}}) + "\n", encoding="utf-8")
    (r / "other.txt").write_text("x\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def mod(repo, monkeypatch):
    spec = importlib.util.spec_from_file_location("config_autocommit_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "REPO", repo)
    return m


def _age(path: Path, seconds: int = 600) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


ROW = {"id": "a1b2", "kind": "bifrost_models_apply", "status": "succeeded", "dry_run": 0,
       "requested_by": "ada:bifrost_model_sync", "reason": "daily sync", "created_at": time.time(),
       "verify_json": json.dumps({"summary": "models: groq +['x/new']"}), "payload_json": "{}"}


def test_commits_only_config_paths_with_ledger_message(repo, mod, monkeypatch):
    cfg = repo / "bifrost" / "config.json"
    cfg.write_text(json.dumps({"providers": {"groq": {}}}) + "\n", encoding="utf-8")
    _age(cfg)
    (repo / "other.txt").write_text("staged by someone else\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    monkeypatch.setattr(mod, "ledger_rows", lambda since: ([ROW], None))
    monkeypatch.setattr("sys.argv", ["config_autocommit.py"])

    assert mod.main() == 0
    msg = _git(repo, "log", "-1", "--format=%B")
    assert msg.startswith("config: models: groq +['x/new']")
    assert "bifrost_models_apply by ada:bifrost_model_sync" in msg
    assert "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" in msg
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["bifrost/config.json"]
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["other.txt"]  # untouched


def test_nothing_to_commit(repo, mod, monkeypatch):
    monkeypatch.setattr("sys.argv", ["config_autocommit.py"])
    head = _git(repo, "rev-parse", "HEAD")
    assert mod.main() == 0
    assert _git(repo, "rev-parse", "HEAD") == head


def test_skips_when_a_human_has_the_file_staged(repo, mod, monkeypatch):
    cfg = repo / "bifrost" / "config.json"
    cfg.write_text(json.dumps({"providers": {"human": {}}}) + "\n", encoding="utf-8")
    _age(cfg)
    _git(repo, "add", "bifrost/config.json")
    monkeypatch.setattr(mod, "ledger_rows", lambda since: ([ROW], None))
    monkeypatch.setattr("sys.argv", ["config_autocommit.py"])
    head = _git(repo, "rev-parse", "HEAD")
    assert mod.main() == 0
    assert _git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("content, age", [('{"providers": {"g": {}}}\n', 5), ('{"providers": {', 600)])
def test_skips_unsettled_or_invalid_json(repo, mod, monkeypatch, content, age):
    cfg = repo / "bifrost" / "config.json"
    cfg.write_text(content, encoding="utf-8")
    _age(cfg, age)
    monkeypatch.setattr(mod, "ledger_rows", lambda since: ([ROW], None))
    monkeypatch.setattr("sys.argv", ["config_autocommit.py"])
    head = _git(repo, "rev-parse", "HEAD")
    assert mod.main() == 0
    assert _git(repo, "rev-parse", "HEAD") == head


def test_change_without_ledger_row_says_so(mod):
    msg = mod.build_message([], None, time.time() - 3600)
    assert msg.startswith("config: live routing change written outside infractl")
    assert "no infractl ledger row since" in msg


def test_rolled_back_rows_are_labelled(mod):
    rolled = dict(ROW, status="rolled_back", id="c3")
    msg = mod.build_message([rolled], None, 0)
    assert "[rolled_back]" in msg.splitlines()[0]
