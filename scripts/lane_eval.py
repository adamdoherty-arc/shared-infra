#!/usr/bin/env python3
"""scripts/lane_eval.py -- weekly model-quality evaluation harness for the
shared Bifrost gateway (WS7 follow-on, 2026-09-25).

Purpose: decide with DATA which free model serves which purpose (json
output, tool calls, reasoning, long-context, code) instead of the
hand-picked ladders in ADA/Legion. Runs a fixed, deterministically-graded
12-task suite (scripts/lane_eval_suite.json) against every active,
consumer-pinned lane through the real gateway (http://127.0.0.1:4445),
respecting each free-tier's rate limits, and publishes:

  - state/lane_eval/runs.jsonl   -- one append-only record per run
  - state/lane_eval/latest.json  -- machine-readable snapshot (exporter reads this)
  - docs/LANE_QUALITY.md         -- human-readable report (generated; do not hand-edit)

No LLM judge anywhere: every grader in this file is exact-match,
JSON-Schema-validated, keyword-checked, or subprocess-executed (the code
task). See `_grade_*` functions.

Lane discovery:
  1. Read bifrost/config.json for active providers/models (skip embed-local
     -- it never serves chat completions).
  2. Drop anything bifrost/operator-disabled.json bans (provider name or
     model-pattern substring match), mirroring scripts/gate.py's own check.
  3. Harvest every "provider/model" string literal any consumer pins today
     out of ADA's bifrost_ladder.py / llm_router.py and Legion's
     llm_router_policy.py, and keep the ones that actually exist in that
     provider's config.json model list -- these are evaluated first/always.
     A provider with no pinned model still gets one representative lane
     (first model in its config.json list) so it isn't invisible in the
     report.
  4. Read bifrost/config.db read-only (WAL-safe: `mode=ro&immutable=1`,
     never opened read-write from the host -- see 30-docker.md) for the
     probe VK's governance_virtual_key_provider_configs rows, exactly like
     infractl/probes/lanes.py's `vk_allowed_providers`. A provider the VK
     cannot reach (e.g. openrouter, ada-prod only) is skipped as
     `vk_not_allowed`, never scored as a failure.

Rate-limit discipline: fully sequential (no concurrent calls to any
provider), a small delay between calls, one retry on HTTP 429 with
backoff (recorded as `rate_limited`, not a grading failure), a hard cap on
total calls per run, and a bounded max_tokens per task.

Usage:
    python scripts/lane_eval.py                # full run, writes all outputs
    python scripts/lane_eval.py --dry-run       # discovery only, no network calls
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE_PATH = REPO_ROOT / "scripts" / "lane_eval_suite.json"
CONFIG_JSON = REPO_ROOT / "bifrost" / "config.json"
CONFIG_DB = REPO_ROOT / "bifrost" / "config.db"
OPERATOR_DISABLED = REPO_ROOT / "bifrost" / "operator-disabled.json"
ENV_FILE = REPO_ROOT / ".env"
STATE_DIR = REPO_ROOT / "state" / "lane_eval"
RUNS_JSONL = STATE_DIR / "runs.jsonl"
LATEST_JSON = STATE_DIR / "latest.json"

PINNED_MODEL_SOURCES = [
    Path("C:/code/ADA/backend/services/llm/bifrost_ladder.py"),
    Path("C:/code/ADA/backend/infrastructure/llm_router.py"),
    Path("C:/code/legion/backend/app/services/llm_router_policy.py"),
]

PURPOSE_CATEGORIES = ["json", "tools", "reasoning", "long_context", "code"]

# ---- tunables (env-overridable; all free-tier-safe defaults) --------------
BASE_URL = os.getenv("LANE_EVAL_BASE_URL", "http://127.0.0.1:4445").rstrip("/")
MAX_LANES = int(os.getenv("LANE_EVAL_MAX_LANES", "20"))
MAX_MODELS_PER_PROVIDER = int(os.getenv("LANE_EVAL_MAX_MODELS_PER_PROVIDER", "2"))
CALL_DELAY_S = float(os.getenv("LANE_EVAL_CALL_DELAY_S", "1.5"))
MAX_TOKENS_CAP = int(os.getenv("LANE_EVAL_MAX_TOKENS_CAP", "512"))
TIMEOUT_LOCAL_S = float(os.getenv("LANE_EVAL_TIMEOUT_LOCAL_S", "180"))
TIMEOUT_CLOUD_S = float(os.getenv("LANE_EVAL_TIMEOUT_CLOUD_S", "90"))
MAX_CALLS_PER_RUN = int(os.getenv("LANE_EVAL_MAX_CALLS_PER_RUN", "300"))
RETRY_BACKOFF_S = float(os.getenv("LANE_EVAL_RETRY_BACKOFF_S", "8"))
CODE_EXEC_TIMEOUT_S = float(os.getenv("LANE_EVAL_CODE_EXEC_TIMEOUT_S", "5"))
MAX_AGE_DAYS_FOR_ALERT = 9  # kept in sync with observability/prometheus/rules/lane_eval_alerts.yml


# ---------------------------------------------------------------------------
# Config / lane discovery
# ---------------------------------------------------------------------------


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _resolve_probe_vk() -> str:
    """Read INFRA_PROBE_VK out of shared-infra/.env, stripped of quotes/CR.
    Never printed, never logged."""
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().strip("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "INFRA_PROBE_VK":
            return val.strip().strip('"').strip("'").strip()
    return ""


def vk_allowed_providers(config_db: Path, vk: str) -> set[str] | None:
    """Providers the probe VK's governance config allows, read read-only
    from Bifrost's config.db. None = unknown (db missing / VK not found) ->
    caller treats as "probe everything". Ported verbatim (same query, same
    read-only URI) from infractl/probes/lanes.py -- see that file's
    docstring for why a synthetic probe must never call a provider its VK
    is 403'd against."""
    if not vk or not config_db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{config_db}?mode=ro&immutable=1", uri=True, timeout=5.0)
        try:
            row = conn.execute("SELECT id FROM governance_virtual_keys WHERE value=? LIMIT 1", (vk,)).fetchone()
            if not row:
                return None
            return {
                p
                for (p,) in conn.execute(
                    "SELECT provider FROM governance_virtual_key_provider_configs WHERE virtual_key_id=?", (row[0],)
                )
            }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _operator_disabled_providers(operator_disabled: dict) -> set[str]:
    return set((operator_disabled.get("providers") or {}).keys())


def _operator_disabled_model_patterns(operator_disabled: dict) -> list[str]:
    return [p.lower() for p in (operator_disabled.get("model_patterns") or {}).keys()]


def _model_is_disabled(model: str, patterns: list[str]) -> bool:
    low = model.lower()
    return any(p in low for p in patterns)


def _provider_models(pcfg: dict) -> list[str]:
    models: list[str] = []
    for k in pcfg.get("keys") or []:
        if not isinstance(k, dict):
            continue
        for m in k.get("models") or []:
            if isinstance(m, str) and m not in models:
                models.append(m)
    return models


_PIN_RE = re.compile(r'["\']([a-z0-9][a-z0-9_.\-]*\/[A-Za-z0-9][A-Za-z0-9_.\-:\/]*)["\']')


def harvest_pinned_models(sources: list[Path], known_providers: set[str]) -> dict[str, list[str]]:
    """Grep every `PINNED_MODEL_SOURCES` file for `"provider/model"` string
    literals and keep only ones whose provider prefix is a real provider
    name in config.json (drops placeholders like the literal
    "provider/model" seen in llm_router.py, and drops embed-local pins).
    Order-preserving per provider so the first-pinned model is tried first.
    Never raises: a missing/unreadable consumer file just yields no pins
    from that file, it does not fail lane discovery."""
    pinned: dict[str, list[str]] = {}
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _PIN_RE.finditer(text):
            literal = match.group(1)
            provider, _, model = literal.partition("/")
            if provider not in known_providers or provider == "embed-local" or not model:
                continue
            bucket = pinned.setdefault(provider, [])
            if literal[len(provider) + 1 :] not in bucket:
                bucket.append(literal[len(provider) + 1 :])
    return pinned


def discover_lanes(vk: str, dry_run_ignore_vk: bool = False) -> tuple[list[dict], dict[str, str]]:
    cfg = _read_json(CONFIG_JSON, {})
    operator_disabled = _read_json(OPERATOR_DISABLED, {})
    disabled_providers = _operator_disabled_providers(operator_disabled)
    disabled_patterns = _operator_disabled_model_patterns(operator_disabled)

    providers_cfg = cfg.get("providers") or {}
    known_providers = set(providers_cfg.keys())
    pinned = harvest_pinned_models(PINNED_MODEL_SOURCES, known_providers)

    allowed = None if dry_run_ignore_vk else vk_allowed_providers(CONFIG_DB, vk)

    lanes: list[dict] = []
    skipped: dict[str, str] = {}
    for provider in sorted(providers_cfg.keys()):
        if provider == "embed-local":
            skipped[provider] = "embed_lane_not_chat"
            continue
        if provider in disabled_providers:
            skipped[provider] = "operator_disabled"
            continue
        pcfg = providers_cfg[provider]
        models = [m for m in _provider_models(pcfg) if not _model_is_disabled(m, disabled_patterns)]
        if not models:
            skipped[provider] = "no_models"
            continue
        if allowed is not None and provider not in allowed:
            skipped[provider] = "vk_not_allowed"
            continue
        chosen = [m for m in pinned.get(provider, []) if m in models]
        if not chosen:
            chosen = models[:1]
        chosen = chosen[:MAX_MODELS_PER_PROVIDER]
        for model in chosen:
            lanes.append({
                "provider": provider,
                "model": model,
                "is_pinned": model in pinned.get(provider, []),
                "timeout_s": TIMEOUT_LOCAL_S if provider.endswith("-local") else TIMEOUT_CLOUD_S,
            })

    if len(lanes) > MAX_LANES:
        overflow = lanes[MAX_LANES:]
        lanes = lanes[:MAX_LANES]
        for lane in overflow:
            skipped[f"{lane['provider']}/{lane['model']}"] = "lane_cap_reached"
    return lanes, skipped


# ---------------------------------------------------------------------------
# Deterministic long-context synthesis (stdlib only, no network / no tiktoken
# vocab download -- an offline-safe word-count approximation is fine here,
# the grading only needs "long enough to matter", not an exact token count)
# ---------------------------------------------------------------------------

_FILLER_WORDS = (
    "system gateway provider model latency throughput token router policy "
    "cluster metric probe alert dashboard cache queue worker container "
    "replica shard region cost budget quota retry backoff timeout schema "
    "payload endpoint client server request response header credential "
    "audit ledger config runtime deploy pipeline release branch commit "
    "index vector embedding context window prompt completion inference"
).split()


def _synthetic_paragraph(rng: random.Random, n_words: int) -> str:
    words = [rng.choice(_FILLER_WORDS) for _ in range(n_words)]
    sentence_len = 12
    sentences = []
    for i in range(0, len(words), sentence_len):
        chunk = words[i : i + sentence_len]
        if not chunk:
            continue
        chunk[0] = chunk[0].capitalize()
        sentences.append(" ".join(chunk) + ".")
    return " ".join(sentences)


def build_context(spec: dict) -> tuple[str, dict]:
    """Returns (context_text, extra) where extra carries fields a grader
    needs (e.g. the needle's exact insertion point isn't needed by the
    grader, only the answer regex already in the suite)."""
    kind = spec["kind"]
    target_tokens = int(spec.get("target_tokens", 1000))
    target_words = int(target_tokens / 1.3)
    rng = random.Random(spec.get("seed", "lane-eval"))

    if kind == "filler_with_needle":
        pre_words = int(target_words * 0.6)
        post_words = target_words - pre_words
        pre = _synthetic_paragraph(rng, pre_words)
        post = _synthetic_paragraph(rng, post_words)
        needle = spec["needle_sentence"]
        return f"{pre}\n\n{needle}\n\n{post}", {}

    if kind == "filler_with_facts":
        # Interleave three short "invoice line" facts through filler so a
        # naive first/last-paragraph-only reader still has to actually read.
        facts = [
            "Invoice number on file: INV-88214.",
            "Total amount due: $4,392.50 (net 30).",
            "Payment due date: 2026-11-03.",
        ]
        third = target_words // 3
        parts = []
        for fact in facts:
            parts.append(_synthetic_paragraph(rng, third))
            parts.append(fact)
        return "\n\n".join(parts), {}

    raise ValueError(f"unknown context kind: {kind}")


# ---------------------------------------------------------------------------
# Gateway call
# ---------------------------------------------------------------------------


class CallResult:
    def __init__(self, ok: bool, status: int, body: dict | None, latency_ms: float, error: str = "",
                 rate_limited: bool = False):
        self.ok = ok
        self.status = status
        self.body = body
        self.latency_ms = latency_ms
        self.error = error
        self.rate_limited = rate_limited


def call_chat_completion(vk: str, model: str, messages: list, max_tokens: int, timeout_s: float,
                          tools: list | None = None) -> CallResult:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": min(max_tokens, MAX_TOKENS_CAP),
        "temperature": 0.0,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    body_bytes = json.dumps(payload).encode("utf-8")
    url = f"{BASE_URL}/v1/chat/completions"

    for attempt in range(2):  # one retry on 429
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method="POST",
            headers={"Content-Type": "application/json", "x-bf-vk": vk},
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                latency_ms = (time.monotonic() - start) * 1000.0
                raw = resp.read()
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    return CallResult(False, resp.status, None, latency_ms, "invalid_json_response")
                return CallResult(True, resp.status, parsed, latency_ms)
        except urllib.error.HTTPError as exc:
            latency_ms = (time.monotonic() - start) * 1000.0
            if exc.code == 429 and attempt == 0:
                time.sleep(RETRY_BACKOFF_S)
                continue
            if exc.code == 429:
                return CallResult(False, 429, None, latency_ms, "rate_limited", rate_limited=True)
            try:
                err_body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                err_body = ""
            return CallResult(False, exc.code, None, latency_ms, f"http_{exc.code}: {err_body}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            latency_ms = (time.monotonic() - start) * 1000.0
            return CallResult(False, 0, None, latency_ms, f"network_error: {exc}")
    return CallResult(False, 0, None, 0.0, "unreachable")


def _extract_text(body: dict) -> str:
    try:
        return body["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _extract_tool_call(body: dict) -> dict | None:
    try:
        calls = body["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            return None
        fn = calls[0].get("function") or {}
        return {"name": fn.get("name"), "arguments_raw": fn.get("arguments")}
    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Graders -- every one is deterministic/programmatic, no LLM judge.
# Each returns (passed: bool, detail: str).
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json|python)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _find_json_span(text: str, want_array: bool) -> str | None:
    open_ch, close_ch = ("[", "]") if want_array else ("{", "}")
    start = text.find(open_ch)
    end = text.rfind(close_ch)
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + 1]


def grade_json_schema(text: str, task: dict) -> tuple[bool, str]:
    import jsonschema

    cleaned = _strip_code_fences(text)
    want_array = task["schema"].get("type") == "array"
    span = _find_json_span(cleaned, want_array) or cleaned
    try:
        obj = json.loads(span)
    except ValueError as exc:
        return False, f"invalid_json: {exc}"
    try:
        jsonschema.validate(obj, task["schema"])
    except jsonschema.ValidationError as exc:
        return False, f"schema_violation: {exc.message}"
    expected = task.get("expected")
    if expected is not None and obj != expected:
        return False, f"schema_ok_but_value_mismatch: got={obj!r} want={expected!r}"
    return True, "ok"


def grade_tool_call(tool_call: dict | None, task: dict) -> tuple[bool, str]:
    if not tool_call or not tool_call.get("name"):
        return False, "no_tool_call_emitted"
    if tool_call["name"] != task["expected_function"]:
        return False, f"wrong_function: got={tool_call['name']}"
    try:
        args = json.loads(tool_call.get("arguments_raw") or "{}")
    except ValueError:
        return False, "arguments_not_json"
    expected = task["expected_args"]
    mismatches = []
    for k, v in expected.items():
        got = args.get(k)
        if isinstance(v, (int, float)) and isinstance(got, (int, float)):
            if abs(float(got) - float(v)) > 1e-6:
                mismatches.append(f"{k}: got={got} want={v}")
        elif isinstance(v, str) and isinstance(got, str):
            if got.strip().lower() != v.strip().lower():
                mismatches.append(f"{k}: got={got!r} want={v!r}")
        elif got != v:
            mismatches.append(f"{k}: got={got!r} want={v!r}")
    if mismatches:
        return False, "arg_mismatch: " + "; ".join(mismatches)
    return True, "ok"


def grade_exact_answer(text: str, task: dict) -> tuple[bool, str]:
    if re.search(task["expected_regex"], text.strip()):
        return True, "ok"
    return False, f"no_match: regex={task['expected_regex']!r} text={text[:120]!r}"


def grade_extraction(text: str, task: dict) -> tuple[bool, str]:
    fields = task["expected_fields"]
    low = text.lower()
    matched = [k for k, v in fields.items() if str(v).lower() in low]
    need = task.get("min_fields_matched", len(fields))
    if len(matched) >= need:
        return True, f"matched={matched}"
    return False, f"matched_only={matched} need>={need}"


def grade_needle(text: str, task: dict) -> tuple[bool, str]:
    pattern = task["context_spec"]["needle_answer_regex"]
    if re.search(pattern, text):
        return True, "ok"
    return False, f"needle_not_found text={text[:120]!r}"


def grade_code(text: str, task: dict) -> tuple[bool, str]:
    cleaned = _strip_code_fences(text)
    if "def " not in cleaned:
        return False, "no_function_definition_found"
    script = cleaned + "\n\n" + task["test_code"]
    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = Path(tmpdir) / "candidate.py"
        script_path.write_text(script, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                timeout=CODE_EXEC_TIMEOUT_S,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            return False, "execution_timeout"
        if proc.returncode == 0:
            return True, "ok"
        return False, f"asserts_failed: {proc.stderr[-300:]}"


_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")


def grade_instruction_follow(text: str, task: dict) -> tuple[bool, str]:
    c = task["constraints"]
    stripped = text.strip()
    if "format_regex" in c:
        if re.fullmatch(c["format_regex"], stripped):
            return True, "ok"
        return False, f"format_mismatch: {stripped[:80]!r}"
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(stripped) if s.strip()]
    reasons = []
    if "min_sentences" in c and len(sentences) < c["min_sentences"]:
        reasons.append(f"too_few_sentences={len(sentences)}")
    if "max_sentences" in c and len(sentences) > c["max_sentences"]:
        reasons.append(f"too_many_sentences={len(sentences)}")
    for ch in c.get("forbid_chars", []):
        if ch in stripped:
            reasons.append(f"forbidden_char={ch!r}")
    low = stripped.lower()
    for w in c.get("forbid_words", []):
        if re.search(rf"\b{re.escape(w.lower())}\b", low):
            reasons.append(f"forbidden_word={w!r}")
    if reasons:
        return False, "; ".join(reasons)
    return True, "ok"


_REFUSAL_PHRASES = (
    "i cannot", "i can't", "i'm not able", "i am not able", "as an ai",
    "i'm sorry, but i", "i am sorry, but i", "i won't", "i will not",
)


def grade_summarize_keywords(text: str, task: dict) -> tuple[bool, str]:
    low = text.lower()
    if any(p in low for p in _REFUSAL_PHRASES):
        return False, "refused"
    missing = [kw for kw in task["required_keywords"] if kw.lower() not in low]
    if missing:
        return False, f"missing_keywords={missing}"
    return True, "ok"


GRADERS = {
    "json_schema": lambda call, task, ctx: grade_json_schema(_extract_text(call.body), task),
    "tool_call": lambda call, task, ctx: grade_tool_call(_extract_tool_call(call.body), task),
    "exact_answer": lambda call, task, ctx: grade_exact_answer(_extract_text(call.body), task),
    "extraction": lambda call, task, ctx: grade_extraction(_extract_text(call.body), task),
    "needle": lambda call, task, ctx: grade_needle(_extract_text(call.body), task),
    "code_fix": lambda call, task, ctx: grade_code(_extract_text(call.body), task),
    "instruction_follow": lambda call, task, ctx: grade_instruction_follow(_extract_text(call.body), task),
    "summarize_keywords": lambda call, task, ctx: grade_summarize_keywords(_extract_text(call.body), task),
}


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------


def build_prompt(task: dict, context_cache: dict) -> str:
    if "context_spec" in task:
        spec = task["context_spec"]
        cache_key = json.dumps(spec, sort_keys=True)
        if cache_key not in context_cache:
            context_cache[cache_key], _ = build_context(spec)
        context = context_cache[cache_key]
        return task["prompt_template"].format(context=context)
    return task["prompt"]


def run_task(vk: str, lane: dict, task: dict, context_cache: dict) -> dict:
    prompt = build_prompt(task, context_cache)
    messages = [{"role": "user", "content": prompt}]
    model_ref = f"{lane['provider']}/{lane['model']}"
    call = call_chat_completion(
        vk, model_ref, messages, task.get("max_tokens", 256), lane["timeout_s"], tools=task.get("tools")
    )
    record = {
        "task_id": task["id"],
        "category": task["category"],
        "latency_ms": round(call.latency_ms, 1),
    }
    if call.rate_limited:
        record.update(outcome="rate_limited", detail="429")
        return record
    if not call.ok:
        record.update(outcome="error", detail=call.error)
        return record
    try:
        grader = GRADERS[task["type"]]
        passed, detail = grader(call, task, context_cache)
    except Exception as exc:  # noqa: BLE001 -- a grader bug must not crash the run
        record.update(outcome="error", detail=f"grader_exception: {exc!r}")
        return record
    record.update(outcome="pass" if passed else "fail", detail=detail)
    return record


# ---------------------------------------------------------------------------
# Run + report
# ---------------------------------------------------------------------------


def run_suite(lanes: list[dict], suite: dict, vk: str) -> list[dict]:
    tasks = suite["tasks"]
    context_cache: dict = {}
    lane_results = []
    calls_made = 0
    for lane in lanes:
        task_records = []
        for task in tasks:
            if calls_made >= MAX_CALLS_PER_RUN:
                task_records.append({
                    "task_id": task["id"], "category": task["category"],
                    "outcome": "skipped", "detail": "run_cap_reached", "latency_ms": 0.0,
                })
                continue
            rec = run_task(vk, lane, task, context_cache)
            task_records.append(rec)
            calls_made += 1
            time.sleep(CALL_DELAY_S)
        lane_results.append(_summarize_lane(lane, task_records))
    return lane_results


def _summarize_lane(lane: dict, task_records: list[dict]) -> dict:
    by_category: dict[str, list[dict]] = {}
    for rec in task_records:
        by_category.setdefault(rec["category"], []).append(rec)

    category_pass_ratio = {}
    for cat, recs in by_category.items():
        graded = [r for r in recs if r["outcome"] in ("pass", "fail")]
        category_pass_ratio[cat] = (sum(1 for r in graded if r["outcome"] == "pass") / len(graded)) if graded else None

    graded_all = [r for r in task_records if r["outcome"] in ("pass", "fail")]
    overall_pass_ratio = None
    if graded_all:
        overall_pass_ratio = sum(1 for r in graded_all if r["outcome"] == "pass") / len(graded_all)
    latencies = [r["latency_ms"] for r in task_records if r["outcome"] in ("pass", "fail")]
    p50 = statistics.median(latencies) if latencies else None
    errors = sum(1 for r in task_records if r["outcome"] == "error")
    rate_limited = sum(1 for r in task_records if r["outcome"] == "rate_limited")

    return {
        "provider": lane["provider"],
        "model": lane["model"],
        "is_pinned": lane["is_pinned"],
        "overall_pass_ratio": overall_pass_ratio,
        "category_pass_ratio": category_pass_ratio,
        "p50_latency_ms": p50,
        "errors": errors,
        "rate_limited": rate_limited,
        "attempted": len(task_records),
        "tasks": task_records,
    }


def _best_lane_per_purpose(lane_summaries: list[dict]) -> dict[str, str | None]:
    best: dict[str, str | None] = {}
    for cat in PURPOSE_CATEGORIES:
        candidates = [
            (s["category_pass_ratio"].get(cat), s.get("p50_latency_ms") or 1e9, f"{s['provider']}/{s['model']}")
            for s in lane_summaries
            if s["category_pass_ratio"].get(cat) is not None
        ]
        if not candidates:
            best[cat] = None
            continue
        candidates.sort(key=lambda t: (-t[0], t[1]))
        best[cat] = candidates[0][2]
    return best


def render_report(latest: dict, out_path: Path) -> None:
    lines = [
        "# Lane quality report",
        "",
        f"GENERATED by `scripts/lane_eval.py` on {latest['generated_at']}. Do not hand-edit --",
        "re-run the harness (`python scripts/lane_eval.py`) to refresh. Suite: "
        f"`scripts/lane_eval_suite.json` v{latest['suite_version']}.",
        "",
        "## Best lane per purpose",
        "",
        "| Purpose | Best lane | Pass ratio |",
        "|---|---|---|",
    ]
    for cat in PURPOSE_CATEGORIES:
        lane_id = latest["best_lane_per_purpose"].get(cat)
        ratio = "-"
        if lane_id:
            for s in latest["lanes"]:
                if f"{s['provider']}/{s['model']}" == lane_id:
                    ratio = f"{s['category_pass_ratio'].get(cat, 0):.2f}"
                    break
        lines.append(f"| {cat} | {lane_id or '_no data_'} | {ratio} |")

    lines += [
        "",
        "## Per-lane detail",
        "",
        "| Lane | Overall | json | tools | reasoning | long_context | code | p50 ms "
        "| errors | rate-limited | pinned |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    def fmt(v):
        return f"{v:.2f}" if v is not None else "-"

    for s in latest["lanes"]:
        cats = s["category_pass_ratio"]
        p50 = f"{s['p50_latency_ms']:.0f}" if s["p50_latency_ms"] is not None else "-"
        lines.append(
            f"| {s['provider']}/{s['model']} | {fmt(s['overall_pass_ratio'])} | "
            f"{fmt(cats.get('json'))} | {fmt(cats.get('tools'))} | {fmt(cats.get('reasoning'))} | "
            f"{fmt(cats.get('long_context'))} | {fmt(cats.get('code'))} | {p50} | "
            f"{s['errors']} | {s['rate_limited']} | {'yes' if s['is_pinned'] else 'no'} |"
        )

    if latest.get("skipped_providers"):
        lines += ["", "## Skipped providers", "", "| Provider/lane | Reason |", "|---|---|"]
        for k, v in latest["skipped_providers"].items():
            lines.append(f"| {k} | {v} |")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="discovery only, no gateway calls")
    args = parser.parse_args()

    vk = _resolve_probe_vk()
    if not vk and not args.dry_run:
        print("ERROR: INFRA_PROBE_VK not found in .env", file=sys.stderr)
        return 1

    suite = _read_json(SUITE_PATH, None)
    if suite is None:
        print(f"ERROR: cannot read {SUITE_PATH}", file=sys.stderr)
        return 1

    lanes, skipped = discover_lanes(vk, dry_run_ignore_vk=False)
    lane_ids = [f"{lane['provider']}/{lane['model']}" for lane in lanes]
    print(f"Discovered {len(lanes)} lane(s): {lane_ids}")
    if skipped:
        print(f"Skipped: {skipped}")

    if args.dry_run:
        return 0

    started = time.time()
    lane_summaries = run_suite(lanes, suite, vk)
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    latest = {
        "generated_at": generated_at,
        "generated_at_unixtime": time.time(),
        "suite_version": suite.get("version", "unknown"),
        "duration_s": round(time.time() - started, 1),
        "lanes": lane_summaries,
        "best_lane_per_purpose": _best_lane_per_purpose(lane_summaries),
        "skipped_providers": skipped,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with RUNS_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(latest) + "\n")
    tmp = LATEST_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(latest, indent=2), encoding="utf-8")
    os.replace(tmp, LATEST_JSON)

    docs_dir = REPO_ROOT / "docs"
    render_report(latest, docs_dir / "LANE_QUALITY.md")

    print(f"Wrote {RUNS_JSONL}, {LATEST_JSON}, {docs_dir / 'LANE_QUALITY.md'}")
    print(f"Best lane per purpose: {latest['best_lane_per_purpose']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
