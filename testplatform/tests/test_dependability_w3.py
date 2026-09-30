from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tplib import runner, server  # noqa: E402
from tplib.legion import LegionClient  # noqa: E402


def test_expand_env_reads_dotenv_and_process_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DB_PASSWORD=s3cret\n# X=1\nQUOTED=\"q v\"\n", encoding="utf-8")
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setenv("FROM_PROC", "proc")
    out = runner.expand_env({"A": "pg://u:${DB_PASSWORD}@h/${FROM_PROC}", "B": "plain", "C": "${QUOTED}"}, tmp_path)
    assert out == {"A": "pg://u:s3cret@h/proc", "B": "plain", "C": "q v"}


def test_expand_env_missing_reference_is_a_run_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    with pytest.raises(runner.RunError):
        runner.expand_env({"A": "${NOPE_NOT_SET}"}, tmp_path)


class _Project:
    name = "demo"
    default_target = "fast"


def test_queued_request_is_persisted_and_restored_with_its_schedule_id(tmp_path, monkeypatch):
    svc = server.RunnerService(legion=object(), lock_dir=tmp_path / "locks", queue_dir=tmp_path / "queue")
    body = {"project": "demo", "target": "full", "trigger": "schedule", "schedule_id": 7}
    svc._persist_queued("demo:k", body, "schedule")
    files = list((tmp_path / "queue").glob("*.json"))
    assert len(files) == 1

    started = []
    monkeypatch.setattr(server, "load_project", lambda name: _Project())
    monkeypatch.setattr(svc, "_run_queued", lambda project, b, trigger, key, qid: started.append((b, trigger, qid)))
    assert svc.restore_queue() == 1
    for _ in range(50):
        if started:
            break
        import time
        time.sleep(0.02)
    assert started and started[0][0]["schedule_id"] == 7 and started[0][1] == "schedule"


def test_restore_queue_drops_entries_for_unknown_projects(tmp_path, monkeypatch):
    svc = server.RunnerService(legion=object(), lock_dir=tmp_path / "locks", queue_dir=tmp_path / "queue")
    svc._persist_queued("gone:k", {"project": "gone", "target": "x"}, "manual")

    def boom(name):
        raise server.ProfileError("unknown project")

    monkeypatch.setattr(server, "load_project", boom)
    assert svc.restore_queue() == 0
    assert not list((tmp_path / "queue").glob("*.json"))


def test_submit_rejects_a_non_integer_schedule_id(tmp_path):
    svc = server.RunnerService(legion=object(), lock_dir=tmp_path / "locks", queue_dir=tmp_path / "queue")
    code, payload = svc.submit({"project": "demo", "trigger": "schedule", "schedule_id": "seven"})
    assert code == 400 and "schedule_id" in payload["error"]


class _Resp:
    def __init__(self, body: bytes, etag: str):
        self._body, self.headers = body, {"ETag": etag}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_hashes_uses_etag_and_serves_cache_on_304(monkeypatch):
    client = LegionClient("http://x")
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.headers.get("If-none-match"))
        if len(seen) == 1:
            return _Resp(json.dumps({"a": "h1"}).encode(), '"e1"')
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)

    monkeypatch.setattr("tplib.legion.urllib.request.urlopen", fake_urlopen)
    assert client.hashes(1) == {"a": "h1"}
    assert client.hashes(1) == {"a": "h1"}
    assert seen == [None, '"e1"']


def test_git_sha_distinguishes_dirty_trees_and_is_stable_for_identical_ones(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "init")
    clean = runner.git_sha(tmp_path)
    assert len(clean) == 40 and "+" not in clean
    (tmp_path / "a.txt").write_text("two\n")
    dirty_two = runner.git_sha(tmp_path)
    assert len(dirty_two) == 40 and dirty_two[32] == "+" and dirty_two[:32] == clean[:32]
    assert runner.git_sha(tmp_path) == dirty_two
    (tmp_path / "a.txt").write_text("three\n")
    assert runner.git_sha(tmp_path) != dirty_two
