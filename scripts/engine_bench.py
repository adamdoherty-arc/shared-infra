#!/usr/bin/env python3
"""Benchmark a vLLM-compatible chat endpoint with ADA's real prompt mix.

Usage:
  python scripts/engine_bench.py --base-url http://127.0.0.1:4445/v1 \
      --model vllm-local/qwen3-chat --api-key sk-bf-... \
      --concurrency 1 8 --n 6 --out reports/engine-bench-baseline-2026-09-21.json

Pulls the top ADA call sites by volume from the native Postgres `llm_call_log`
table (last 24h), builds one representative prompt per site from
`prompt_templates` (system_prompt/output_instructions/expected_schema — this
schema has no stored user_prompt_template column, verified 2026-09-21), and
replays them at each requested concurrency level. Reports success %,
p50/p95 latency, tok/s, one tool-call pass/fail probe, one guided-JSON
(response_format=json_schema) pass/fail probe, and a prefix-cache hit probe
(same long system prompt sent twice back-to-back, second call's latency vs
first).

This gate would pass trivially if it fell back to a hardcoded prompt list
on any DB error — it deliberately does NOT: `_load_prompt_mix` raises on a
DB failure after its own bounded retry loop rather than silently
substituting synthetic prompts, because a "benchmark" that quietly stopped
using real traffic shape would misreport engine readiness exactly when the
DB is unhealthy (which, per this program's own Part B finding, is often).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

try:
    import asyncpg  # type: ignore
except ImportError:
    asyncpg = None


DB_DSN_DEFAULT = "postgresql://postgres:postgres123@127.0.0.1:5432/adam"

TOP_CALL_SITES_SQL = """
SELECT call_site, prompt_slug, count(*) c
FROM llm_call_log
WHERE created_at > now() - interval '24 hours'
  AND provider NOT IN ('embed-local')
  AND call_site NOT LIKE '%_embed_text%'
GROUP BY 1, 2
ORDER BY c DESC
LIMIT 8
"""

PROMPT_TEMPLATE_SQL = """
SELECT slug, system_prompt, output_instructions, expected_schema
FROM prompt_templates
WHERE slug = ANY($1::text[]) AND is_active = true
"""


@dataclass
class PromptCase:
    call_site: str
    prompt_slug: str | None
    system_prompt: str
    user_prompt: str


@dataclass
class CallResult:
    ok: bool
    latency_s: float
    completion_tokens: int
    error: str | None = None


async def _load_prompt_mix(dsn: str, retries: int = 6, delay_s: float = 3.0) -> list[PromptCase]:
    """Load the top real call-site prompts. Raises after `retries` on DB failure
    rather than substituting synthetic data (see module docstring)."""
    if asyncpg is None:
        raise RuntimeError(
            "asyncpg not installed and no fallback is permitted here — "
            "install it (pip install asyncpg) rather than mocking the prompt mix."
        )
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        conn = None
        try:
            conn = await asyncpg.connect(dsn, timeout=10)
            rows = await conn.fetch(TOP_CALL_SITES_SQL)
            slugs = [r["prompt_slug"] for r in rows if r["prompt_slug"]]
            templates: dict[str, asyncpg.Record] = {}
            if slugs:
                trows = await conn.fetch(PROMPT_TEMPLATE_SQL, slugs)
                templates = {t["slug"]: t for t in trows}
            cases: list[PromptCase] = []
            for r in rows:
                slug = r["prompt_slug"]
                t = templates.get(slug) if slug else None
                if t is not None:
                    system_prompt = t["system_prompt"] or "You are a helpful assistant."
                    tail = (t["output_instructions"] or "") + (
                        f"\nSchema: {t['expected_schema']}" if t["expected_schema"] else ""
                    )
                    user_prompt = (
                        "Apply the above to this real ADA call site's typical input: "
                        f"call_site={r['call_site']}. {tail}".strip()
                    )
                else:
                    system_prompt = "You are a helpful assistant."
                    user_prompt = (
                        f"Summarize the current state of the ADA call site "
                        f"'{r['call_site']}' in two sentences."
                    )
                cases.append(
                    PromptCase(
                        call_site=r["call_site"],
                        prompt_slug=slug,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                    )
                )
            if not cases:
                raise RuntimeError("llm_call_log returned zero rows in the last 24h — cannot build a real prompt mix")
            return cases
        except Exception as exc:  # noqa: BLE001 - deliberately broad, retried below
            last_exc = exc
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            if attempt < retries:
                await asyncio.sleep(delay_s)
    raise RuntimeError(f"could not load real prompt mix from Postgres after {retries} attempts: {last_exc}")


def _post_chat(base_url: str, api_key: str | None, model: str, messages: list[dict],
               extra: dict | None = None, timeout: float = 120.0,
               max_tokens: int = 256) -> tuple[bool, float, int, str | None, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    if extra:
        payload.update(extra)
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-bf-vk"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        latency = time.monotonic() - t0
        usage = body.get("usage", {}) or {}
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_ok = choice.get("message") is not None or choice.get("delta") is not None
        content = message.get("content") or ""
        return finish_ok, latency, completion_tokens, None, content
    except urllib.error.HTTPError as e:
        latency = time.monotonic() - t0
        try:
            err_body = e.read().decode("utf-8")[:300]
        except Exception:
            err_body = str(e)
        return False, latency, 0, f"HTTP {e.code}: {err_body}", ""
    except Exception as e:  # noqa: BLE001
        latency = time.monotonic() - t0
        return False, latency, 0, str(e), ""


def fetch_num_requests_running(base_url: str) -> float | None:
    """Reads vLLM's native `/metrics` (Prometheus text) for
    `vllm:num_requests_running`, summed across label sets. `base_url` is the
    OpenAI-compatible `/v1` URL; metrics live one level up at the server
    root. Returns None (never a fabricated 0) when the endpoint is
    unreachable or the metric is absent, so a caller can tell "not running
    yet" apart from "couldn't measure"."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    url = root + "/metrics"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            text = resp.read().decode("utf-8")
    except Exception:
        return None
    total = 0.0
    found = False
    for line in text.splitlines():
        if line.startswith("vllm:num_requests_running"):
            try:
                total += float(line.rsplit(" ", 1)[-1])
                found = True
            except ValueError:
                continue
    return total if found else None


def _pctile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def run_concurrency_level(cases: list[PromptCase], base_url: str, api_key: str | None,
                           model: str, concurrency: int, n: int, max_tokens: int = 256) -> dict:
    import concurrent.futures as cf

    jobs = []
    for i in range(n):
        case = cases[i % len(cases)]
        messages = [
            {"role": "system", "content": case.system_prompt},
            {"role": "user", "content": case.user_prompt},
        ]
        jobs.append((case, messages))

    running_before = fetch_num_requests_running(base_url)
    results: list[CallResult] = []
    t_start = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [
            ex.submit(_post_chat, base_url, api_key, model, messages, None, 120.0, max_tokens)
            for _case, messages in jobs
        ]
        for fut in cf.as_completed(futures):
            ok, latency, ctoks, err, _content = fut.result()
            results.append(CallResult(ok=ok, latency_s=latency, completion_tokens=ctoks, error=err))
    wall_s = time.monotonic() - t_start
    running_during = fetch_num_requests_running(base_url)

    n_ok = sum(1 for r in results if r.ok)
    latencies = [r.latency_s for r in results]
    total_ctoks = sum(r.completion_tokens for r in results)
    per_request_tok_s = [
        round(r.completion_tokens / r.latency_s, 2) for r in results if r.ok and r.latency_s > 0
    ]
    return {
        "concurrency": concurrency,
        "n": n,
        "max_tokens": max_tokens,
        "success_pct": round(100.0 * n_ok / len(results), 1) if results else 0.0,
        "p50_s": round(_pctile(latencies, 0.50), 3),
        "p95_s": round(_pctile(latencies, 0.95), 3),
        "wall_s": round(wall_s, 3),
        "aggregate_tok_s": round(total_ctoks / wall_s, 2) if wall_s > 0 else 0.0,
        "per_request_tok_s_median": round(statistics.median(per_request_tok_s), 2) if per_request_tok_s else 0.0,
        "per_request_tok_s_p95": round(_pctile(per_request_tok_s, 0.95), 2) if per_request_tok_s else 0.0,
        "per_request_tok_s_samples": per_request_tok_s,
        "vllm_num_requests_running_before": running_before,
        "vllm_num_requests_running_during_sample": running_during,
        "errors": [r.error for r in results if r.error][:5],
    }


def run_tool_call_probe(base_url: str, api_key: str | None, model: str) -> dict:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_symbol_quote",
                "description": "Get the latest quote for a stock symbol",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                },
            },
        }
    ]
    messages = [{"role": "user", "content": "What is the current quote for AAPL? Use the tool."}]
    ok, latency, _ctoks, err, _content = _post_chat(
        base_url, api_key, model, messages, extra={"tools": tools, "tool_choice": "auto"}
    )
    return {"pass": ok, "latency_s": round(latency, 3), "error": err}


def run_guided_json_probe(base_url: str, api_key: str | None, model: str) -> dict:
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "verdict",
            "schema": {
                "type": "object",
                "properties": {"verdict": {"type": "string"}, "confidence": {"type": "number"}},
                "required": ["verdict", "confidence"],
            },
        },
    }
    messages = [{"role": "user", "content": "Return a verdict object for AAPL earnings beat, JSON only."}]
    ok, latency, _ctoks, err, _content = _post_chat(
        base_url, api_key, model, messages, extra={"response_format": schema}
    )
    return {"pass": ok, "latency_s": round(latency, 3), "error": err}


def run_prefix_cache_probe(base_url: str, api_key: str | None, model: str) -> dict:
    long_prefix = ("You are ADA's trading analyst. " * 400)[:20000]
    messages = [
        {"role": "system", "content": long_prefix},
        {"role": "user", "content": "Say OK."},
    ]
    ok1, lat1, _c1, err1, _content1 = _post_chat(base_url, api_key, model, messages)
    ok2, lat2, _c2, err2, _content2 = _post_chat(base_url, api_key, model, messages)
    hit_likely = ok1 and ok2 and lat2 < lat1 * 0.9
    return {
        "first_call_s": round(lat1, 3),
        "second_call_s": round(lat2, 3),
        "prefix_cache_hit_likely": hit_likely,
        "errors": [e for e in (err1, err2) if e],
    }


def run_thinking_disabled_probe(base_url: str, api_key: str | None, model: str) -> dict:
    """`enable_thinking:false` is set on the canary via
    `--default-chat-template-kwargs`; this proves it is actually honoured
    by the served model rather than assumed from the launch flag. A
    reasoning-tuned Qwen3 model that ignores the kwarg emits a `<think>...
    </think>` block before its answer — this probe fails if that block
    appears anywhere in the response."""
    messages = [
        {"role": "user", "content": "A stock rallied 8% on no news. Give one sentence explaining the likely cause."}
    ]
    ok, latency, ctoks, err, content = _post_chat(base_url, api_key, model, messages, max_tokens=300)
    has_think_tag = "<think" in content.lower() or "</think>" in content.lower()
    return {
        "pass": ok and not has_think_tag,
        "latency_s": round(latency, 3),
        "completion_tokens": ctoks,
        "think_tag_found": has_think_tag,
        "error": err,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default=os.environ.get("BIFROST_VK", ""))
    ap.add_argument("--concurrency", nargs="+", type=int, default=[1, 8])
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--db-dsn", default=os.environ.get("ADA_DB_DSN", DB_DSN_DEFAULT))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cases = await _load_prompt_mix(args.db_dsn)

    result: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": args.base_url,
        "model": args.model,
        "max_tokens": args.max_tokens,
        "prompt_mix": [
            {"call_site": c.call_site, "prompt_slug": c.prompt_slug} for c in cases
        ],
        "concurrency_results": [],
    }

    for c in args.concurrency:
        print(f"[engine_bench] concurrency={c} n={args.n} max_tokens={args.max_tokens} ...", file=sys.stderr)
        level = run_concurrency_level(cases, args.base_url, args.api_key, args.model, c, args.n, args.max_tokens)
        result["concurrency_results"].append(level)
        print(json.dumps(level), file=sys.stderr)

    print("[engine_bench] tool-call probe ...", file=sys.stderr)
    result["tool_call_probe"] = run_tool_call_probe(args.base_url, args.api_key, args.model)
    print("[engine_bench] guided-json probe ...", file=sys.stderr)
    result["guided_json_probe"] = run_guided_json_probe(args.base_url, args.api_key, args.model)
    print("[engine_bench] thinking-disabled probe ...", file=sys.stderr)
    result["thinking_disabled_probe"] = run_thinking_disabled_probe(args.base_url, args.api_key, args.model)
    print("[engine_bench] prefix-cache probe ...", file=sys.stderr)
    result["prefix_cache_probe"] = run_prefix_cache_probe(args.base_url, args.api_key, args.model)

    out_str = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_str)
        print(f"[engine_bench] wrote {args.out}", file=sys.stderr)
    else:
        print(out_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
