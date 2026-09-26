"""Unit tests for scripts/lane_eval.py -- graders and lane discovery.

No network calls: every test exercises the pure-function graders and the
config-driven discovery logic directly, using in-memory config.json-shaped
dicts and temp files. Live-gateway behavior (actually calling Bifrost) is
exercised by running `python scripts/lane_eval.py` for real, not here --
see docs/LANE_QUALITY.md's "first-run headline" for that evidence.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "lane_eval", Path(__file__).resolve().parents[1] / "lane_eval.py"
)
lane_eval = importlib.util.module_from_spec(_SPEC)
sys.modules["lane_eval"] = lane_eval
_SPEC.loader.exec_module(lane_eval)


# ---------------------------------------------------------------------------
# Graders
# ---------------------------------------------------------------------------


def test_grade_json_schema_pass():
    task = {
        "schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
        "expected": {"name": "Ada"},
    }
    ok, _ = lane_eval.grade_json_schema('{"name": "Ada"}', task)
    assert ok


def test_grade_json_schema_strips_code_fence():
    task = {"schema": {"type": "object"}, "expected": None}
    ok, _ = lane_eval.grade_json_schema('```json\n{"a": 1}\n```', task)
    assert ok


def test_grade_json_schema_value_mismatch_fails():
    task = {"schema": {"type": "object"}, "expected": {"name": "Ada"}}
    ok, detail = lane_eval.grade_json_schema('{"name": "Bob"}', task)
    assert not ok and "value_mismatch" in detail


def test_grade_json_schema_invalid_json_fails():
    ok, detail = lane_eval.grade_json_schema("not json at all", {"schema": {"type": "object"}, "expected": None})
    assert not ok and "invalid_json" in detail


def test_grade_tool_call_pass():
    task = {"expected_function": "get_weather", "expected_args": {"location": "Tokyo", "unit": "celsius"}}
    call = {"name": "get_weather", "arguments_raw": json.dumps({"location": "Tokyo", "unit": "celsius"})}
    ok, _ = lane_eval.grade_tool_call(call, task)
    assert ok


def test_grade_tool_call_wrong_function():
    task = {"expected_function": "get_weather", "expected_args": {}}
    ok, detail = lane_eval.grade_tool_call({"name": "other_fn", "arguments_raw": "{}"}, task)
    assert not ok and "wrong_function" in detail


def test_grade_tool_call_no_call_emitted():
    ok, detail = lane_eval.grade_tool_call(None, {"expected_function": "x", "expected_args": {}})
    assert not ok and "no_tool_call" in detail


def test_grade_tool_call_numeric_tolerance():
    task = {"expected_function": "calculate", "expected_args": {"a": 47, "b": 128}}
    call = {"name": "calculate", "arguments_raw": json.dumps({"a": 47.0, "b": 128.0})}
    ok, _ = lane_eval.grade_tool_call(call, task)
    assert ok


def test_grade_exact_answer_pass_and_fail():
    task = {"expected_regex": "3[,.]?772"}
    assert lane_eval.grade_exact_answer("The answer is 3772.", task)[0]
    assert not lane_eval.grade_exact_answer("42", task)[0]


def test_grade_extraction_partial_credit():
    task = {"expected_fields": {"Invoice": "INV-1", "Total": "$5", "Due": "2026-01-01"}, "min_fields_matched": 2}
    ok, detail = lane_eval.grade_extraction("Invoice: INV-1\nTotal: $5\n", task)
    assert ok and "matched" in detail


def test_grade_extraction_below_threshold_fails():
    task = {"expected_fields": {"Invoice": "INV-1", "Total": "$5", "Due": "2026-01-01"}, "min_fields_matched": 3}
    ok, _ = lane_eval.grade_extraction("Invoice: INV-1\n", task)
    assert not ok


def test_grade_needle():
    task = {"context_spec": {"needle_answer_regex": "(?i)zebra-4471-quartz"}}
    assert lane_eval.grade_needle("ZEBRA-4471-QUARTZ", task)[0]
    assert not lane_eval.grade_needle("no idea", task)[0]


def test_grade_code_pass():
    task = {
        "function_name": "second_largest",
        "test_code": "assert second_largest([1, 2, 3]) == 2\n",
    }
    good = "```python\ndef second_largest(nums):\n    u = sorted(set(nums))\n    return u[-2]\n```"
    ok, _ = lane_eval.grade_code(good, task)
    assert ok


def test_grade_code_fails_on_bug():
    task = {
        "function_name": "second_largest",
        "test_code": "assert second_largest([10, 9, 1]) == 9\n",
    }
    buggy = "```python\ndef second_largest(nums):\n    u = sorted(set(nums))\n    return u[0]\n```"
    ok, detail = lane_eval.grade_code(buggy, task)
    assert not ok and "asserts_failed" in detail


def test_grade_code_no_function_found():
    ok, detail = lane_eval.grade_code("I refuse to write code.", {"function_name": "x", "test_code": "pass\n"})
    assert not ok and "no_function_definition" in detail


def test_grade_instruction_follow_sentence_and_char_constraints():
    task = {"constraints": {"min_sentences": 2, "max_sentences": 2, "forbid_chars": [","], "forbid_words": ["gauge"]}}
    ok, _ = lane_eval.grade_instruction_follow("This is one. This is two.", task)
    assert ok
    ok2, detail = lane_eval.grade_instruction_follow("This has a comma, right here.", task)
    assert not ok2 and "forbidden_char" in detail
    ok3, detail3 = lane_eval.grade_instruction_follow("A gauge measures things. Two.", task)
    assert not ok3 and "forbidden_word" in detail3


def test_grade_instruction_follow_format_regex():
    task = {"constraints": {"format_regex": r"^\s*2,3,5,7,11\s*$"}}
    assert lane_eval.grade_instruction_follow("2,3,5,7,11", task)[0]
    assert not lane_eval.grade_instruction_follow("2, 3, 5, 7, 11", task)[0]


def test_grade_summarize_keywords_pass_and_refusal():
    task = {"required_keywords": ["gateway", "provider"]}
    ok, _ = lane_eval.grade_summarize_keywords("The gateway routes to many providers.", task)
    assert ok
    ok2, detail2 = lane_eval.grade_summarize_keywords("I cannot help with that.", task)
    assert not ok2 and detail2 == "refused"
    ok3, detail3 = lane_eval.grade_summarize_keywords("This mentions only the gateway.", task)
    assert not ok3 and "missing_keywords" in detail3


# ---------------------------------------------------------------------------
# Context synthesis
# ---------------------------------------------------------------------------


def test_build_context_needle_is_deterministic_and_contains_sentence():
    spec = {"kind": "filler_with_needle", "target_tokens": 500, "seed": "s1", "needle_sentence": "SECRET-XYZ."}
    ctx1, _ = lane_eval.build_context(spec)
    ctx2, _ = lane_eval.build_context(spec)
    assert ctx1 == ctx2
    assert "SECRET-XYZ." in ctx1


def test_build_context_facts_present():
    ctx, _ = lane_eval.build_context({"kind": "filler_with_facts", "target_tokens": 300, "seed": "s2"})
    assert "INV-88214" in ctx and "$4,392.50" in ctx


# ---------------------------------------------------------------------------
# Lane discovery
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, providers: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"providers": providers}), encoding="utf-8")
    return p


def test_provider_models_dedupes_and_flattens():
    pcfg = {"keys": [{"models": ["a", "b"]}, {"models": ["b", "c"]}]}
    assert lane_eval._provider_models(pcfg) == ["a", "b", "c"]


def test_model_is_disabled_matches_substring_case_insensitive():
    patterns = ["kimi", "mistral"]
    assert lane_eval._model_is_disabled("moonshotai/Kimi-K2.6", patterns)
    assert not lane_eval._model_is_disabled("qwen/qwen3.8-27b", patterns)


def test_harvest_pinned_models_filters_placeholders(tmp_path):
    src = tmp_path / "router.py"
    src.write_text(
        'DEFAULT = "vllm-local/qwen3-chat"\n'
        'PLACEHOLDER = "provider/model"\n'
        'ALSO = "groq/openai/gpt-oss-120b"\n'
        'EMBED = "embed-local/Qwen/Qwen3-Embedding-0.6B"\n',
        encoding="utf-8",
    )
    known = {"vllm-local", "groq", "embed-local"}
    pinned = lane_eval.harvest_pinned_models([src], known)
    assert pinned["vllm-local"] == ["qwen3-chat"]
    assert pinned["groq"] == ["openai/gpt-oss-120b"]
    assert "embed-local" not in pinned
    assert "provider" not in pinned  # placeholder's fake "provider" prefix is not a known provider


def test_discover_lanes_skips_embed_disabled_and_no_models(tmp_path, monkeypatch):
    providers = {
        "vllm-local": {"keys": [{"models": ["qwen3.8-27b", "qwen3-chat"]}]},
        "embed-local": {"keys": [{"models": ["Qwen/Qwen3-Embedding-0.6B"]}]},
        "parked-provider": {"keys": [{"models": ["some-model"]}]},
        "empty-provider": {"keys": [{"models": []}]},
    }
    cfg_path = _write_config(tmp_path, providers)
    operator_disabled_path = tmp_path / "operator-disabled.json"
    operator_disabled_path.write_text(
        json.dumps({"providers": {"parked-provider": "reason"}, "model_patterns": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(lane_eval, "CONFIG_JSON", cfg_path)
    monkeypatch.setattr(lane_eval, "OPERATOR_DISABLED", operator_disabled_path)
    monkeypatch.setattr(lane_eval, "CONFIG_DB", tmp_path / "does-not-exist.db")
    monkeypatch.setattr(lane_eval, "PINNED_MODEL_SOURCES", [])

    lanes, skipped = lane_eval.discover_lanes(vk="", dry_run_ignore_vk=True)

    lane_providers = {lane["provider"] for lane in lanes}
    assert "vllm-local" in lane_providers
    assert "embed-local" not in lane_providers
    assert skipped["embed-local"] == "embed_lane_not_chat"
    assert skipped["parked-provider"] == "operator_disabled"
    assert skipped["empty-provider"] == "no_models"


def test_vk_allowed_providers_reads_governance_tables(tmp_path):
    db_path = tmp_path / "config.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE governance_virtual_keys (id TEXT, value TEXT)")
    conn.execute("CREATE TABLE governance_virtual_key_provider_configs (virtual_key_id TEXT, provider TEXT)")
    conn.execute("INSERT INTO governance_virtual_keys VALUES ('vk1', 'sk-bf-test')")
    conn.execute("INSERT INTO governance_virtual_key_provider_configs VALUES ('vk1', 'groq')")
    conn.execute("INSERT INTO governance_virtual_key_provider_configs VALUES ('vk1', 'vllm-local')")
    conn.commit()
    conn.close()

    allowed = lane_eval.vk_allowed_providers(db_path, "sk-bf-test")
    assert allowed == {"groq", "vllm-local"}

    assert lane_eval.vk_allowed_providers(db_path, "sk-bf-unknown") is None
    assert lane_eval.vk_allowed_providers(tmp_path / "missing.db", "sk-bf-test") is None


def test_resolve_probe_vk_strips_quotes_and_cr(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_bytes(b'OTHER=1\r\nINFRA_PROBE_VK="sk-bf-abc123"\r\n')
    monkeypatch.setattr(lane_eval, "ENV_FILE", env_path)
    assert lane_eval._resolve_probe_vk() == "sk-bf-abc123"


def test_resolve_probe_vk_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_eval, "ENV_FILE", tmp_path / "no-such-file.env")
    assert lane_eval._resolve_probe_vk() == ""


def test_best_lane_per_purpose_picks_highest_pass_ratio_then_lower_latency():
    summaries = [
        {"provider": "a", "model": "m1", "category_pass_ratio": {"json": 0.5}, "p50_latency_ms": 100},
        {"provider": "b", "model": "m2", "category_pass_ratio": {"json": 0.9}, "p50_latency_ms": 500},
        {"provider": "c", "model": "m3", "category_pass_ratio": {"json": 0.9}, "p50_latency_ms": 200},
    ]
    best = lane_eval._best_lane_per_purpose(summaries)
    assert best["json"] == "c/m3"
    assert best["tools"] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

def test_request_budget_has_reasoning_floor(monkeypatch):
    """Reasoning lanes need room to think; a 64-token task still asks for the floor."""
    sent = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"choices": [{"message": {"content": "3772"}, "finish_reason": "stop"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout):
        sent.update(json.loads(req.data))
        return _Resp()

    monkeypatch.setattr(lane_eval.urllib.request, "urlopen", _fake_urlopen)
    lane_eval.call_chat_completion("vk", "groq/x", [{"role": "user", "content": "q"}], 64, 5)
    assert sent["max_tokens"] == min(max(64, lane_eval.MIN_REQUEST_TOKENS), lane_eval.MAX_TOKENS_CAP) >= 1024


def test_length_cutoff_is_truncated_not_failed(monkeypatch):
    body = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    monkeypatch.setattr(lane_eval, "call_chat_completion",
                        lambda *a, **k: lane_eval.CallResult(True, 200, body, 10.0))
    task = {"id": "t", "category": "reasoning", "type": "exact_answer", "prompt": "q", "answer_regex": "x"}
    lane = {"provider": "groq", "model": "x", "timeout_s": 5}
    rec = lane_eval.run_task("vk", lane, task, {})
    assert rec["outcome"] == "truncated"
    summary = lane_eval._summarize_lane({"provider": "groq", "model": "x", "is_pinned": True}, [rec])
    assert summary["truncated"] == 1
    assert summary["category_pass_ratio"]["reasoning"] is None
