"""Prometheus exporter for Bifrost — reads logs.db and serves /metrics.

Bifrost's vendor image v1.5.0 doesn't ship the upstream Prometheus plugin,
but it writes every inference (provider, model, latency, status, tokens,
cost) to a SQLite file at /app/data/logs.db. This sidecar tails that
file and exposes the same numbers a built-in plugin would.

Scrape config (ada-prometheus + legion-prometheus already in compose):

    - job_name: bifrost
      static_configs:
        - targets: ['bifrost-metrics:9100']

Metrics shipped:
- bifrost_requests_total{provider, model, status, request_type} (counter)
- bifrost_request_latency_ms_bucket{provider, model, request_type} (histogram)
- bifrost_prompt_tokens_total{provider, model} (counter)
- bifrost_completion_tokens_total{provider, model} (counter)
- bifrost_cost_usd_total{provider, model} (counter)
- bifrost_logs_db_bytes (gauge) — size of logs.db on disk
- bifrost_active_providers (gauge) — read from config.db
- bifrost_active_virtual_keys (gauge) — read from config.db

Counters are reset every process restart (Prometheus tolerates this via
`rate()` / `increase()`), but the read cursor is persisted in
/state/cursor.txt so we don't double-count or miss rows across restarts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    REGISTRY,
    generate_latest,
)

LOGS_DB = Path(os.getenv("BIFROST_LOGS_DB", "/data/logs.db"))
CONFIG_DB = Path(os.getenv("BIFROST_CONFIG_DB", "/data/config.db"))
STATE_DIR = Path(os.getenv("EXPORTER_STATE_DIR", "/state"))
CURSOR_FILE = STATE_DIR / "cursor.txt"
SCRAPE_INTERVAL_S = int(os.getenv("EXPORTER_INTERVAL_S", "15"))
PORT = int(os.getenv("EXPORTER_PORT", "9100"))

# ---- Active lane prober ---------------------------------------------------
# The metrics above are PASSIVE — they only reflect requests that organically
# flow through logs.db. A lane that dies but is rarely called (a fallback, a
# parked provider, an expired key) goes silently down until something happens
# to try it. That exact gap let nvidia-nim wedge and the Gemini key expire
# unnoticed for days (2026-06). This prober ACTIVELY pings each lane on a
# cadence and emits bifrost_lane_up so death is visible + alertable.
#
# Probes go through the gateway itself (provider/model routing == what callers
# see), authenticated with an active virtual key read from config.db (no
# secret duplicated into env). Per-lane period throttles free-tier quota:
# critical primaries every 10m (generous quotas), fallbacks hourly.
PROBE_ENABLED = os.getenv("BIFROST_LANE_PROBE_ENABLED", "1") == "1"
PROBE_BASE = os.getenv("BIFROST_PROBE_BASE", "http://shared-bifrost:8080").rstrip("/")
PROBE_TIMEOUT_S = int(os.getenv("BIFROST_PROBE_TIMEOUT_S", "15"))
PROBE_TICK_S = int(os.getenv("BIFROST_PROBE_TICK_S", "60"))
PROBE_VK_ENV = os.getenv("BIFROST_PROBE_VK", "")  # optional override; else read config.db

# (provider/model, kind, tier, period_seconds). tier drives alerting:
# "critical" lanes page; "fallback" lanes are gauge-only awareness. z.ai is
# intentionally omitted — it hangs through the gateway (known integration
# defect) and would stall the probe tick; it's in no active project chain.
PROBE_LANES = [
    # --- critical: local + each project's cloud primary (10m) ---
    ("vllm-local/qwen3-chat", "chat", "critical", 300),
    ("embed-local/Qwen/Qwen3-Embedding-0.6B", "embed", "critical", 300),
    ("nvidia-nim/moonshotai/kimi-k2.6", "chat", "critical", 300, 40),  # ADA primary (NIM cold-start can hit 25s)
    ("nvidia-nim/z-ai/glm-5.1", "chat", "critical", 300, 40),          # Legion reasoning (NIM cold-start can hit 25s)
    ("groq/openai/gpt-oss-120b", "chat", "critical", 300),            # Zero primary (fast)
    ("moonshot/kimi-k2.6", "chat", "critical", 300, 40),              # compat shim -> NIM (cold-start can hit 25s)
    # --- fallback: deep lanes + parked/dead, awareness only (1h) ---
    ("nvidia-nim/qwen/qwen3.5-122b-a10b", "chat", "fallback", 3600, 45),  # 122B: 12-17s, needs longer probe timeout
    ("cerebras/gpt-oss-120b", "chat", "fallback", 3600),
    ("mistral/mistral-large-latest", "chat", "fallback", 3600),
    ("hf-router/moonshotai/Kimi-K2.6", "chat", "fallback", 3600),
    ("openrouter/openrouter/free", "chat", "fallback", 3600),
    ("gemini/gemini-3.5-flash", "chat", "fallback", 3600),          # dead key — stays visibly down until rotated
]

# Histogram buckets in milliseconds — chosen to cover the realistic Bifrost
# range: ~30 ms for local embed, ~300 ms for vllm-local chat, ~1-3 s for
# Kimi K2.6, occasional 10-20 s for Moonshot slow paths.
LATENCY_BUCKETS_MS = (
    20, 50, 100, 200, 500,
    1_000, 2_000, 5_000, 10_000, 20_000, 30_000, 60_000,
)

requests_total = Counter(
    "bifrost_requests_total",
    "Total inference requests routed through Bifrost.",
    ["provider", "model", "status", "request_type"],
)
latency_hist = Histogram(
    "bifrost_request_latency_ms",
    "Bifrost request latency in milliseconds.",
    ["provider", "model", "request_type"],
    buckets=LATENCY_BUCKETS_MS,
)
prompt_tokens_total = Counter(
    "bifrost_prompt_tokens_total",
    "Total prompt tokens consumed.",
    ["provider", "model"],
)
completion_tokens_total = Counter(
    "bifrost_completion_tokens_total",
    "Total completion tokens generated.",
    ["provider", "model"],
)
cost_total = Counter(
    "bifrost_cost_usd_total",
    "Total cumulative inference cost in USD.",
    ["provider", "model"],
)
logs_db_bytes = Gauge(
    "bifrost_logs_db_bytes",
    "Size of bifrost logs.db on disk in bytes.",
)
active_providers = Gauge(
    "bifrost_active_providers",
    "Number of active providers registered in config.db.",
)
active_virtual_keys = Gauge(
    "bifrost_active_virtual_keys",
    "Number of active virtual keys.",
)
exporter_last_scrape_seconds = Gauge(
    "bifrost_exporter_last_scrape_unixtime",
    "Unix time of the last successful scrape pass.",
)
exporter_scrape_errors_total = Counter(
    "bifrost_exporter_scrape_errors_total",
    "Total scrape errors hit by the exporter.",
    ["table"],
)

# ---- Active lane-probe metrics --------------------------------------------
lane_up = Gauge(
    "bifrost_lane_up",
    "1 if the last active probe of this provider/model lane succeeded, else 0.",
    ["provider", "model", "tier", "kind"],
)
lane_probe_latency_ms = Gauge(
    "bifrost_lane_probe_latency_ms",
    "Latency of the last active lane probe in milliseconds.",
    ["provider", "model"],
)
lane_probe_last_seconds = Gauge(
    "bifrost_lane_probe_last_unixtime",
    "Unix time of the last active probe pass (any lane).",
)
lane_probe_enabled = Gauge(
    "bifrost_lane_probe_enabled",
    "1 if the active lane prober is running (probe VK resolved), else 0.",
)


def _read_cursor() -> int:
    try:
        return int(CURSOR_FILE.read_text().strip())
    except Exception:
        return -1


def _write_cursor(cursor_id: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(str(cursor_id))


def _bootstrap_cursor() -> int:
    """First-run cursor: jump to the current MAX(ROWID) so we don't ingest
    months of historical rows. Prometheus rate()/increase() only needs
    deltas from "now" forward; historical aggregates can still be
    queried directly against logs.db with SQL when needed.
    """
    if not LOGS_DB.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{LOGS_DB}?mode=ro", uri=True, timeout=5.0)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(MAX(ROWID), 0) FROM logs")
            return int(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


def _scrape_logs(cursor_rowid: int) -> int:
    """Pull new rows from logs.db.logs and emit counters. Returns max ROWID seen.

    The `id` column on logs is a UUID varchar so we can't use it as a
    monotonic cursor — use SQLite's implicit ROWID instead. ROWID is
    auto-incrementing INTEGER, always present unless the table is
    WITHOUT ROWID (the logs table is not).
    """
    if not LOGS_DB.exists():
        exporter_scrape_errors_total.labels(table="logs").inc()
        return cursor_rowid
    max_rowid = cursor_rowid
    try:
        conn = sqlite3.connect(f"file:{LOGS_DB}?mode=ro", uri=True, timeout=5.0)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ROWID, object_type, provider, model, status, latency,
                       prompt_tokens, completion_tokens, cost
                FROM logs
                WHERE ROWID > ?
                ORDER BY ROWID ASC
                LIMIT 5000
                """,
                (cursor_rowid,),
            )
            for row in cur.fetchall():
                row_rowid, obj_type, provider, model, status, latency, p_tok, c_tok, cost = row
                provider = provider or "unknown"
                model = model or ""
                status = status or "unknown"
                request_type = obj_type or "unknown"
                requests_total.labels(provider, model, status, request_type).inc()
                if latency is not None:
                    try:
                        latency_hist.labels(provider, model, request_type).observe(float(latency))
                    except (TypeError, ValueError):
                        pass
                if p_tok:
                    prompt_tokens_total.labels(provider, model).inc(p_tok)
                if c_tok:
                    completion_tokens_total.labels(provider, model).inc(c_tok)
                if cost:
                    try:
                        cost_total.labels(provider, model).inc(float(cost))
                    except (TypeError, ValueError):
                        pass
                if isinstance(row_rowid, int) and row_rowid > max_rowid:
                    max_rowid = row_rowid
        finally:
            conn.close()
    except Exception:
        exporter_scrape_errors_total.labels(table="logs").inc()
    return max_rowid


def _scrape_config() -> None:
    """Count active providers + virtual keys from config.db."""
    if not CONFIG_DB.exists():
        exporter_scrape_errors_total.labels(table="config").inc()
        return
    try:
        conn = sqlite3.connect(f"file:{CONFIG_DB}?mode=ro&immutable=1", uri=True, timeout=5.0)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM config_providers")
            active_providers.set(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM governance_virtual_keys")
            active_virtual_keys.set(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception:
        exporter_scrape_errors_total.labels(table="config").inc()


def _scrape_loop() -> None:
    cursor = _read_cursor()
    if cursor < 0:
        cursor = _bootstrap_cursor()
        _write_cursor(cursor)
        print(
            f"scrape-loop bootstrapped at MAX(ROWID)={cursor} "
            f"(historical rows skipped; only forward deltas exported)",
            flush=True,
        )
    else:
        print(f"scrape-loop resuming from cursor={cursor}", flush=True)
    iteration = 0
    while True:
        iteration += 1
        try:
            new_cursor = _scrape_logs(cursor)
            if new_cursor != cursor:
                _write_cursor(new_cursor)
                advanced = new_cursor - cursor
                cursor = new_cursor
                print(
                    f"scrape #{iteration}: cursor advanced by {advanced} to {cursor}",
                    flush=True,
                )
            else:
                if iteration <= 3 or iteration % 60 == 0:
                    print(f"scrape #{iteration}: no new rows (cursor={cursor})", flush=True)
            _scrape_config()
            if LOGS_DB.exists():
                try:
                    logs_db_bytes.set(LOGS_DB.stat().st_size)
                except OSError:
                    pass
            exporter_last_scrape_seconds.set(time.time())
        except Exception as e:
            print(f"scrape #{iteration} ERROR: {e!r}", flush=True)
            exporter_scrape_errors_total.labels(table="loop").inc()
        time.sleep(SCRAPE_INTERVAL_S)


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            output = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)
        elif self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"bifrost-metrics-exporter ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default access log noise; Prometheus polls every 15 s.
        return


def _resolve_probe_vk() -> str:
    """Resolve a virtual-key bearer for probing: explicit env override, else
    the first active VK's value from config.db. Returns "" if none found."""
    if PROBE_VK_ENV:
        return PROBE_VK_ENV
    if not CONFIG_DB.exists():
        return ""
    try:
        conn = sqlite3.connect(
            f"file:{CONFIG_DB}?mode=ro&immutable=1", uri=True, timeout=5.0
        )
        try:
            row = conn.execute(
                "SELECT value FROM governance_virtual_keys "
                "WHERE is_active=1 AND value LIKE 'sk-bf-%' ORDER BY name LIMIT 1"
            ).fetchone()
            return row[0] if row and row[0] else ""
        finally:
            conn.close()
    except Exception:
        return ""


def _probe_lane(model: str, kind: str, vk: str, timeout: int) -> tuple[bool, float]:
    """Send one tiny request for `model` through the gateway. Returns (ok, latency_ms)."""
    if kind == "embed":
        url = f"{PROBE_BASE}/v1/embeddings"
        payload = {"model": model, "input": "ping"}
    else:
        url = f"{PROBE_BASE}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 4,
            "temperature": 1.0,
        }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {vk}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        ms = (time.time() - t0) * 1000.0
        ok = bool(data.get("data") if kind == "embed" else data.get("choices"))
        return ok, ms
    except Exception:
        return False, (time.time() - t0) * 1000.0


def _probe_loop() -> None:
    """Probe each lane on its own period; emit bifrost_lane_up + latency.

    One daemon thread woken every PROBE_TICK_S; each lane is probed only when
    its period has elapsed (free-tier-friendly). A failed probe sets up=0 so a
    silently-dead lane becomes visible to Prometheus within its period.
    """
    if not PROBE_ENABLED:
        lane_probe_enabled.set(0)
        print("lane-prober disabled (BIFROST_LANE_PROBE_ENABLED=0)", flush=True)
        return
    last_probe: dict[str, float] = {}
    while True:
        vk = _resolve_probe_vk()
        lane_probe_enabled.set(1 if vk else 0)
        if not vk:
            print("lane-prober: no active VK resolved yet; retrying", flush=True)
            time.sleep(PROBE_TICK_S)
            continue
        now = time.time()
        for lane in PROBE_LANES:
            model, kind, tier, period = lane[0], lane[1], lane[2], lane[3]
            timeout = lane[4] if len(lane) > 4 else PROBE_TIMEOUT_S
            if now - last_probe.get(model, 0.0) < period:
                continue
            provider, _, bare = model.partition("/")
            ok, ms = _probe_lane(model, kind, vk, timeout)
            lane_up.labels(provider, bare, tier, kind).set(1 if ok else 0)
            lane_probe_latency_ms.labels(provider, bare).set(round(ms, 1))
            last_probe[model] = now
            if not ok:
                print(f"lane-prober: DOWN {model} ({tier}) after {ms:.0f}ms", flush=True)
        lane_probe_last_seconds.set(time.time())
        time.sleep(PROBE_TICK_S)


def main() -> None:
    threading.Thread(target=_scrape_loop, name="scrape-loop", daemon=True).start()
    threading.Thread(target=_probe_loop, name="lane-prober", daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), MetricsHandler)
    print(
        f"bifrost-metrics-exporter listening on :{PORT} "
        f"(logs_db={LOGS_DB}, config_db={CONFIG_DB}, interval={SCRAPE_INTERVAL_S}s, "
        f"lane_probe={'on' if PROBE_ENABLED else 'off'} base={PROBE_BASE})",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
