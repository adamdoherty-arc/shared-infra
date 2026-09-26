"""bifrost_lane_eval_* gauge parsing (WS7 lane-eval harness, 2026-09-25).

Same import-by-file-path pattern as test_probe_retry.py: exporter.py isn't a
package, so it's loaded via importlib.util.spec_from_file_location."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")
# Loaded once by conftest.py (avoids a duplicate-timeseries registration when
# this module and test_probe_retry.py both import exporter.py).
exporter = sys.modules["bifrost_exporter"]


def _set_latest(tmp_path: Path, monkeypatch, payload: dict) -> None:
    state_dir = tmp_path / "lane_eval"
    state_dir.mkdir(parents=True, exist_ok=True)
    latest = state_dir / "latest.json"
    latest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(exporter, "LANE_EVAL_STATE_DIR", state_dir)
    monkeypatch.setattr(exporter, "LANE_EVAL_LATEST", latest)


def test_missing_latest_json_sets_age_negative_one(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "LANE_EVAL_STATE_DIR", tmp_path / "nope")
    monkeypatch.setattr(exporter, "LANE_EVAL_LATEST", tmp_path / "nope" / "latest.json")
    exporter._scrape_lane_eval()
    assert exporter.lane_eval_age_seconds._value.get() == -1


def test_valid_latest_json_sets_pass_ratio_and_age(tmp_path, monkeypatch):
    now = time.time()
    payload = {
        "generated_at_unixtime": now - 120,
        "lanes": [
            {
                "provider": "vllm-local",
                "model": "qwen3.8-27b",
                "category_pass_ratio": {"json": 0.83, "code": 1.0, "tools": None},
            },
            {
                "provider": "groq",
                "model": "openai/gpt-oss-120b",
                "category_pass_ratio": {"json": 0.5},
            },
        ],
    }
    _set_latest(tmp_path, monkeypatch, payload)
    exporter._scrape_lane_eval()

    age = exporter.lane_eval_age_seconds._value.get()
    assert 110 <= age <= 130

    json_ratio = exporter.lane_eval_pass_ratio.labels("vllm-local", "qwen3.8-27b", "json")._value.get()
    code_ratio = exporter.lane_eval_pass_ratio.labels("vllm-local", "qwen3.8-27b", "code")._value.get()
    groq_ratio = exporter.lane_eval_pass_ratio.labels("groq", "openai/gpt-oss-120b", "json")._value.get()
    assert json_ratio == pytest.approx(0.83)
    assert code_ratio == pytest.approx(1.0)
    assert groq_ratio == pytest.approx(0.5)


def test_malformed_json_increments_scrape_errors_and_does_not_raise(tmp_path, monkeypatch):
    state_dir = tmp_path / "lane_eval"
    state_dir.mkdir(parents=True, exist_ok=True)
    latest = state_dir / "latest.json"
    latest.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(exporter, "LANE_EVAL_STATE_DIR", state_dir)
    monkeypatch.setattr(exporter, "LANE_EVAL_LATEST", latest)
    before = exporter.exporter_scrape_errors_total.labels(table="lane_eval")._value.get()
    exporter._scrape_lane_eval()
    after = exporter.exporter_scrape_errors_total.labels(table="lane_eval")._value.get()
    assert after == before + 1


def test_missing_generated_at_unixtime_sets_age_negative_one(tmp_path, monkeypatch):
    _set_latest(tmp_path, monkeypatch, {"lanes": []})
    exporter._scrape_lane_eval()
    assert exporter.lane_eval_age_seconds._value.get() == -1
