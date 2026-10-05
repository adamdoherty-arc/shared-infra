"""The playwright tier: object pages, scripted flows and the case names that say which mode failed.

2026-10-05 (ADA Labs 2.0 Wave 0r-core): Admin Mode is client state, so a smoke of `/clients?tab=rules` in Admin Mode
needs `runner.py smoke <url> --admin-mode --assert-testid=admin-mode-banner`, and the edit round trip is a scripted
flow (`runner.py flow <name> <base_url> --admin-mode`). A tier's `pages` may now carry objects, and `flows` is new.

This gate would pass trivially if the fake runner accepted a banner assertion without `--admin-mode` (it fails
without the flag, exactly as the real runner does, so the control only passes when the flag is translated), if
every case named only the URL path (the same page in two modes would be one case, and 18 `/labs/bitcoin?tab=...`
pages already were), or if an unknown key were ignored (a typo would run the weaker check and report a pass).
The negative twin `assert_absent_testid` has the mirror-image fake: it FAILS when the absence flag arrives together
with `--admin-mode` (the banner is present then), so a tier that dropped the flag from an Admin Mode page would pass
here only because nothing was asserted, and the sabotage test below pins that the flag really is on the command line.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shipped_profiles import load_shipped_project  # noqa: E402

from tplib import parsers, runner  # noqa: E402
from tplib.profile import Project  # noqa: E402

BASE = "http://x:1"

FAKE_RUNNER = '''
import json
import os
import sys
import time

argv = sys.argv[1:]
with open(__RECORD__, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
with open(__RECORD__ + ".budget", "a", encoding="utf-8") as fh:
    fh.write(json.dumps(os.environ.get("TESTCTL_ITEM_TIMEOUT_S")) + "\\n")
print("noise before the verdict line")
if argv[0] == "smoke":
    url = argv[1]
    if "/slow" in url:
        time.sleep(1.3)
    if "--assert-absent-testid=admin-mode-banner" in argv and "--admin-mode" in argv:
        out = {"status": "failure", "reason": "assertion_testid_present"}
    elif "--assert-testid=admin-mode-banner" in argv and "--admin-mode" not in argv:
        out = {"status": "failure", "reason": "assertion_missing_testid"}
    elif "/broken" in url:
        out = {"status": "failure", "reason": "no_render", "console_errors": ["Uncaught TypeError: x"]}
    else:
        out = {"status": "success", "smoke_mode": "browser"}
else:
    name = argv[1]
    if name == "failing_flow" or (name == "needs_admin" and "--admin-mode" not in argv):
        out = {"status": "failure", "mode": "flow", "name": name, "steps": [],
               "reason": "assertion_failed: step 2 failed", "detail": "Traceback\\nAssertionError: step 2 failed"}
    else:
        out = {"status": "success", "mode": "flow", "name": name, "steps": ["opened /", "checked"]}
print(json.dumps(out))
print("0")
sys.exit(0 if out["status"] == "success" else 1)
'''


def make_tier(tmp_path: Path, **extra) -> tuple[Project, dict, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "argv.jsonl"
    (tmp_path / "fake_runner.py").write_text(FAKE_RUNNER.replace("__RECORD__", repr(str(record))), encoding="utf-8")
    tier = {"framework": "playwright", "rss_mb": 0, "runner": "fake_runner.py", "base_url": BASE, "timeout_s": 600, **extra}
    art = tmp_path / "art"
    art.mkdir(exist_ok=True)
    return Project(name="demo", root=tmp_path, legion_project_id=1, profile={"tiers": {"e2e_demo": tier}}), tier, art, record


def run_tier(tmp_path: Path, **extra):
    project, tier, art, record = make_tier(tmp_path, **extra)
    return runner.run_playwright(project, tier, art), art, record


def recorded(record: Path) -> list[list[str]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]


def budgets(record: Path) -> list:
    path = Path(str(record) + ".budget")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def by_node(execution) -> dict[str, dict]:
    return {c["node_id"]: c for c in execution.cases}


def test_control_string_pages_run_exactly_as_before(tmp_path):
    execution, _, record = run_tier(tmp_path, pages=["/", "/alerts?x=1"])
    assert recorded(record) == [["smoke", f"{BASE}/"], ["smoke", f"{BASE}/alerts?x=1"]]
    assert execution.status == "passed"
    assert [c["node_id"] for c in execution.cases] == ["e2e::/", "e2e::/alerts"]


def test_control_an_object_page_passes_the_admin_assert_and_testid_flags(tmp_path):
    page = {"path": "/clients?tab=rules", "admin_mode": True, "assert": "Rules", "assert_testid": "admin-mode-banner"}
    execution, _, record = run_tier(tmp_path, pages=[page])
    assert recorded(record) == [["smoke", f"{BASE}/clients?tab=rules", "--admin-mode", "--assert=Rules",
                                 "--assert-testid=admin-mode-banner"]]
    assert execution.status == "passed"
    assert [c["node_id"] for c in execution.cases] == ["e2e::/clients?tab=rules [admin]"]


def test_control_an_object_page_without_admin_mode_adds_no_admin_flag(tmp_path):
    execution, _, record = run_tier(tmp_path, pages=[{"path": "/clients?tab=rules"}])
    assert recorded(record) == [["smoke", f"{BASE}/clients?tab=rules"]]
    assert [c["node_id"] for c in execution.cases] == ["e2e::/clients?tab=rules"]


def test_sabotage_the_banner_assertion_without_the_admin_flag_fails_the_tier(tmp_path):
    """Without admin_mode the strict fake runner fails the banner assertion, as the real runner does: this is the proof
    that the passing control above only passes because run_playwright translates admin_mode into --admin-mode."""
    execution, _, record = run_tier(tmp_path, pages=[{"path": "/", "assert_testid": "admin-mode-banner"}])
    assert recorded(record) == [["smoke", f"{BASE}/", "--assert-testid=admin-mode-banner"]]
    assert execution.status == "failed"
    failure = execution.cases[0]["failure"]
    assert failure["type"] == "SmokeFailure" and "assertion_missing_testid" in failure["message"]


def test_control_an_absence_page_passes_the_absent_flag_and_passes_without_admin_mode(tmp_path):
    page = {"path": "/clients?tab=rules", "assert_absent_testid": "admin-mode-banner"}
    execution, _, record = run_tier(tmp_path, pages=[page])
    assert recorded(record) == [["smoke", f"{BASE}/clients?tab=rules", "--assert-absent-testid=admin-mode-banner"]]
    assert execution.status == "passed"
    assert [c["node_id"] for c in execution.cases] == ["e2e::/clients?tab=rules"]


def test_sabotage_an_absence_page_in_admin_mode_fails_the_tier_with_the_runner_reason(tmp_path):
    """The banner is present in Admin Mode, so the strict fake fails the absence flag exactly as the real runner does:
    the failing verdict is the proof that run_playwright puts `--assert-absent-testid` on the command line at all."""
    page = {"path": "/", "admin_mode": True, "assert_absent_testid": "admin-mode-banner"}
    execution, _, record = run_tier(tmp_path, pages=[page])
    assert recorded(record) == [["smoke", f"{BASE}/", "--admin-mode", "--assert-absent-testid=admin-mode-banner"]]
    assert execution.status == "failed"
    failure = execution.cases[0]["failure"]
    assert failure["type"] == "SmokeFailure" and "assertion_testid_present" in failure["message"]


def test_control_both_directions_of_one_page_are_two_cases_and_both_pass(tmp_path):
    present = {"path": "/", "admin_mode": True, "assert_testid": "admin-mode-banner"}
    absent = {"path": "/", "assert_absent_testid": "admin-mode-banner"}
    execution, _, record = run_tier(tmp_path, pages=[present, absent])
    assert [(c["node_id"], c["status"]) for c in execution.cases] == [("e2e::/ [admin]", "passed"), ("e2e::/", "passed")]
    assert recorded(record) == [
        ["smoke", f"{BASE}/", "--admin-mode", "--assert-testid=admin-mode-banner"],
        ["smoke", f"{BASE}/", "--assert-absent-testid=admin-mode-banner"]]


def test_control_every_assertion_key_reaches_the_command_line_in_a_fixed_order(tmp_path):
    page = {"path": "/p", "admin_mode": True, "assert": "Rules", "assert_testid": "a", "assert_absent_testid": "b"}
    items = runner.playwright_items({"pages": [page]}, BASE)
    assert items[0]["argv"] == ["smoke", f"{BASE}/p", "--admin-mode", "--assert=Rules", "--assert-testid=a",
                                "--assert-absent-testid=b"]


def test_control_the_same_page_in_both_modes_is_two_cases_and_the_admin_one_passes(tmp_path):
    banner = {"path": "/clients?tab=rules", "assert_testid": "admin-mode-banner"}
    execution, _, _ = run_tier(tmp_path, pages=[banner, {**banner, "admin_mode": True}])
    assert [(c["node_id"], c["status"]) for c in execution.cases] == [
        ("e2e::/clients?tab=rules", "failed"), ("e2e::/clients?tab=rules [admin]", "passed")]
    assert execution.status == "failed"


def test_control_flows_run_with_the_flow_argv_and_are_cases(tmp_path):
    execution, art, record = run_tier(tmp_path, flows=[{"name": "admin_banner_probe", "admin_mode": True},
                                                       {"name": "plain_flow"}])
    assert recorded(record) == [["flow", "admin_banner_probe", BASE, "--admin-mode"], ["flow", "plain_flow", BASE]]
    assert execution.status == "passed"
    assert [c["node_id"] for c in execution.cases] == ["e2e::flow:admin_banner_probe [admin]", "e2e::flow:plain_flow"]
    log = (art / "output.log").read_text(encoding="utf-8")
    assert "== flow:admin_banner_probe [admin] rc=0" in log and "== flow:plain_flow rc=0" in log
    report = json.loads((art / "report.json").read_text(encoding="utf-8"))
    assert [r["label"] for r in report] == ["flow:admin_banner_probe [admin]", "flow:plain_flow"]
    assert report[0]["argv"] == ["flow", "admin_banner_probe", BASE, "--admin-mode"] and report[0]["output"]["steps"]


def test_control_pages_and_flows_share_one_tier_and_both_are_reported(tmp_path):
    execution, art, _ = run_tier(tmp_path, pages=["/", {"path": "/rules", "admin_mode": True}],
                                 flows=[{"name": "plain_flow"}])
    assert [c["node_id"] for c in execution.cases] == ["e2e::/", "e2e::/rules [admin]", "e2e::flow:plain_flow"]
    assert len(json.loads((art / "report.json").read_text(encoding="utf-8"))) == 3


def test_sabotage_a_failing_flow_fails_the_tier_and_names_the_flow_and_mode(tmp_path):
    execution, _, _ = run_tier(tmp_path, pages=["/"], flows=[{"name": "failing_flow", "admin_mode": True}])
    assert execution.status == "failed"
    cases = by_node(execution)
    assert cases["e2e::/"]["status"] == "passed"
    failed = cases["e2e::flow:failing_flow [admin]"]
    assert failed["status"] == "failed"
    assert failed["failure"]["type"] == "FlowFailure"
    assert "assertion_failed: step 2 failed" in failed["failure"]["message"]
    assert "AssertionError: step 2 failed" in failed["failure"]["trace_tail"]


def test_sabotage_a_flow_that_needs_the_admin_flag_fails_without_it(tmp_path):
    without, _, _ = run_tier(tmp_path / "plain", flows=[{"name": "needs_admin"}])
    assert without.status == "failed"
    with_flag, _, _ = run_tier(tmp_path / "admin", flows=[{"name": "needs_admin", "admin_mode": True}])
    assert with_flag.status == "passed", "control: the same flow passes once admin_mode is translated"


def test_control_a_failing_page_still_fails_with_its_console_errors_in_the_detail(tmp_path):
    execution, _, _ = run_tier(tmp_path, pages=["/broken"])
    failure = execution.cases[0]["failure"]
    assert execution.status == "failed" and failure["type"] == "SmokeFailure"
    assert "Uncaught TypeError: x" in failure["trace_tail"]


@pytest.fixture
def item_timeouts(monkeypatch) -> list[float]:
    seen: list[float] = []
    real = runner._run

    def spy(cmd, timeout, *args, **kwargs):
        seen.append(timeout)
        return real(cmd, timeout, *args, **kwargs)

    monkeypatch.setattr(runner, "_run", spy)
    return seen


def test_sabotage_the_per_item_timeout_counts_flows_as_well_as_pages(tmp_path, item_timeouts):
    """600 s over two pages and one flow is 200 s each; counting only the pages would hand each item 300 s."""
    execution, _, _ = run_tier(tmp_path, pages=["/", "/alerts"], flows=[{"name": "plain_flow"}])
    assert execution.status == "passed"
    assert item_timeouts == [200, 200, 200]


def test_control_the_per_item_timeout_keeps_its_30_second_floor(tmp_path, item_timeouts):
    run_tier(tmp_path, pages=["/"], flows=[{"name": "a"}, {"name": "b"}], timeout_s=40)
    assert item_timeouts == [30, 30, 30]


@pytest.mark.parametrize("extra, needle", [
    ({"pages": [{"path": "/x", "admin-mode": True}]}, "unknown key"),
    ({"pages": [{"path": "/x", "asert": "t"}]}, "unknown key"),
    ({"pages": [{"path": "/x", "admin_mode": "true"}]}, "admin_mode must be true or false"),
    ({"pages": [{"admin_mode": True}]}, "string path starting with"),
    ({"pages": [{"path": ""}]}, "string path starting with"),
    ({"pages": [{"path": "clients?tab=rules"}]}, "string path starting with"),
    ({"pages": [{"path": "/x", "assert_testid": ""}]}, "assert_testid must be a non-empty string"),
    ({"pages": [{"path": "/x", "admin_mode": True, "assert_testid": None}]}, "assert_testid must be a non-empty string"),
    ({"pages": [{"path": "/x", "assert": None}]}, "assert must be a non-empty string"),
    ({"pages": [{"path": "/x", "admin_mode": None}]}, "admin_mode must be true or false"),
    ({"pages": [{"path": "/x", "assert": 5}]}, "assert must be a non-empty string"),
    ({"pages": [{"path": "/x", "assert_absent_testid": ""}]}, "assert_absent_testid must be a non-empty string"),
    ({"pages": [{"path": "/x", "assert_absent_testid": None}]}, "assert_absent_testid must be a non-empty string"),
    ({"pages": [{"path": "/x", "assert_absent_testid": 5}]}, "assert_absent_testid must be a non-empty string"),
    ({"pages": [{"path": "/x", "assert-absent-testid": "a"}]}, "unknown key"),
    ({"pages": [{"path": "/x", "assert_testid": "a", "assert_absent_testid": "a"}]}, "requires and forbids"),
    ({"pages": [7]}, "must be a mapping"),
    ({"pages": ["/"], "flows": [{"admin_mode": True}]}, "plain identifier"),
    ({"pages": ["/"], "flows": [{"name": "../x"}]}, "plain identifier"),
    ({"pages": ["/"], "flows": [{"name": "a b"}]}, "plain identifier"),
    ({"pages": ["/"], "flows": [{"name": "a", "admn": True}]}, "unknown key"),
    ({"pages": ["/"], "flows": ["plain_flow"]}, "must be a mapping"),
    ({"pages": "/"}, "must be lists"),
    ({"pages": [], "flows": []}, "no pages and no flows"),
    ({}, "no pages and no flows"),
])
def test_sabotage_a_malformed_entry_is_an_error_and_nothing_runs(tmp_path, extra, needle):
    execution, _, record = run_tier(tmp_path, **extra)
    assert execution.status == "error" and needle in (execution.error_summary or "")
    assert recorded(record) == [], "a tier with a bad entry must not run the entries before it either"


def test_sabotage_a_blank_yaml_assertion_is_an_error_not_a_dropped_check(tmp_path):
    """`assert_testid:` with no value loads as None. The old `.get(key) is None` test read that as "no assertion", so an
    Admin Mode page ran without its banner check and reported a pass."""
    tier = yaml.safe_load("pages:\n  - path: /\n    admin_mode: true\n    assert_testid:\n")
    assert tier["pages"][0]["assert_testid"] is None, "control: YAML really does load a blank value as None"
    execution, _, record = run_tier(tmp_path, **tier)
    assert execution.status == "error" and "assert_testid must be a non-empty string" in (execution.error_summary or "")
    assert recorded(record) == []


def test_sabotage_a_blank_yaml_absence_assertion_is_an_error_not_a_dropped_check(tmp_path):
    """The same hole as the blank `assert_testid`: `assert_absent_testid:` loads as None, and a read-only twin whose
    absence check silently vanished would report a pass for a page that was only loaded."""
    tier = yaml.safe_load("pages:\n  - path: /\n    assert_absent_testid:\n")
    assert tier["pages"][0]["assert_absent_testid"] is None, "control: YAML really does load a blank value as None"
    execution, _, record = run_tier(tmp_path, **tier)
    assert execution.status == "error" and "assert_absent_testid must be a non-empty string" in (execution.error_summary or "")
    assert recorded(record) == []


def test_control_a_yaml_page_with_every_key_filled_in_runs_with_every_flag(tmp_path):
    tier = yaml.safe_load("pages:\n  - path: /\n    admin_mode: true\n    assert: Rules\n    assert_testid: admin-mode-banner\n")
    execution, _, record = run_tier(tmp_path, **tier)
    assert execution.status == "passed"
    assert recorded(record) == [["smoke", f"{BASE}/", "--admin-mode", "--assert=Rules", "--assert-testid=admin-mode-banner"]]
    absence = yaml.safe_load("pages:\n  - path: /\n    assert_absent_testid: admin-mode-banner\n")
    execution, _, record = run_tier(tmp_path / "absence", **absence)
    assert execution.status == "passed"
    assert recorded(record) == [["smoke", f"{BASE}/", "--assert-absent-testid=admin-mode-banner"]]


def test_sabotage_pages_that_differ_only_by_query_are_distinct_cases(tmp_path):
    """The 18 e2e_bitcoin pages all reduced to e2e::/labs/bitcoin, so one failing tab shared a case with 17 passing ones."""
    pages = ["/labs/bitcoin", "/labs/bitcoin?tab=board", "/labs/bitcoin?asset=ETH&tab=board"]
    items = runner.playwright_items({"pages": pages}, BASE)
    assert [i["label"] for i in items] == ["/labs/bitcoin", "/labs/bitcoin?tab=board", "/labs/bitcoin?asset=ETH&tab=board"]
    old = [urlparse(f"{BASE}{p}").path for p in pages]
    assert len(set(old)) == 1, "control: the old naming really did collapse them"


def test_control_distinct_paths_keep_their_old_path_only_case_names(tmp_path):
    items = runner.playwright_items({"pages": ["/chart?symbol=AAPL", "/labs", "/stocks/detail/AAPL"]}, BASE)
    assert [i["label"] for i in items] == ["/chart", "/labs", "/stocks/detail/AAPL"]


def test_control_a_repeated_page_gets_a_numbered_twin(tmp_path):
    items = runner.playwright_items({"pages": ["/a?x=1", "/a?x=1", {"path": "/a?x=1"}, {"path": "/a?x=1"}]}, BASE)
    assert [i["label"] for i in items] == ["/a", "/a?x=1", "/a?x=1 #2", "/a?x=1 #3"]


def test_control_two_flows_with_one_name_get_numbered_twins(tmp_path):
    items = runner.playwright_items({"pages": ["/"], "flows": [{"name": "plain_flow"}, {"name": "plain_flow"}]}, BASE)
    assert [i["label"] for i in items] == ["/", "flow:plain_flow", "flow:plain_flow #2"]


def test_sabotage_items_the_deadline_cut_off_fail_instead_of_vanishing(tmp_path):
    """The old loop broke out at the deadline and the skipped pages left no case, so a tier could pass on a fraction of
    its pages."""
    execution, _, record = run_tier(tmp_path, pages=["/slow", "/after_a"], flows=[{"name": "plain_flow"}], timeout_s=1)
    assert execution.status == "failed"
    assert [(c["node_id"], c["status"]) for c in execution.cases] == [
        ("e2e::/slow", "passed"), ("e2e::/after_a", "failed"), ("e2e::flow:plain_flow", "failed")]
    assert all("deadline" in c["failure"]["message"] for c in execution.cases[1:])
    assert [argv[1] for argv in recorded(record)] == [f"{BASE}/slow"], "nothing past the deadline may be started"


def test_control_inside_the_deadline_every_item_runs(tmp_path):
    execution, _, record = run_tier(tmp_path, pages=["/slow", "/after_a"], flows=[{"name": "plain_flow"}], timeout_s=600)
    assert execution.status == "passed" and len(recorded(record)) == 3


def test_sabotage_a_trailing_number_line_does_not_hide_the_verdict(tmp_path):
    """The fake prints `0` after the verdict; the old loop took the last line that parsed as JSON, got the int 0 and
    crashed on `.get`."""
    assert runner.last_json_object('noise\n{"status": "success"}\n0\n') == {"status": "success"}
    assert runner.last_json_object("no json here\n42\n[1]\n") == {}
    execution, _, _ = run_tier(tmp_path, pages=["/"])
    assert execution.status == "passed"


def test_control_parse_playwright_smoke_keeps_its_legacy_names_without_a_label():
    cases = parsers.parse_playwright_smoke([
        {"url": f"{BASE}/labs?tab=x", "returncode": 1, "output": {"status": "failure", "reason": "no_render"},
         "duration_ms": 9},
        {"url": f"{BASE}/labs", "label": "/labs [admin]", "returncode": 0, "output": {"status": "success"}}])
    assert [c["node_id"] for c in cases] == ["e2e::/labs", "e2e::/labs [admin]"]
    assert cases[0]["failure"]["type"] == "SmokeFailure"


def test_control_a_crashed_runner_with_no_json_fails_the_case_with_its_stderr(tmp_path):
    (tmp_path / "boom.py").write_text("import sys\nprint('Traceback: boom')\nsys.exit(3)\n", encoding="utf-8")
    project, tier, art, _ = make_tier(tmp_path, pages=["/"], flows=[{"name": "plain_flow"}])
    tier["runner"] = "boom.py"
    execution = runner.run_playwright(project, tier, art)
    assert execution.status == "failed"
    assert [c["status"] for c in execution.cases] == ["failed", "failed"]
    assert execution.cases[1]["failure"]["type"] == "FlowFailure" and "boom" in execution.cases[1]["failure"]["message"]


def test_sabotage_every_runner_process_is_told_its_kill_budget(tmp_path):
    """testctl kills a runner that outlives its per-item timeout and throws its output away, so a slow flow left its case
    with no verdict. The runner can only print first if it knows the budget: 600 s over two pages and a flow is 200 s each,
    and the report records it."""
    execution, art, record = run_tier(tmp_path, pages=["/", "/alerts"], flows=[{"name": "plain_flow"}])
    assert execution.status == "passed"
    assert budgets(record) == ["200", "200", "200"]
    report = json.loads((art / "report.json").read_text(encoding="utf-8"))
    assert [r["budget_s"] for r in report] == [200, 200, 200]


def test_control_the_budget_is_the_kill_timeout_and_keeps_the_30_second_floor(tmp_path, item_timeouts):
    _, _, record = run_tier(tmp_path, pages=["/"], flows=[{"name": "a"}, {"name": "b"}], timeout_s=40)
    assert [int(b) for b in budgets(record)] == item_timeouts == [30, 30, 30], \
        "a budget longer than the kill timeout would let the runner wait past its own death"


def test_control_a_runner_that_ignores_the_budget_still_runs_as_before(tmp_path):
    (tmp_path / "plain.py").write_text("import json\nprint(json.dumps({'status': 'success'}))\n", encoding="utf-8")
    project, tier, art, _ = make_tier(tmp_path, pages=["/"])
    tier["runner"] = "plain.py"
    assert runner.run_playwright(project, tier, art).status == "passed"


def test_the_budget_variable_is_pinned_to_the_ada_runner_copy():
    """`PLAYWRIGHT_BUDGET_ENV` and ADA's `ITEM_TIMEOUT_ENV` (playwright-testing/runner.py) are one name written twice, and a
    mismatch is silent: the runner would run unbounded and testctl would go on killing it with no verdict. ADA pins the same
    literal in `test_the_item_timeout_variable_is_pinned_to_the_testctl_copy`.

    This gate would pass trivially if only this side were pinned (renaming the ADA copy alone would break nothing here), so the
    source must also keep naming the other copy, which is what sends the next editor to it."""
    assert runner.PLAYWRIGHT_BUDGET_ENV == "TESTCTL_ITEM_TIMEOUT_S"
    source = (Path(__file__).resolve().parents[1] / "tplib" / "runner.py").read_text(encoding="utf-8")
    assert "ITEM_TIMEOUT_ENV" in source and "playwright-testing/runner.py" in source


def test_the_flow_name_pattern_is_pinned_to_the_ada_runner_copy():
    """`PLAYWRIGHT_FLOW_NAME` and ADA's `FLOW_NAME_PATTERN` (playwright-testing/runner.py) are one rule written twice, and a
    tier entry that one side accepts and the other refuses is a flow that errors only at run time. ADA pins the same literal
    in its own repo (`test_the_flow_name_pattern_is_pinned_to_the_testctl_copy`).

    This gate would pass trivially if only this side were pinned (changing the ADA copy alone would break nothing here), so
    the source must also keep naming the other copy, which is what sends the next editor to it."""
    assert runner.PLAYWRIGHT_FLOW_NAME.pattern == r"[A-Za-z][A-Za-z0-9_]{0,63}"
    source = (Path(__file__).resolve().parents[1] / "tplib" / "runner.py").read_text(encoding="utf-8")
    assert "FLOW_NAME_PATTERN" in source and "playwright-testing/runner.py" in source


def test_control_the_shipped_ada_playwright_tiers_are_valid_and_cover_admin_mode(tmp_path):
    project = load_shipped_project("ada", tmp_path)
    seen = 0
    for name in project.tier_names:
        tier = project.tier(name)
        if tier.get("framework") != "playwright":
            continue
        items = runner.playwright_items(tier, tier["base_url"])
        assert len({i["label"] for i in items}) == len(items), f"tier {name} has a duplicate case name"
        seen += 1
    assert seen >= 3, "ADA ships e2e, e2e_bitcoin, e2e_labs and the owner-controls tiers"
    probe = runner.playwright_items(project.tier("e2e_admin_probe"), "http://x")
    assert any(i["admin_mode"] and "--assert-testid=admin-mode-banner" in i["argv"] for i in probe)
    assert any(not i["admin_mode"] and "--assert-absent-testid=admin-mode-banner" in i["argv"] for i in probe), \
        "the probe must prove both directions: banner present with the seed AND absent without it"
    assert any(i["kind"] == "flow" and i["label"] == "flow:admin_seed_semantics_probe [admin]" for i in probe)
    owner = runner.playwright_items(project.tier("e2e_owner_controls"), "http://x")
    assert {"/clients?tab=rules", "/clients?tab=rules [admin]", "flow:admin_banner_probe [admin]"} <= {
        i["label"] for i in owner}
    readonly = next(i for i in owner if i["label"] == "/clients?tab=rules")
    assert "--assert-absent-testid=admin-mode-banner" in readonly["argv"] and "--admin-mode" not in readonly["argv"], \
        "the read-only twin of the Admin Mode page must assert the banner is absent, not merely load"
