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

import os
import sqlite3
import threading
import time
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
        conn = sqlite3.connect(f"file:{CONFIG_DB}?mode=ro", uri=True, timeout=5.0)
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


def main() -> None:
    threading.Thread(target=_scrape_loop, name="scrape-loop", daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), MetricsHandler)
    print(
        f"bifrost-metrics-exporter listening on :{PORT} "
        f"(logs_db={LOGS_DB}, config_db={CONFIG_DB}, interval={SCRAPE_INTERVAL_S}s)",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
