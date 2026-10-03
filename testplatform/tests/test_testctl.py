from __future__ import annotations

import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tplib import artifacts, parsers, printer, profile, runner  # noqa: E402
from tplib.bodyhash import body_hash  # noqa: E402
from tplib.legion import LegionClient  # noqa: E402
from tplib.lock import ERROR_ACCESS_DENIED, ProjectLock, _win_pid_alive, pid_alive  # noqa: E402
from tplib.signature import normalize_message, signature  # noqa: E402


def test_signature_strips_volatile_tokens():
    a = signature("AssertionError", "expected 3 got 2 at 0xdeadbeef1234 in /app/backend/x.py on 2026-09-29T10:11:12Z")
    b = signature("AssertionError", "expected 41 got 7 at 0xabcdef123456 in /srv/other/y.py on 2027-01-01T00:00:00Z")
    assert a == b
    assert "0x" not in a and "2026" not in a


def test_signature_uuid_and_windows_path():
    s = normalize_message(r"order 6f1c2a3b-1111-2222-3333-444455556666 saved to C:\tmp\a\b.json")
    assert "<uuid>" in s and "<path>" in s


def test_signature_distinguishes_different_errors():
    assert signature("KeyError", "'alpha'") != signature("ValueError", "'alpha'")


def test_signature_is_bounded():
    assert len(signature("E", "x" * 5000)) <= 200


def test_printer_caps_default_and_detail():
    buf = io.StringIO()
    n = printer.emit([f"line {i}" for i in range(100)], detail=False, stream=buf)
    assert n == 15 and len(buf.getvalue().splitlines()) == 15
    assert "more lines" in buf.getvalue().splitlines()[-1]
    buf = io.StringIO()
    printer.emit([f"line {i}" for i in range(100)], detail=True, stream=buf)
    assert len(buf.getvalue().splitlines()) == 60


def test_printer_control_short_output_untouched():
    buf = io.StringIO()
    printer.emit(["a", "b"], stream=buf)
    assert buf.getvalue() == "a\nb\n"


def test_printer_counts_embedded_newlines_and_truncates_wide_lines():
    lines = printer.fit(["x\n" * 40, "y" * 1000])
    assert len(lines) == 15
    assert all(len(ln) <= printer.MAX_LINE_CHARS for ln in printer.fit(["y" * 1000]))


def test_no_stray_stdout_writes_outside_printer():
    root = Path(__file__).resolve().parent.parent / "tplib"
    offenders = []
    for path in root.glob("*.py"):
        if path.name == "printer.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("print(") or "sys.stdout" in stripped:
                offenders.append(f"{path.name}:{i}")
    assert offenders == []


SAMPLE_TEST = '''
import pytest

def helper():
    return 1

class TestA:
    def test_b(self):
        assert helper() == 1

    @pytest.mark.parametrize("x", [1, 2])
    def test_p(self, x):
        assert x

def test_top():
    assert True
'''


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(SAMPLE_TEST, encoding="utf-8")
    return tmp_path


def test_body_hash_targets_function_source(tmp_path):
    repo = _repo(tmp_path)
    h1 = body_hash(repo, "tests/test_x.py::TestA::test_b")
    h2 = body_hash(repo, "tests/test_x.py::test_top")
    assert h1 != h2
    assert body_hash(repo, "tests/test_x.py::TestA::test_p[1]") == body_hash(repo, "tests/test_x.py::TestA::test_p[2]")
    edited = SAMPLE_TEST.replace("helper() == 1", "helper() == 2")
    (repo / "tests" / "test_x.py").write_text(edited, encoding="utf-8")
    assert body_hash(repo, "tests/test_x.py::TestA::test_b") != h1
    assert body_hash(repo, "tests/test_x.py::test_top") == h2


def test_body_hash_missing_file_falls_back(tmp_path):
    assert len(body_hash(tmp_path, "tests/nope.py::test_a")) == 40


def _pytest_report():
    return {
        "root": "/app",
        "summary": {"total": 6},
        "collectors": [{"nodeid": "tests/test_broken.py", "outcome": "failed",
                        "longrepr": "ImportError while importing\nE   ModuleNotFoundError: No module named 'zzz'"}],
        "tests": [
            {"nodeid": "tests/test_x.py::test_top", "outcome": "passed",
             "setup": {"duration": 0.001, "outcome": "passed"}, "call": {"duration": 0.04, "outcome": "passed"},
             "teardown": {"duration": 0.001, "outcome": "passed"}},
            {"nodeid": "tests/test_x.py::TestA::test_b", "outcome": "failed",
             "setup": {"duration": 0, "outcome": "passed"},
             "call": {"duration": 0.01, "outcome": "failed",
                      "crash": {"path": "/app/tests/test_x.py", "lineno": 9,
                                "message": "AssertionError: expected 3 got 2\nassert 3 == 2"},
                      "longrepr": "def t():\n>   assert 3 == 2\nE   AssertionError: expected 3 got 2\n\ntests/test_x.py:9: AssertionError"},
             "teardown": {"duration": 0, "outcome": "passed"}},
            {"nodeid": "tests/test_x.py::TestA::test_p[1]", "outcome": "rerun",
             "call": {"duration": 0.02, "outcome": "passed"}},
            {"nodeid": "tests/test_x.py::TestA::test_p[2]", "outcome": "skipped", "call": {"duration": 0}},
            {"nodeid": "tests/test_x.py::test_setup_boom", "outcome": "error",
             "setup": {"duration": 0, "outcome": "failed", "crash": {"message": "RuntimeError: db down"},
                       "longrepr": "E   RuntimeError: db down"}},
        ],
    }


def test_parse_pytest_json_statuses_and_failures(tmp_path):
    repo = _repo(tmp_path)
    cases = {c["node_id"]: c for c in parsers.parse_pytest_json(_pytest_report(), repo, reruns=2)}
    assert cases["tests/test_x.py::test_top"]["status"] == "passed"
    assert cases["tests/test_x.py::test_top"]["duration_ms"] == 42
    failed = cases["tests/test_x.py::TestA::test_b"]
    assert failed["status"] == "failed" and failed["attempts"] == 3
    assert failed["failure"]["type"] == "AssertionError"
    assert failed["failure"]["signature"] == "AssertionError: expected <n> got <n>"
    assert failed["file"] == "tests/test_x.py" and len(failed["body_hash"]) == 40
    assert cases["tests/test_x.py::TestA::test_p[1]"]["status"] == "rerun_passed"
    assert cases["tests/test_x.py::TestA::test_p[2]"]["status"] == "skipped"
    assert cases["tests/test_x.py::test_setup_boom"]["status"] == "error"
    assert cases["tests/test_x.py::test_setup_boom"]["failure"]["type"] == "RuntimeError"
    collector = cases["tests/test_broken.py"]
    assert collector["status"] == "error" and collector["failure"]["type"] == "ModuleNotFoundError"
    totals = parsers.totals_of(list(cases.values()))
    assert totals == {"total": 6, "passed": 1, "failed": 1, "errors": 2, "skipped": 1, "rerun_passed": 1}


def test_trace_tail_capped_at_40_lines(tmp_path):
    report = {"tests": [{"nodeid": "tests/test_x.py::test_top", "outcome": "failed",
                         "call": {"outcome": "failed", "crash": {"message": "ValueError: x"},
                                  "longrepr": "\n".join(f"L{i}" for i in range(500))}}]}
    case = parsers.parse_pytest_json(report, tmp_path)[0]
    assert len(case["failure"]["trace_tail"].splitlines()) == 40
    assert case["failure"]["trace_tail"].splitlines()[-1] == "L499"


def test_path_prefix_applied(tmp_path):
    report = {"tests": [{"nodeid": "tests/test_x.py::test_top", "outcome": "passed", "call": {"duration": 0}}]}
    case = parsers.parse_pytest_json(report, tmp_path, path_prefix="backend/")[0]
    assert case["node_id"] == "backend/tests/test_x.py::test_top"


def test_parse_vitest_json(tmp_path):
    report = {"testResults": [{"name": "/app/src/a.test.ts", "status": "failed", "assertionResults": [
        {"fullName": "A works", "title": "works", "status": "passed", "duration": 3},
        {"fullName": "A breaks", "title": "breaks", "status": "failed", "duration": 5,
         "failureMessages": ["AssertionError: expected 1 to be 2\n at x.ts:3"]},
        {"fullName": "A later", "title": "later", "status": "pending"}]}]}
    cases = parsers.parse_vitest_json(report, tmp_path)
    assert [c["status"] for c in cases] == ["passed", "failed", "skipped"]
    assert cases[1]["failure"]["type"] == "AssertionError"


def test_parse_playwright_smoke():
    cases = parsers.parse_playwright_smoke([
        {"url": "http://x/alerts", "returncode": 0, "output": {"status": "success"}, "duration_ms": 5},
        {"url": "http://x/labs", "returncode": 1, "output": {"status": "failure", "reason": "timeout waiting"},
         "duration_ms": 9}])
    assert [c["status"] for c in cases] == ["passed", "failed"]
    assert cases[1]["node_id"] == "e2e::/labs"


def test_select_rows_sparse_keeps_failures_and_changed_hashes():
    cases = [
        {"node_id": "a", "status": "passed", "body_hash": "h1"},
        {"node_id": "b", "status": "passed", "body_hash": "NEW"},
        {"node_id": "c", "status": "failed", "body_hash": "h3"},
        {"node_id": "d", "status": "rerun_passed", "body_hash": "h4"},
        {"node_id": "e", "status": "passed", "body_hash": "h5"},
    ]
    known = {"a": "h1", "b": "old", "c": "h3", "d": "h4"}
    kept = {c["node_id"] for c in parsers.select_rows(cases, known, sparse=True)}
    assert kept == {"b", "c", "d", "e"}
    assert len(parsers.select_rows(cases, known, sparse=False)) == 5


PROFILE = """
project: demo
framework: pytest
default_target: fast
path_tier: path
runtime: {kind: exec, container: c}
tiers:
  fast: {paths: [tests], timeout_s: 60}
  path: {timeout_s: 30}
"""


def _registry(tmp_path: Path, profile_text: str = PROFILE) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".testplatform.yml").write_text(profile_text, encoding="utf-8")
    reg = tmp_path / "projects.yml"
    reg.write_text(f"projects:\n  demo:\n    root: {repo.as_posix()}\n    legion_project_id: 9\n", encoding="utf-8")
    return reg


def test_profile_loader_and_target_resolution(tmp_path):
    project = profile.load_project("demo", _registry(tmp_path))
    assert project.legion_project_id == 9 and project.tier_names == ["fast", "path"]
    assert runner.resolve_target(project, None)[0] == "fast"
    tier, cfg, paths = runner.resolve_target(project, "tests/test_a.py::test_b")
    assert tier == "path" and paths == ["tests/test_a.py::test_b"]
    assert profile.parse_target_spec("ada:backend/tests/a.py::T::t") == ("ada", "backend/tests/a.py::T::t")
    assert profile.parse_target_spec("ada") == ("ada", None)


VITEST_PROFILE = PROFILE.replace("path_tier: path", "path_tier: path\nvitest_path_tier: vitest") + "  vitest: {framework: vitest, timeout_s: 60}\n"  # noqa: E501


def test_ts_path_targets_route_to_the_vitest_tier(tmp_path):
    """A .tsx path handed to the pytest path tier collects 0 items and errors; it must reach vitest."""
    project = profile.load_project("demo", _registry(tmp_path, VITEST_PROFILE))
    tier, _cfg, paths = runner.resolve_target(project, "frontend/src/a/__tests__/X.test.tsx")
    assert tier == "vitest" and paths == ["frontend/src/a/__tests__/X.test.tsx"]
    assert runner.resolve_target(project, "tests/test_a.py")[0] == "path"


def test_ts_path_without_vitest_path_tier_keeps_the_old_routing(tmp_path):
    project = profile.load_project("demo", _registry(tmp_path))
    assert runner.resolve_target(project, "frontend/src/X.test.tsx")[0] == "path"


def test_vitest_path_tier_must_name_a_vitest_tier(tmp_path):
    with pytest.raises(profile.ProfileError):
        profile.load_project("demo", _registry(tmp_path, PROFILE.replace("path_tier: path", "path_tier: path\nvitest_path_tier: nope")))
    with pytest.raises(profile.ProfileError):
        profile.load_project("demo", _registry(tmp_path, PROFILE.replace("path_tier: path", "path_tier: path\nvitest_path_tier: fast")))


def test_profile_loader_rejects_bad_profiles(tmp_path):
    with pytest.raises(profile.ProfileError):
        profile.load_project("nope", _registry(tmp_path))
    with pytest.raises(profile.ProfileError):
        profile.load_project("demo", _registry(tmp_path, "framework: pytest\ntiers: {}\n"))
    with pytest.raises(profile.ProfileError):
        profile.load_project("demo", _registry(tmp_path, PROFILE.replace("timeout_s: 60", "timeout_s: 0")))
    with pytest.raises(profile.ProfileError):
        profile.load_project("demo", _registry(tmp_path, PROFILE.replace("framework: pytest", "framework: ruby")))


def test_shipped_profiles_validate():
    reg = profile.load_registry()
    for name in reg["projects"]:
        root = Path(str(reg["projects"][name]["root"]))
        if (root / profile.PROFILE_NAME).exists():
            profile.load_project(name)


def test_lock_acquire_attach_release(tmp_path):
    a = ProjectLock("demo", tmp_path)
    ok, held = a.acquire()
    assert ok and held is None
    a.set_run_id(77)
    b = ProjectLock("demo", tmp_path)
    ok2, held2 = b.acquire()
    assert not ok2 and held2.run_id == 77 and held2.pid == os.getpid()
    assert b.wait_for_run_id(1.0).run_id == 77
    a.release()
    ok3, _ = b.acquire()
    assert ok3
    b.release()
    assert not (tmp_path / "demo.json").exists()


def test_lock_stale_owner_is_taken_over(tmp_path):
    (tmp_path / "demo.json").write_text(json.dumps({"pid": 999999, "run_id": 5}), encoding="utf-8")
    assert not pid_alive(999999)
    lock = ProjectLock("demo", tmp_path)
    ok, _ = lock.acquire()
    assert ok and lock.stale_taken.run_id == 5
    lock.release()


def test_lock_is_per_project(tmp_path):
    a, b = ProjectLock("ada", tmp_path), ProjectLock("legion", tmp_path)
    assert a.acquire()[0] and b.acquire()[0]
    a.release()
    b.release()


def test_pid_alive_self():
    assert pid_alive(os.getpid())


class _FakeKernel32:
    def GetExitCodeProcess(self, handle, ref):
        ref._obj.value = 259
        return True

    def CloseHandle(self, handle):
        return True


class _FakeOpenProcess:
    restype = None

    def __init__(self, handle):
        self.handle = handle

    def __call__(self, *args):
        return self.handle


def _fake_kernel32(handle):
    fake = _FakeKernel32()
    fake.OpenProcess = _FakeOpenProcess(handle)
    return fake


def test_win_pid_alive_access_denied_means_alive():
    assert _win_pid_alive(_fake_kernel32(0), 4242, lambda: ERROR_ACCESS_DENIED)


def test_win_pid_alive_invalid_parameter_means_dead():
    assert not _win_pid_alive(_fake_kernel32(0), 4242, lambda: 87)


def test_win_pid_alive_open_handle_reads_exit_code():
    assert _win_pid_alive(_fake_kernel32(1234), 4242, lambda: 0)


def test_lock_held_by_unopenable_process_is_not_taken_over(tmp_path, monkeypatch):
    import tplib.lock as lock_mod
    (tmp_path / "demo.json").write_text(json.dumps({"pid": 4242, "run_id": 9}), encoding="utf-8")
    monkeypatch.setattr(lock_mod, "pid_alive", lambda pid: _win_pid_alive(_fake_kernel32(0), pid, lambda: ERROR_ACCESS_DENIED))
    lock = ProjectLock("demo", tmp_path)
    ok, held = lock.acquire()
    assert not ok and held.run_id == 9 and not held.stale and lock.stale_taken is None


def test_artifact_prune(tmp_path):
    import datetime as dt
    old = tmp_path / "ada" / (dt.date.today() - dt.timedelta(days=20)).isoformat() / "u1"
    new = tmp_path / "ada" / dt.date.today().isoformat() / "u2"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    assert artifacts.prune(tmp_path) == 1
    assert not old.exists() and new.exists()


def test_map_changed_to_tests(tmp_path):
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_widget_service.py").write_text("", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "test_other.py").write_text("", encoding="utf-8")
    got = runner.map_changed_to_tests(
        tmp_path, ["backend/services/widget_service.py", "backend/tests/test_other.py", "README.md"],
        ["backend/tests/**/test_*.py"])
    assert got == ["backend/tests/test_other.py", "backend/tests/test_widget_service.py"]


class _FakeLegion(BaseHTTPRequestHandler):
    log: list = []
    runs: dict = {}

    def _json(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeLegion.log.append((self.path, body))
        if self.path.endswith("/runs"):
            self._json(201, {"run_id": 123})
        elif self.path.endswith("/results"):
            self._json(200, {"run_id": 123, "verdict": {"status": body["status"]}, "text": "FAILED 1 new failure\nline2"})

    def do_GET(self):
        _FakeLegion.log.append((self.path, None))
        if "/quarantine" in self.path:
            self._json(200, ["tests/test_x.py::test_q"])
        elif "/cases/hashes" in self.path:
            self._json(200, {})
        elif self.path.startswith("/api/test-platform/runs/123"):
            self._json(200, {"status": "failed", "text": "FAILED attached text"})
        else:
            self._json(404, {})

    def log_message(self, *a):
        pass


@pytest.fixture()
def fake_legion():
    _FakeLegion.log = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeLegion)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield LegionClient(f"http://127.0.0.1:{srv.server_address[1]}")
    srv.shutdown()


def test_start_and_run_posts_contract_and_prints_verbatim_text(tmp_path, monkeypatch, fake_legion):
    project = profile.load_project("demo", _registry(tmp_path))
    exe = runner.Execution("failed", [
        {"node_id": "tests/t.py::a", "file": "tests/t.py", "status": "passed", "duration_ms": 1, "attempts": 1,
         "body_hash": "h", "feature_slug": None, "requirement_ids": []},
        {"node_id": "tests/t.py::b", "file": "tests/t.py", "status": "failed", "duration_ms": 1, "attempts": 1,
         "body_hash": "h2", "feature_slug": None, "requirement_ids": [],
         "failure": {"type": "AssertionError", "message": "m", "signature": "s", "trace_tail": "t"}}],
        None, 2, 1)
    monkeypatch.setattr(runner, "execute_framework", lambda *a, **k: exe)
    lock = ProjectLock("demo", tmp_path / "locks")
    assert lock.acquire()[0]
    outcome = runner.start_and_run(project, "changed", "claude", fake_legion, lock, artifacts_root=tmp_path / "art")
    lock.release()
    assert outcome.run_id == 123 and outcome.status == "failed" and outcome.exit_code == 1
    assert outcome.text == "FAILED 1 new failure\nline2"
    posts = {p: b for p, b in _FakeLegion.log if b is not None}
    create = posts["/api/test-platform/runs"]
    assert create["project_id"] == 9 and create["target"] == "changed" and create["trigger"] == "claude"
    assert create["framework"] == "pytest" and create["artifact_path"].endswith(outcome.artifact_dir.name)
    results = posts["/api/test-platform/runs/123/results"]
    assert results["totals"]["failed"] == 1 and results["totals"]["total"] == 2
    assert results["quarantined_deselected"] == 2
    assert {c["node_id"] for c in results["cases"]} == {"tests/t.py::a", "tests/t.py::b"}
    assert (outcome.artifact_dir / "verdict.txt").exists()


_ONE_PASS = {"node_id": "tests/t.py::a", "file": "tests/t.py", "status": "passed", "duration_ms": 1, "attempts": 1,
             "body_hash": "h", "feature_slug": None, "requirement_ids": []}


def test_legion_down_saves_pending_and_renders_local_verdict(tmp_path, monkeypatch):
    project = profile.load_project("demo", _registry(tmp_path))
    exe = runner.Execution("error", [], "container c is not running", 0, None)
    monkeypatch.setattr(runner, "execute_framework", lambda *a, **k: exe)
    lock = ProjectLock("demo", tmp_path / "locks")
    lock.acquire()
    dead = LegionClient("http://127.0.0.1:9", timeout=1)
    outcome = runner.start_and_run(project, None, "manual", dead, lock, artifacts_root=tmp_path / "art")
    lock.release()
    assert outcome.run_id is None and outcome.exit_code == 2
    assert "ERROR" in outcome.text and "container c is not running" in outcome.text
    assert (outcome.artifact_dir / "pending_ingest.json").exists()


def test_attach_and_wait_returns_verdict_of_inflight_run(tmp_path, fake_legion):
    owner = ProjectLock("demo", tmp_path)
    owner.acquire()
    owner.set_run_id(123)
    waiter = ProjectLock("demo", tmp_path)
    assert not waiter.acquire()[0]
    outcome = runner.attach_and_wait(fake_legion, waiter, timeout_s=10)
    owner.release()
    assert outcome.attached and outcome.run_id == 123 and outcome.text == "FAILED attached text"
    assert outcome.exit_code == 1


def test_interpret_pytest_timeout_oom_and_min_executed(tmp_path):
    project = profile.load_project("demo", _registry(tmp_path))
    log = tmp_path / "out.log"
    log.write_text("collecting ...\nKilled\n", encoding="utf-8")
    tier = {"timeout_s": 60, "min_executed": 1}
    ex = runner.interpret_pytest(project, tier, None, "claude", 137, None, log, elapsed=5, timeout_s=60,
                                 quarantined_count=0)
    assert ex.status == "error" and "OOM" in ex.error_summary
    ex = runner.interpret_pytest(project, tier, None, "claude", 124, None, log, elapsed=61, timeout_s=60,
                                 quarantined_count=0)
    assert ex.status == "timeout"
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"tests": [{"nodeid": "a.py::t", "outcome": "skipped", "call": {}}]}), encoding="utf-8")
    ex = runner.interpret_pytest(project, tier, None, "claude", 0, report, log, 1, 60, 0)
    assert ex.status == "error" and "at least 1" in ex.error_summary
    report.write_text(json.dumps({"tests": [{"nodeid": "a.py::t", "outcome": "passed", "call": {"duration": 0}}]}),
                      encoding="utf-8")
    assert runner.interpret_pytest(project, tier, None, "claude", 0, report, log, 1, 60, 0).status == "passed"


def test_pytest_args_by_trigger(tmp_path):
    project = profile.load_project("demo", _registry(tmp_path))
    tier = project.tier("fast")
    sched = runner._pytest_args(project, tier, None, "schedule", "/r.json", [], [])
    assert "--reruns" in sched and "no:randomly" not in sched
    claude = runner._pytest_args(project, tier, None, "claude", "/r.json", ["tests/a.py::q"], [])
    assert "--reruns" not in claude and "no:randomly" in claude
    assert claude[claude.index("--deselect") + 1] == "tests/a.py::q"


def test_paths_map_via_import_graph_and_direct_tests(tmp_path):
    from tplib import selection
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "services").mkdir()
    (tmp_path / "backend" / "services" / "widget_core.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "test_uses.py").write_text(
        "from backend.services.widget_core import x\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "test_other.py").write_text("import os\n", encoding="utf-8")
    globs = ["backend/tests/**/test_*.py"]
    runtime = {"kind": "run", "container": "none"}
    ids, info = selection.select_for_paths(tmp_path, runtime, "/nope", ["backend/services/widget_core.py"], globs)
    assert ids == ["backend/tests/test_uses.py"] and info["source"] == "import-graph"
    ids, _ = selection.select_for_paths(tmp_path, runtime, "/nope", ["backend/tests/test_other.py"], globs)
    assert ids == ["backend/tests/test_other.py"]
    ids, _ = selection.select_for_paths(tmp_path, runtime, "/nope", ["README.md"], globs)
    assert ids == []


def test_changed_without_paths_or_testmon_data_refuses_with_a_hint():
    from tplib import selection
    hint = selection.preflight_changed({"kind": "run", "container": "none"}, {"mode": "changed"}, False)
    assert hint and "--paths" in hint and "testmon" in hint
    assert selection.preflight_changed({"kind": "run", "container": "none"}, {"mode": "changed"}, True) is None
    assert selection.preflight_changed({"kind": "run", "container": "none"}, {"mode": "tier"}, False) is None


def test_check_floors_flags_zero_match_and_shortfall(tmp_path):
    import json as _json

    from tplib import runner
    floors = tmp_path / "floors.json"
    floors.write_text(_json.dumps({"floors": {"svc": {"classname_prefixes": ["test_a", "test_b"], "min_executed": 3}}}),
                      encoding="utf-8")
    ok = [{"node_id": f"backend/tests/test_a.py::t{i}", "status": "passed"} for i in range(3)]
    ok += [{"node_id": "backend/tests/test_b.py::t", "status": "passed"}]
    assert runner.check_floors(floors, ok) is None
    assert "zero tests" in runner.check_floors(floors, ok[:3])
    assert "floor=3" in runner.check_floors(floors, [ok[0], ok[3]])
    assert runner.check_floors(tmp_path / "absent.json", ok) is None


def test_commands_framework_maps_exit_codes_to_case_statuses(tmp_path):
    from types import SimpleNamespace

    from tplib import runner
    art = tmp_path / "art"
    art.mkdir()
    py = "import sys; sys.exit(int(sys.argv[1]))"
    tier = {"timeout_s": 60, "commands": [
        {"name": "green", "run": ["python", "-c", py, "0"]},
        {"name": "red", "run": ["python", "-c", py, "1"]},
        {"name": "broken", "run": ["python", "-c", py, "2"]}]}
    exe = runner.run_commands(SimpleNamespace(root=tmp_path), tier, art)
    by = {c["node_id"]: c["status"] for c in exe.cases}
    assert by == {"gates::green": "passed", "gates::red": "failed", "gates::broken": "error"}
    assert exe.status == "failed"
    assert all("failure" in c for c in exe.cases if c["status"] != "passed")


def test_path_tier_marker_applies_to_file_runs_but_not_node_ids():
    from types import SimpleNamespace

    from tplib import runner
    project = SimpleNamespace(profile={"pytest": {"command": ["python", "-m", "pytest"]}})
    tier = {"marker": "not slow", "marker_unless_node_id": True, "workers": 0}
    whole_file = runner._pytest_args(project, tier, ["backend/tests/test_x.py"], "claude", "/r.json", [], [])
    one_test = runner._pytest_args(project, tier, ["backend/tests/test_x.py::TestA::test_b"], "claude", "/r.json", [], [])
    assert "not slow" in whole_file
    assert "not slow" not in one_test


def test_lock_records_the_request_key_and_a_second_holder_can_queue(tmp_path):
    import threading
    import time

    from tplib import runner
    from tplib.lock import ProjectLock
    first = ProjectLock("p", tmp_path)
    key_a = runner.request_key("changed", ["a.py"])
    assert first.acquire(key_a)[0]
    first.set_run_id(7)
    second = ProjectLock("p", tmp_path)
    ok, held = second.acquire(runner.request_key("full", None))
    assert not ok and held.key == key_a and held.run_id == 7
    assert runner.request_key("changed", ["b.py", "a.py"]) == runner.request_key("changed", ["a.py", "b.py"])
    threading.Timer(0.3, first.release).start()
    started = time.time()
    assert second.wait_acquire(runner.request_key("full", None), timeout=10, poll=0.1)
    assert time.time() - started < 5
    second.release()


def test_service_queues_a_different_request_once(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tplib import runner, server
    from tplib.lock import ProjectLock
    project = profile.load_project("demo", _registry(tmp_path))
    project.name = "p"
    monkeypatch.setattr(server, "load_project", lambda name: project)
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)
    started = []
    monkeypatch.setattr(server.threading, "Thread", lambda target, args=(), daemon=True, name="": SimpleNamespace(
        start=lambda: started.append(args)))
    holder = ProjectLock("p", tmp_path)
    assert holder.acquire(runner.request_key("changed", None))[0]
    service = server.RunnerService(legion=object(), lock_dir=tmp_path)
    first = service.submit({"project": "p", "target": "fast", "trigger": "schedule"})
    second = service.submit({"project": "p", "target": "fast", "trigger": "schedule"})
    assert first == (202, {"accepted": True, "run_id": None, "queued": True, "duplicate": False,
                           "queue_position": None, "lane": "heavy", "waiting_for_run_id": None})
    assert second[1]["duplicate"] is True and len(started) == 1
    holder.release()


def test_lock_queue_is_first_come_first_served(tmp_path):
    import threading
    import time

    from tplib.lock import ProjectLock
    holder = ProjectLock("p", tmp_path)
    assert holder.acquire("h")[0]
    order: list[str] = []

    def waiter(name: str) -> None:
        lock = ProjectLock("p", tmp_path)
        assert lock.wait_acquire(name, timeout=20, poll=0.05)
        order.append(name)
        time.sleep(0.1)
        lock.release()

    threads = []
    for name in ("first", "second", "third"):
        t = threading.Thread(target=waiter, args=(name,))
        t.start()
        threads.append(t)
        time.sleep(0.15)
    holder.release()
    for t in threads:
        t.join(timeout=30)
    assert order == ["first", "second", "third"]


class _FlakyLegion(BaseHTTPRequestHandler):
    fail_creates = 0
    reject_creates = 0
    fail_results = 0
    results_ok_but_drop = False
    ingested: list = []
    created = 0

    def _json(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls = _FlakyLegion
        if self.path.endswith("/runs"):
            if cls.reject_creates > 0:
                cls.reject_creates -= 1
                return self._json(422, {"detail": "invalid"})
            if cls.fail_creates > 0:
                cls.fail_creates -= 1
                return self._json(503, {"detail": "busy"})
            cls.created += 1
            return self._json(201, {"run_id": 200 + cls.created})
        run_id = int(self.path.split("/")[-2])
        if run_id in cls.ingested:
            return self._json(409, {"detail": "already"})
        if cls.fail_results > 0:
            cls.fail_results -= 1
            if cls.results_ok_but_drop:
                cls.ingested.append(run_id)
            return self._json(500, {"detail": "boom"})
        cls.ingested.append(run_id)
        self._json(200, {"run_id": run_id, "verdict": {"status": body["status"]}, "text": f"ingested {run_id}"})

    def do_GET(self):
        run_id = int(self.path.split("?")[0].split("/")[-1])
        self._json(200, {"status": "passed", "verdict": {"status": "passed"}, "text": f"stored {run_id}"})

    def log_message(self, *a):
        pass


@pytest.fixture()
def flaky_legion():
    cls = _FlakyLegion
    cls.fail_creates = cls.fail_results = cls.created = cls.reject_creates = 0
    cls.results_ok_but_drop = False
    cls.ingested = []
    srv = HTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield LegionClient(f"http://127.0.0.1:{srv.server_address[1]}", backoff=(0.0,))
    srv.shutdown()


def test_create_run_retries_5xx_then_succeeds(flaky_legion):
    _FlakyLegion.fail_creates = 2
    assert flaky_legion.create_run({"project_id": 1}) == 201


def test_create_run_gives_up_after_three_attempts(flaky_legion):
    from tplib.legion import LegionError
    _FlakyLegion.fail_creates = 3
    with pytest.raises(LegionError):
        flaky_legion.create_run({"project_id": 1})
    assert _FlakyLegion.fail_creates == 0


def test_post_results_409_after_dropped_response_returns_stored_verdict(flaky_legion):
    _FlakyLegion.fail_results = 1
    _FlakyLegion.results_ok_but_drop = True
    resp = flaky_legion.post_results(7, {"status": "passed"})
    assert resp["text"] == "stored 7"


def test_post_results_first_attempt_409_is_not_retried(flaky_legion):
    from tplib.legion import LegionError
    _FlakyLegion.ingested.append(9)
    with pytest.raises(LegionError) as ei:
        flaky_legion.post_results(9, {"status": "passed"})
    assert ei.value.status == 409


def _write_pending(root, name, run_id, day="2026-09-29"):
    d = root / "demo" / day / name
    d.mkdir(parents=True)
    (d / "pending_ingest.json").write_text(json.dumps({
        "project": "demo", "run_id": run_id,
        "payload": {"status": "passed", "cases": []},
        "create_run": {"project_id": 1, "target": "t"}}))
    return d


def test_drain_ingests_pending_once_and_marks_ingested(tmp_path, flaky_legion):
    a = _write_pending(tmp_path, "a", None)
    b = _write_pending(tmp_path, "b", 55)
    first = runner.drain_pending(flaky_legion, tmp_path)
    assert len(first) == 2 and not any(x.startswith("failed") for x in first)
    assert (a / "ingested.json").exists() and (b / "ingested.json").exists()
    assert not (a / "pending_ingest.json").exists()
    assert (a / "verdict.txt").read_text().startswith("ingested")
    assert runner.drain_pending(flaky_legion, tmp_path) == []
    assert _FlakyLegion.created == 1 and sorted(_FlakyLegion.ingested) == [55, 201]


def test_drain_stops_at_first_unreachable_and_restores_pending(tmp_path):
    a = _write_pending(tmp_path, "a", None)
    b = _write_pending(tmp_path, "b", None)
    dead = LegionClient("http://127.0.0.1:9", timeout=1, backoff=(0.0,))
    out = runner.drain_pending(dead, tmp_path)
    assert len(out) == 1 and out[0].startswith("failed")
    assert (a / "pending_ingest.json").exists() and (b / "pending_ingest.json").exists()


def test_drain_claim_prevents_double_ingest_and_stale_claim_is_released(tmp_path, flaky_legion):
    d = _write_pending(tmp_path, "a", 77)
    claim = d / "ingesting.1.1.json"
    (d / "pending_ingest.json").rename(claim)
    assert runner.drain_pending(flaky_legion, tmp_path) == []
    assert _FlakyLegion.ingested == []
    old = os.path.getmtime(claim) - runner.CLAIM_STALE_S - 10
    os.utime(claim, (old, old))
    out = runner.drain_pending(flaky_legion, tmp_path)
    assert out == ["run 77 <- a"] and _FlakyLegion.ingested == [77]


def test_start_and_run_drains_earlier_pending_first(tmp_path, monkeypatch, flaky_legion):
    art = tmp_path / "art"
    old = _write_pending(art, "old", 88)
    project = profile.load_project("demo", _registry(tmp_path))
    exe = runner.Execution("passed", [_ONE_PASS], None, 0, None)
    monkeypatch.setattr(runner, "execute_framework", lambda *a, **k: exe)
    lock = ProjectLock("demo", tmp_path / "locks")
    lock.acquire()
    outcome = runner.start_and_run(project, None, "manual", flaky_legion, lock, artifacts_root=art)
    lock.release()
    assert outcome.run_id == 201
    assert (old / "ingested.json").exists() and 88 in _FlakyLegion.ingested


def test_drain_rejected_item_is_parked_and_does_not_block_the_queue(tmp_path, flaky_legion):
    bad = _write_pending(tmp_path, "a-bad", None)
    good = _write_pending(tmp_path, "b-good", 66)
    _FlakyLegion.reject_creates = 1
    out = runner.drain_pending(flaky_legion, tmp_path)
    assert out[0].startswith("rejected") and out[1] == "run 66 <- b-good"
    assert (bad / "pending_ingest.rejected.json").exists() and not (bad / "pending_ingest.json").exists()
    assert (good / "ingested.json").exists()
    assert runner.drain_pending(flaky_legion, tmp_path) == []


def test_create_rejected_is_reported_as_rejected_and_not_saved_for_replay(tmp_path, monkeypatch, flaky_legion):
    project = profile.load_project("demo", _registry(tmp_path))
    exe = runner.Execution("passed", [_ONE_PASS], None, 0, None)
    monkeypatch.setattr(runner, "execute_framework", lambda *a, **k: exe)
    _FlakyLegion.reject_creates = 1
    lock = ProjectLock("demo", tmp_path / "locks")
    lock.acquire()
    outcome = runner.start_and_run(project, None, "manual", flaky_legion, lock, artifacts_root=tmp_path / "art")
    lock.release()
    assert outcome.run_id is None and "rejected" in outcome.text
    assert not (outcome.artifact_dir / "pending_ingest.json").exists()


def test_fit_rows_keeps_failures_and_stays_under_the_budget():
    from tplib import parsers
    rows = [{"node_id": f"t::{i:05d}", "status": "passed", "body_hash": "h", "pad": "x" * 200} for i in range(200)]
    rows.append({"node_id": "t::fail", "status": "failed", "body_hash": "h", "pad": "x" * 200})
    kept = parsers.fit_rows(rows, {r["node_id"]: "h" for r in rows}, budget=10_000)
    assert any(r["node_id"] == "t::fail" for r in kept)
    assert 0 < len(kept) < len(rows)
    assert parsers.fit_rows(rows[:3], {}, budget=10_000) == rows[:3]


def test_testmon_miss_for_a_file_falls_back_to_the_import_graph(tmp_path, monkeypatch):
    from tplib import selection
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "services").mkdir()
    for name in ("known_mod", "missing_mod"):
        (tmp_path / "backend" / "services" / f"{name}.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "test_a.py").write_text("from backend.services.known_mod import x\n", encoding="utf-8")
    (tmp_path / "backend" / "tests" / "test_b.py").write_text("from backend.services.missing_mod import x\n", encoding="utf-8")
    monkeypatch.setattr(selection, "testmon_pairs", lambda *a, **k: [["backend/services/known_mod.py", "backend/tests/test_a.py::t"]])
    ids, info = selection.select_for_paths(
        tmp_path, {"kind": "exec", "container": "c"}, "/d",
        ["backend/services/known_mod.py", "backend/services/missing_mod.py"], ["backend/tests/**/test_*.py"])
    assert ids == ["backend/tests/test_a.py::t", "backend/tests/test_b.py"]
    assert info["source"] == "testmon+import-graph" and info["import_graph_files"] == ["backend/services/missing_mod.py"]


def test_emit_survives_characters_the_console_cannot_encode():
    import io

    from tplib import printer
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    printer.emit(["PASS ✔ done"], stream=stream)
    stream.flush()
    assert raw.getvalue().startswith(b"PASS ")


def test_vitest_container_paths_strip_the_repo_subdir():
    assert runner.vitest_container_paths(["frontend/src/X.test.tsx", r"frontend\src\Y.test.ts"], "frontend") == [
        "src/X.test.tsx", "src/Y.test.ts"]
    assert runner.vitest_container_paths(["src/Z.test.ts"], "frontend") == ["src/Z.test.ts"]
    assert runner.vitest_container_paths(["frontend/src/X.test.tsx"], "") == ["frontend/src/X.test.tsx"]
    assert runner.vitest_container_paths(None, "frontend") == []


def test_tmpfs_masks_only_paths_that_exist_under_a_readonly_mount(tmp_path):
    """Docker cannot mount over a path a read-only bind mount lacks; a missing live-data dir needs no mask."""
    from tplib import runner
    (tmp_path / "accounts" / "personal" / "data").mkdir(parents=True)
    runtime = {"mounts": [{"host": ".", "container": "/opt/app", "mode": "ro"}],
               "tmpfs": ["/opt/app/accounts/personal/data", "/opt/app/accounts/work/data", "/scratch"]}
    assert runner.tmpfs_masks(tmp_path, runtime) == ["/opt/app/accounts/personal/data", "/scratch"]
    assert runner.tmpfs_masks(tmp_path, {"mounts": []}) == []


def test_commands_resolve_windows_cmd_shims_through_path(tmp_path, monkeypatch):
    """`npm` is npm.cmd on Windows and CreateProcess will not find it bare, so a gate named `npm` must resolve."""
    from types import SimpleNamespace

    from tplib import runner
    seen: list[str] = []

    def fake_run(argv, timeout, out_file=None, cwd=None, env=None):
        seen.append(argv[0])
        return 0, "ok"

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: f"/resolved/{name}.cmd" if name == "npm" else None)
    art = tmp_path / "art"
    art.mkdir()
    tier = {"timeout_s": 60, "commands": [{"name": "lint", "run": ["npm", "run", "lint"]},
                                          {"name": "absent", "run": ["no-such-tool", "x"]}]}
    exe = runner.run_commands(SimpleNamespace(root=tmp_path), tier, art)
    assert seen == ["/resolved/npm.cmd", "no-such-tool"] and exe.status == "passed"


def test_plain_changed_hint_names_paths_when_the_runtime_cannot_record_testmon():
    from tplib import selection
    run_hint = selection.preflight_changed({"kind": "run", "container": "none"}, {"mode": "changed"}, False)
    assert "--paths" in run_hint and "testctl run <project>:testmon" not in run_hint


def test_host_vitest_runs_npx_in_the_repo_subdir_and_ids_are_repo_relative(tmp_path, monkeypatch):
    import json as _json
    from types import SimpleNamespace

    from tplib import runner
    web = tmp_path / "console" / "web"
    (web / "node_modules").mkdir(parents=True)
    (web / "src").mkdir()
    (web / "src" / "a.test.ts").write_text("x", encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    calls: dict = {}

    def fake_run(argv, timeout, out_file=None, cwd=None, env=None):
        calls["argv"], calls["cwd"] = argv, cwd
        out = next(a for a in argv if a.startswith("--outputFile=")).split("=", 1)[1]
        Path(out).write_text(_json.dumps({"testResults": [{
            "name": (web / "src" / "a.test.ts").as_posix(), "status": "passed",
            "assertionResults": [{"status": "passed", "fullName": "a works", "duration": 3}]}]}), encoding="utf-8")
        return 0, ""

    monkeypatch.setattr(runner, "_run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/npx" if name == "npx" else None)
    project = SimpleNamespace(root=tmp_path, profile={"vitest": {"kind": "host", "repo_subdir": "console/web"}})
    exe = runner._vitest_call(project, {"timeout_s": 60}, ["run", "src/a.test.ts"], art, "")
    assert exe.status == "passed" and Path(calls["cwd"]) == web
    assert calls["argv"][:3] == ["/bin/npx", "vitest", "run"]
    assert [c["node_id"] for c in exe.cases] == ["console/web/src/a.test.ts::a works"]
    (web / "node_modules").rmdir()
    missing = runner._vitest_call(project, {"timeout_s": 60}, ["run"], art, "")
    assert missing.status == "error" and "node_modules" in missing.error_summary


def test_commands_pass_a_gates_own_env_to_its_process(tmp_path):
    from types import SimpleNamespace

    from tplib import runner
    art = tmp_path / "art"
    art.mkdir()
    code = "import os, sys; sys.exit(0 if os.environ.get('GATE_FLAG') == 'yes' else 1)"
    tier = {"timeout_s": 60, "commands": [{"name": "with", "run": ["python", "-c", code], "env": {"GATE_FLAG": "yes"}},
                                          {"name": "without", "run": ["python", "-c", code]}]}
    exe = runner.run_commands(SimpleNamespace(root=tmp_path), tier, art)
    assert {c["node_id"]: c["status"] for c in exe.cases} == {"gates::with": "passed", "gates::without": "failed"}


def test_a_large_path_selection_spills_into_a_pytest_argfile():
    written: dict[str, str] = {}

    def write(remote, text):
        written[remote] = text
        return 0

    paths = [f"backend/tests/test_{i:04d}_some_long_module_name.py" for i in range(1700)]
    args = ["python", "-m", "pytest", *paths, "-n0", "--json-report"]
    out = runner.spill_paths_to_argfile(["docker", "exec", "c"], args, len(paths), 3, write, "/tmp/tp/r.json.args")
    assert out == ["python", "-m", "pytest", "@/tmp/tp/r.json.args", "-n0", "--json-report"]
    assert written["/tmp/tp/r.json.args"].splitlines() == paths


def test_control_a_small_selection_or_a_failed_write_keeps_the_paths_inline():
    args = ["python", "-m", "pytest", "backend/tests/test_a.py", "-n0"]
    assert runner.spill_paths_to_argfile(["docker"], args, 1, 3, lambda r, t: 0, "/x") == args
    big = ["python", "-m", "pytest", *[f"backend/tests/test_{i}.py" * 3 for i in range(2000)], "-n0"]
    assert runner.spill_paths_to_argfile(["docker"], big, 2000, 3, lambda r, t: 1, "/x") == big
