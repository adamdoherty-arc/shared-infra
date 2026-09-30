"""Requirement tracing: parsers lift feature_slug / requirement_ids from pytest metadata and vitest titles."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import parsers  # noqa: E402


def _pytest_report(*tests):
    return {"tests": list(tests), "collectors": []}


def _test(nodeid, metadata=None):
    t = {"nodeid": nodeid, "outcome": "passed", "call": {"duration": 0.01}}
    if metadata is not None:
        t["metadata"] = metadata
    return t


def test_pytest_metadata_becomes_slug_and_requirements(tmp_path):
    report = _pytest_report(_test("backend/tests/t.py::a", {
        "feature_slug": "product_feature:bitcoin-lab", "requirement_ids": ["FR-016", "FR-016", "SC-001"]}))
    (case,) = parsers.parse_pytest_json(report, tmp_path)
    assert case["feature_slug"] == "product_feature:bitcoin-lab"
    assert case["requirement_ids"] == ["FR-016", "SC-001"]


def test_pytest_short_slug_is_resolved_to_the_full_slug(tmp_path):
    report = _pytest_report(_test("t.py::a", {"feature_slug": "bitcoin-lab", "requirement_ids": ["FR-001"]}))
    (case,) = parsers.parse_pytest_json(report, tmp_path)
    assert case["feature_slug"] == "product_feature:bitcoin-lab"


def test_control_untagged_pytest_case_is_none_and_empty(tmp_path):
    report = _pytest_report(_test("t.py::a"), _test("t.py::b", {"unrelated": 1}), _test("t.py::c", "junk"))
    for case in parsers.parse_pytest_json(report, tmp_path):
        assert case["feature_slug"] is None
        assert case["requirement_ids"] == []


def test_malformed_metadata_never_raises(tmp_path):
    report = _pytest_report(_test("t.py::a", {"feature_slug": 7, "requirement_ids": "FR-001"}))
    (case,) = parsers.parse_pytest_json(report, tmp_path)
    assert case["feature_slug"] is None and case["requirement_ids"] == []


def _vitest(tmp_path, header, full_name):
    f = tmp_path / "src" / "a.test.ts"
    f.parent.mkdir(parents=True)
    f.write_text(header + "\nimport x\n", encoding="utf-8")
    report = {"testResults": [{"name": str(f).replace("\\", "/"), "status": "passed", "assertionResults": [
        {"status": "passed", "fullName": full_name, "title": full_name, "duration": 1}]}]}
    return parsers.parse_vitest_json(report, tmp_path, container_root=str(tmp_path).replace("\\", "/"))


def test_vitest_title_tag_and_file_header_resolve_slug_and_requirements(tmp_path):
    (case,) = _vitest(tmp_path, "// feature: bitcoin-lab", "board renders [FR-012, SC-001] the bet")
    assert case["requirement_ids"] == ["FR-012", "SC-001"]
    assert case["feature_slug"] == "product_feature:bitcoin-lab"


def test_vitest_describe_slug_tag_wins_over_header(tmp_path):
    (case,) = _vitest(tmp_path, "// feature: labs", "[product_feature:bitcoin-lab] board [FR-003] renders")
    assert case["feature_slug"] == "product_feature:bitcoin-lab"
    assert case["requirement_ids"] == ["FR-003"]


def test_control_vitest_untagged_title_is_none_and_empty(tmp_path):
    (case,) = _vitest(tmp_path, "// feature: bitcoin-lab", "board renders FR-012 without brackets")
    assert case["feature_slug"] is None
    assert case["requirement_ids"] == []


def test_vitest_missing_header_with_tag_gives_none_slug(tmp_path):
    (case,) = _vitest(tmp_path, "// no header", "renders [FR-012]")
    assert case["requirement_ids"] == ["FR-012"]
    assert case["feature_slug"] is None


def test_sparse_selection_always_sends_traced_rows_even_when_the_body_hash_is_known():
    traced = {"node_id": "a", "status": "passed", "body_hash": "h", "requirement_ids": ["FR-001"]}
    plain = {"node_id": "b", "status": "passed", "body_hash": "h", "requirement_ids": []}
    kept = parsers.select_rows([traced, plain], {"a": "h", "b": "h"}, True)
    assert kept == [traced]
