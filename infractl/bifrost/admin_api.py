"""Thin HTTP client for shared-bifrost's own admin surface + the metrics
exporter's `/metrics`. NOT per-consumer-VK completion probes — infractl only
has INFRA_PROBE_VK (claude-code-local), not any project's own VK, so lane
health for OTHER consumers is read from bifrost-metrics' `bifrost_lane_up`
gauge (populated by exporter.py's `_load_probe_lanes()`-derived prober),
never re-probed here with a foreign VK."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import httpx


def health(probe_base: str, timeout_s: float = 5.0) -> tuple[bool, str]:
    try:
        resp = httpx.get(f"{probe_base}/health", timeout=timeout_s)
        return resp.status_code == 200, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)


def list_models(probe_base: str, vk: str | None = None, timeout_s: float = 5.0) -> tuple[bool, int, str]:
    """Returns (ok, model_count, detail). Any response under 500 means the
    gateway is up; a 200 with a model count additionally proves the VK path.

    Callers pass INFRA_PROBE_VK whenever it is configured. History: on
    2026-09-15 (Bifrost v1.5.0) an AUTHENTICATED `/v1/models` hung past 30s,
    so every caller went unauthenticated. Re-measured 2026-09-25 on v2.0.0:
    the authenticated call answers in 0.15s, and the UNAUTHENTICATED call is
    the harmful one -- Bifrost still fans the listing out to every provider
    and logs one "list models for provider X: virtual key is required" error
    per provider per call (9 providers x every probe tick = ~2,600 junk
    error rows/day in the log store). Unauthenticated is kept only as the
    fallback when no VK is configured."""
    headers = {"Authorization": f"Bearer {vk}"} if vk else {}
    try:
        resp = httpx.get(f"{probe_base}/v1/models", headers=headers, timeout=timeout_s)
        if resp.status_code >= 500:
            return False, 0, f"HTTP {resp.status_code}"
        try:
            data = resp.json()
            count = len(data.get("data", [])) if isinstance(data, dict) else 0
        except ValueError:
            count = 0
        return True, count, f"HTTP {resp.status_code}, {count} models"
    except httpx.HTTPError as exc:
        return False, 0, str(exc)


def synthetic_completion(probe_base: str, vk: str, model: str = "vllm-local/qwen3-chat",
                          timeout_s: float = 60.0) -> tuple[bool, str]:
    """Reuses bifrost/auth_autoheal.py's `synthetic_completion()` logic
    verbatim: a 4xx counts as RESPONDED (the gateway is processing requests,
    which is the only thing this probe asks); only a timeout, connection
    error, or 5xx indicate a hang."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{probe_base}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {vk}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            r.read()
            return True, f"HTTP {r.status} in {time.time() - t0:.1f}s"
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        if 500 <= e.code < 600:
            return False, f"HTTP {e.code} in {dt:.1f}s"
        return True, f"HTTP {e.code} in {dt:.1f}s (responsive; not a hang)"
    except Exception as e:  # noqa: BLE001 — timeout, conn refused, reset
        return False, f"{type(e).__name__} after {time.time() - t0:.1f}s"


def synthetic_embedding(probe_base: str, vk: str, model: str, timeout_s: float = 30.0) -> tuple[bool, str]:
    """Embedding-lane counterpart of synthetic_completion(). The embed lane
    used to be probed with a chat request, which the embed server rejects;
    a 4xx counted as "responsive", so the probe proved nothing (2026-09-25).
    Here only a 200 with a non-empty vector counts as up."""
    body = json.dumps({"model": model, "input": "ok"}).encode()
    req = urllib.request.Request(
        f"{probe_base}/v1/embeddings",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {vk}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read() or b"{}")
        vec = ((data.get("data") or [{}])[0]).get("embedding") or []
        if not vec:
            return False, f"HTTP {r.status} but empty embedding in {time.time() - t0:.1f}s"
        return True, f"HTTP {r.status} dim={len(vec)} in {time.time() - t0:.1f}s"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} in {time.time() - t0:.1f}s"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__} after {time.time() - t0:.1f}s"


def fetch_metrics_text(metrics_url: str, timeout_s: float = 10.0) -> str:
    resp = httpx.get(metrics_url, timeout=timeout_s)
    resp.raise_for_status()
    return resp.text


def parse_lane_up_gauges(metrics_text: str) -> list[dict]:
    """Parses `bifrost_lane_up{provider="...",model="...",tier="...",kind="..."} <0|1>`
    lines out of the exporter's Prometheus text exposition. Pure text parse —
    no prometheus_client dependency needed for this one gauge family."""
    out = []
    for line in metrics_text.splitlines():
        if not line.startswith("bifrost_lane_up{"):
            continue
        try:
            labels_part, value_part = line[len("bifrost_lane_up{"):].rsplit("}", 1)
            value = float(value_part.strip())
            labels = {}
            for pair in labels_part.split(","):
                if "=" not in pair:
                    continue
                k, v = pair.split("=", 1)
                labels[k.strip()] = v.strip().strip('"')
            labels["up"] = value == 1.0
            out.append(labels)
        except (ValueError, IndexError):
            continue
    return out
