"""Lane-probe retry rule (2026-09-25): a cloud lane is retried once before it
reads DOWN (NIM cold starts); a local lane never is."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")
_SPEC = importlib.util.spec_from_file_location("bifrost_exporter", Path(__file__).resolve().parents[1] / "exporter.py")
exporter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(exporter)


def _scripted(results):
    calls = []

    def fake(model, kind, vk, timeout):
        calls.append(model)
        return results[len(calls) - 1]

    return fake, calls


def test_cloud_lane_cold_start_then_warm_is_up(monkeypatch):
    fake, calls = _scripted([(False, 60000.0), (True, 600.0)])
    monkeypatch.setattr(exporter, "_probe_lane_once", fake)
    ok, ms = exporter._probe_lane("nvidia-nim/nvidia/nemotron-3.5-lightning-30b-a3b", "chat", "vk", 60)
    assert ok is True and len(calls) == 2 and ms == pytest.approx(60600.0)


def test_cloud_lane_failing_twice_is_down(monkeypatch):
    fake, calls = _scripted([(False, 100.0), (False, 100.0)])
    monkeypatch.setattr(exporter, "_probe_lane_once", fake)
    ok, _ = exporter._probe_lane("aion/aion-labs/aion-2.0", "chat", "vk", 60)
    assert ok is False and len(calls) == 2


def test_local_lane_is_never_retried(monkeypatch):
    fake, calls = _scripted([(False, 120000.0), (True, 1.0)])
    monkeypatch.setattr(exporter, "_probe_lane_once", fake)
    ok, _ = exporter._probe_lane("vllm-local/qwen3.8-27b", "chat", "vk", 120)
    assert ok is False and len(calls) == 1


def test_success_first_try_makes_one_call(monkeypatch):
    fake, calls = _scripted([(True, 300.0)])
    monkeypatch.setattr(exporter, "_probe_lane_once", fake)
    assert exporter._probe_lane("groq/openai/gpt-oss-20b", "chat", "vk", 60) == (True, 300.0)
    assert len(calls) == 1
