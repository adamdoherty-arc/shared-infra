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
PROBE_TIMEOUT_S = int(os.getenv("BIFROST_PROBE_TIMEOUT_S", "60"))
PROBE_TICK_S = int(os.getenv("BIFROST_PROBE_TICK_S", "60"))
PROBE_VK_ENV = os.getenv("BIFROST_PROBE_VK", "")  # optional override; else read config.db

# Lane list is DERIVED from config.json (2026-09-15). The previous hand-written
# PROBE_LANES tuple list went stale twice: it probed parked providers
# (moonshot, cerebras, mistral, gemini) and a model nvidia-nim no longer lists
# (z-ai/glm-5.2), so BifrostCriticalLaneDown fired for days on a lane nobody
# could call. Now every ACTIVE provider in config.json gets exactly one
# representative lane, re-read every tick so a park/unpark takes effect within
# PROBE_TICK_S with no rebuild. Providers the probe VK is not allowed to reach
# (openrouter is paid and only ada-prod may use it) are reported as skipped,
# never as down.
# config.json is the ONLY source of truth for "active": disabled-providers.json
# is a recipe archive that still carries old groq/openrouter blocks for
# providers that were later re-enabled, so it must not be consulted here.
CONFIG_JSON = Path(os.getenv("BIFROST_CONFIG_JSON", "/data/config.json"))
PROBE_LANES_OUT = STATE_DIR / "probe_lanes.json"
PROBE_CRITICAL_PROVIDERS = {
    p.strip()
    for p in os.getenv(
        "BIFROST_PROBE_CRITICAL_PROVIDERS", "vllm-local,embed-local,nvidia-nim,groq"
    ).split(",")
    if p.strip()
}
PROBE_PERIOD_CRITICAL_S = int(os.getenv("BIFROST_PROBE_PERIOD_CRITICAL_S", "600"))
PROBE_PERIOD_FALLBACK_S = int(os.getenv("BIFROST_PROBE_PERIOD_FALLBACK_S", "3600"))
# The local chat engine runs at 100 % GPU with MAX_SEQS=32 and ~113k
# requests/day queued in front of the probe; a 4-token completion can wait
# well past 15 s without the lane being dead. Real callers use 120 s.
PROBE_TIMEOUT_LOCAL_S = int(os.getenv("BIFROST_PROBE_TIMEOUT_LOCAL_S", "120"))
# Ordered preference of probe model per provider; the first one present in
# the provider's config.json model list wins, else the first listed model.
# Override/extend with BIFROST_PROBE_MODELS='{"provider": ["model", ...]}'.
_DEFAULT_PROBE_MODEL_PREFS: dict[str, list[str]] = {
    "vllm-local": ["qwen3.8-27b", "qwen3-chat", "local-chat"],
    "embed-local": ["Qwen/Qwen3-Embedding-0.6B"],
    "nvidia-nim": ["nvidia/nemotron-3.5-lightning-30b-a3b", "openai/gpt-oss-20b"],
    "groq": ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
    "freellmapi": ["gpt-oss-120b", "openai/gpt-oss-120b", "DeepSeek-V3.2"],
    "openrouter": ["openrouter/free", "nvidia/nemotron-3.5-lightning:free"],
    "hf-router": ["openai/gpt-oss-20b", "meta-llama/Llama-3.1-8B-Instruct"],
    "sealion": ["aisingapore/Gemma-SEA-LION-v4-27B-IT"],
    "aion": ["aion-labs/aion-3.0-mini"],
}
try:
    _PROBE_MODEL_PREFS = {
        **_DEFAULT_PROBE_MODEL_PREFS,
        **{k: list(v) for k, v in json.loads(os.getenv("BIFROST_PROBE_MODELS", "{}")).items()},
    }
except Exception as exc:  # malformed override must not kill the prober
    print(f"lane-prober: ignoring BIFROST_PROBE_MODELS ({exc})", flush=True)
    _PROBE_MODEL_PREFS = dict(_DEFAULT_PROBE_MODEL_PREFS)

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
list_models_probe_total = Counter(
    "bifrost_list_models_probe_total",
    "GET /v1/models fan-out rows per provider (one row per provider per catalog probe; "
    "custom providers without list_models answer unsupported_operation). Kept out of "
    "bifrost_requests_total so catalog probes never read as inference errors.",
    ["provider", "status"],
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
lane_probe_skipped = Gauge(
    "bifrost_lane_probe_skipped",
    "1 for each active provider the prober deliberately does not probe "
    "(reason label says why, e.g. the probe VK may not reach it).",
    ["provider", "reason"],
)
lane_probe_lanes = Gauge(
    "bifrost_lane_probe_lanes",
    "Number of lanes currently derived from config.json for active probing.",
)


def _looks_like_timestamp(val: str) -> bool:
    """True if `val` looks like a `logs.timestamp` value (e.g.
    '2026-07-18 04:32:05.671726134+00:00'), not a legacy bare-integer ROWID
    cursor from the pre-Fix-1000157 exporter. Deliberately loose — we only
    need to reject old cursor.txt content left over from before the
    ROWID -> timestamp migration (2026-07-18)."""
    return len(val) >= 10 and val[4] == "-" and val[7] == "-"


def _read_cursor() -> str:
    try:
        val = CURSOR_FILE.read_text().strip()
        if val and not _looks_like_timestamp(val):
            # Legacy ROWID cursor (e.g. "4554392") from before Fix-1000157.
            # A bare digit string text-compares as LESS than any real
            # 'YYYY-...' timestamp ('4' > '2' lexically is backwards: SQLite
            # would evaluate `timestamp > '4554392'` as FALSE for every row,
            # since '2026-...' < '4554392' lexically) -- i.e. reusing it
            # verbatim would silently re-break scraping the same way the
            # ROWID cursor did after VACUUM. Treat as "no cursor" so the
            # caller re-bootstraps cleanly.
            return ""
        return val
    except Exception:
        return ""


def _write_cursor(cursor_ts: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(cursor_ts)


def _bootstrap_cursor() -> str:
    """First-run (or post-migration) cursor: jump to the current
    MAX(timestamp) so we don't ingest months of historical rows. Prometheus
    rate()/increase() only needs deltas from "now" forward; historical
    aggregates can still be queried directly against logs.db with SQL when
    needed.
    """
    if not LOGS_DB.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{LOGS_DB}?mode=ro", uri=True, timeout=5.0)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(MAX(timestamp), '') FROM logs")
            return str(cur.fetchone()[0] or "")
        finally:
            conn.close()
    except Exception:
        return ""


def _scrape_logs(cursor_ts: str) -> str:
    """Pull new rows from logs.db.logs and emit counters. Returns max
    `timestamp` value seen (as a string; the column is ISO-ish text).

    Fix-1000157 (2026-07-18): this used to cursor on SQLite's implicit
    ROWID. The `id` column on `logs` is a UUID varchar (no INTEGER PRIMARY
    KEY), and per SQLite docs VACUUM "may reset the ROWID values" for
    exactly that shape of table. bifrost-logs-pruner runs DELETE + VACUUM
    every night (03:00 UTC) whenever it deletes any stale rows -- which is
    every night under real traffic -- so the ROWID cursor got silently
    renumbered out from under the exporter: cursor > new MAX(ROWID) forever,
    "no new rows" logged on every scrape indefinitely (confirmed stuck for
    9+ hours straight during this investigation; likely stuck since the
    2026-07-13 mega-prune). `timestamp` is a column VALUE written once per
    row and is untouched by VACUUM's physical page reshuffling, so cursoring
    on it is immune to this failure mode by construction.
    """
    if not LOGS_DB.exists():
        exporter_scrape_errors_total.labels(table="logs").inc()
        return cursor_ts
    max_ts = cursor_ts
    try:
        conn = sqlite3.connect(f"file:{LOGS_DB}?mode=ro", uri=True, timeout=5.0)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT timestamp, object_type, provider, model, status, latency,
                       prompt_tokens, completion_tokens, cost
                FROM logs
                WHERE timestamp > ?
                ORDER BY timestamp ASC
                LIMIT 5000
                """,
                (cursor_ts,),
            )
            for row in cur.fetchall():
                row_ts, obj_type, provider, model, status, latency, p_tok, c_tok, cost = row
                provider = provider or "unknown"
                model = model or ""
                status = status or "unknown"
                request_type = obj_type or "unknown"
                if request_type == "list_models":
                    list_models_probe_total.labels(provider, status).inc()
                    if isinstance(row_ts, str) and row_ts > max_ts:
                        max_ts = row_ts
                    continue
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
                if isinstance(row_ts, str) and row_ts > max_ts:
                    max_ts = row_ts
        finally:
            conn.close()
    except Exception:
        exporter_scrape_errors_total.labels(table="logs").inc()
    return max_ts


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
    if not cursor:
        cursor = _bootstrap_cursor()
        _write_cursor(cursor)
        print(
            f"scrape-loop bootstrapped at MAX(timestamp)={cursor!r} "
            f"(historical rows skipped; only forward deltas exported)",
            flush=True,
        )
    else:
        print(f"scrape-loop resuming from cursor={cursor!r}", flush=True)
    iteration = 0
    while True:
        iteration += 1
        try:
            new_cursor = _scrape_logs(cursor)
            if new_cursor != cursor:
                _write_cursor(new_cursor)
                prev_cursor = cursor
                cursor = new_cursor
                print(
                    f"scrape #{iteration}: cursor advanced from {prev_cursor!r} to {cursor!r}",
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
            # Only an infra-owned key may probe. Auto-pick used to be
            # ORDER BY name LIMIT 1, which silently chose a-finance-prod (the
            # erpnext consumer's key) and charged ~600 probe rows/day to it
            # (measured 2026-09-15). Consumer keys (*-prod) are never eligible.
            row = conn.execute(
                "SELECT value FROM governance_virtual_keys "
                "WHERE is_active=1 AND value LIKE 'sk-bf-%' "
                "AND name NOT LIKE '%-prod' "
                "ORDER BY CASE name WHEN 'claude-code-local' THEN 0 ELSE 1 END, name "
                "LIMIT 1"
            ).fetchone()
            if not row or not row[0]:
                print("[exporter] no infra-owned probe VK (set BIFROST_PROBE_VK); lane probes disabled", flush=True)
                return ""
            return row[0]
        finally:
            conn.close()
    except Exception:
        return ""


def _probe_vk_providers(vk: str) -> set[str] | None:
    """Providers the probe VK's governance config allows. None = unknown
    (config.db unreadable or VK not found) -> probe everything."""
    if not CONFIG_DB.exists():
        return None
    try:
        conn = sqlite3.connect(
            f"file:{CONFIG_DB}?mode=ro&immutable=1", uri=True, timeout=5.0
        )
        try:
            row = conn.execute(
                "SELECT id FROM governance_virtual_keys WHERE value=? LIMIT 1", (vk,)
            ).fetchone()
            if not row:
                return None
            return {
                p
                for (p,) in conn.execute(
                    "SELECT provider FROM governance_virtual_key_provider_configs "
                    "WHERE virtual_key_id=?",
                    (row[0],),
                )
            }
        finally:
            conn.close()
    except Exception:
        return None


def _load_probe_lanes(vk_providers: set[str] | None) -> tuple[list[tuple], dict[str, str]]:
    """Derive one probe lane per active provider from config.json.

    Returns (lanes, skipped) where each lane is
    (provider/model, kind, tier, period_s, timeout_s) and skipped maps a
    provider name to the reason it is not probed. Never raises: an unreadable
    config.json yields ([], {}) and the caller keeps the previous list.
    """
    try:
        cfg = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"lane-prober: cannot read {CONFIG_JSON}: {exc}", flush=True)
        return [], {}
    lanes: list[tuple] = []
    skipped: dict[str, str] = {}
    for provider, pcfg in sorted((cfg.get("providers") or {}).items()):
        keys = [k for k in (pcfg.get("keys") or []) if isinstance(k, dict)]
        models: list[str] = []
        for k in keys:
            for m in k.get("models") or []:
                if isinstance(m, str) and m not in models:
                    models.append(m)
        if not models:
            skipped[provider] = "no_models"
            continue
        if vk_providers is not None and provider not in vk_providers:
            skipped[provider] = "vk_not_allowed"
            continue
        prefs = _PROBE_MODEL_PREFS.get(provider, [])
        model = next((m for m in prefs if m in models), models[0])
        kind = "embed" if "embed" in provider.lower() else "chat"
        tier = "critical" if provider in PROBE_CRITICAL_PROVIDERS else "fallback"
        period = PROBE_PERIOD_CRITICAL_S if tier == "critical" else PROBE_PERIOD_FALLBACK_S
        timeout = PROBE_TIMEOUT_LOCAL_S if provider.endswith("-local") else PROBE_TIMEOUT_S
        lanes.append((f"{provider}/{model}", kind, tier, period, timeout))
    return lanes, skipped


def _write_probe_lanes(lanes: list[tuple], skipped: dict[str, str]) -> None:
    """Persist the derived list so operators (and infractl) can see exactly
    what is being probed without reading Prometheus."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROBE_LANES_OUT.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "source": str(CONFIG_JSON),
                    "lanes": [
                        {"model": m, "kind": k, "tier": t, "period_s": p, "timeout_s": to}
                        for (m, k, t, p, to) in lanes
                    ],
                    "skipped": skipped,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, PROBE_LANES_OUT)
    except Exception as exc:
        print(f"lane-prober: could not write {PROBE_LANES_OUT}: {exc}", flush=True)


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

    One daemon thread woken every PROBE_TICK_S; the lane list is re-derived
    from config.json on every wake (cheap: one small JSON read) so parking a
    provider drops its lane -- and its stale metric series -- within one tick.
    Each lane is probed only when its period has elapsed (free-tier-friendly).
    A failed probe sets up=0 so a silently-dead lane becomes visible to
    Prometheus within its period.
    """
    if not PROBE_ENABLED:
        lane_probe_enabled.set(0)
        print("lane-prober disabled (BIFROST_LANE_PROBE_ENABLED=0)", flush=True)
        return
    last_probe: dict[str, float] = {}
    exported_up: set[tuple[str, str, str, str]] = set()
    exported_lat: set[tuple[str, str]] = set()
    exported_skip: set[tuple[str, str]] = set()
    lanes: list[tuple] = []
    last_sig: str | None = None
    vk_cached = ""
    vk_providers: set[str] | None = None
    while True:
        vk = _resolve_probe_vk()
        lane_probe_enabled.set(1 if vk else 0)
        if not vk:
            print("lane-prober: no active VK resolved yet; retrying", flush=True)
            time.sleep(PROBE_TICK_S)
            continue
        if vk != vk_cached or vk_providers is None:
            vk_providers = _probe_vk_providers(vk)
            vk_cached = vk
        new_lanes, skipped = _load_probe_lanes(vk_providers)
        if new_lanes:
            sig = json.dumps([new_lanes, sorted(skipped.items())])
            if sig != last_sig:
                lanes = new_lanes
                last_sig = sig
                _write_probe_lanes(lanes, skipped)
                lane_probe_lanes.set(len(lanes))
                want_up = {
                    (m.partition("/")[0], m.partition("/")[2], t, k)
                    for (m, k, t, _p, _to) in lanes
                }
                for labels in exported_up - want_up:
                    try:
                        lane_up.remove(*labels)
                    except KeyError:
                        pass
                    exported_up.discard(labels)
                    print(f"lane-prober: dropped lane {labels[0]}/{labels[1]}", flush=True)
                want_lat = {(a, b) for (a, b, _t, _k) in want_up}
                for labels in exported_lat - want_lat:
                    try:
                        lane_probe_latency_ms.remove(*labels)
                    except KeyError:
                        pass
                    exported_lat.discard(labels)
                active_models = {l[0] for l in lanes}
                for m in list(last_probe):
                    if m not in active_models:
                        last_probe.pop(m, None)
                want_skip = set(skipped.items())
                for labels in exported_skip - want_skip:
                    try:
                        lane_probe_skipped.remove(*labels)
                    except KeyError:
                        pass
                for prov, reason in want_skip:
                    lane_probe_skipped.labels(prov, reason).set(1)
                exported_skip = want_skip
                print(
                    "lane-prober: lanes="
                    + ", ".join(f"{m}[{t}]" for (m, _k, t, _p, _to) in lanes)
                    + (f" skipped={skipped}" if skipped else ""),
                    flush=True,
                )
        now = time.time()
        for model, kind, tier, period, timeout in lanes:
            if now - last_probe.get(model, 0.0) < period:
                continue
            provider, _, bare = model.partition("/")
            ok, ms = _probe_lane(model, kind, vk, timeout)
            lane_up.labels(provider, bare, tier, kind).set(1 if ok else 0)
            exported_up.add((provider, bare, tier, kind))
            lane_probe_latency_ms.labels(provider, bare).set(round(ms, 1))
            exported_lat.add((provider, bare))
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
