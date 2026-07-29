#!/usr/bin/env python3
"""Bifrost auth-autoheal - auto-park any provider with sustained auth failures.

WHY THIS EXISTS (2026-07-08): a single provider whose key goes bad (expired,
revoked, wrong project) drags the whole gateway down in log noise. Bifrost runs
a model-discovery loop every minute, so a dead native/list-models provider
floods logs.db + stdout at ~4 warn/min forever ("failed to list models for
provider gemini: ... invalid authentication credentials"), which is exactly
what happened with the Gemini lane on 2026-07-08 before it was parked by hand.
This sidecar does that park automatically, mirroring the manual procedure and
the established vllm-autoheal / vllm-wedge-monitor sidecar pattern in this repo.

DETECTION (load-immune, false-positive-resistant):
  Read `docker logs shared-bifrost --since WINDOW_S` over the Docker socket and
  count auth-failure log lines PER PROVIDER. A provider is parked only when it
  crosses MIN_HITS within WINDOW_S (sustained) AND it is still an active
  provider in config.json AND it is not on the protected list. A healthy
  provider produces ZERO matching lines, so the threshold can never false-park
  it; a one-off transient (1-2 lines) stays well under MIN_HITS.

  We anchor on provider-scoped auth errors only:
    - "failed to list models for provider <p>: <reason>"   (names <p> directly)
    - "failed to list models with key <uuid>: <reason>"    (uuid -> provider via
                                                             config_keys.key_id)
  and require <reason> to contain real auth language (invalid credentials /
  api key not valid / unauthorized / 401 / 403 ...). Generic transport errors
  ("failed to execute HTTP request to provider API") are NOT auth and never
  count. Gateway-level 403s on /v1/chat/completions are NOT counted either --
  those are governance/VK rejections, not provider auth failures.

PARK ACTION (the exact manual 5-step procedure, atomic-ish):
  1. pre-flight: provider is active + not protected  (else skip, no-op)
  2. snapshot config.json + disabled-providers.json + config.db  (timestamped)
  3. docker stop shared-bifrost           (release the config.db handle)
  4. move providers[<p>] : config.json -> disabled-providers.json, stamping a
     _comment "auto-parked <p> <ISO> by bifrost-autoheal (sustained auth ...)"
  5. deregister <p> from config.db: config_keys, config_providers.name,
     governance_virtual_key_provider_config_keys,
     governance_virtual_key_provider_configs. (governance_model_pricing is
     LEFT ALONE by default -- CLAUDE.md + bifrost/README.md document it as
     Bifrost's built-in pricing datasheet / reference data, not an active
     registration, and the manual Gemini park left its 65 rows in place. Set
     AUTOHEAL_PURGE_PRICING=true to also purge those pricing rows.)
  6. run sync_vk_allowlists.py against the same config.db (BIFROST_CONFIG_DB)
  7. docker start shared-bifrost
  8. alert: append to autoheal.log + optional Discord webhook
  A finally-block guarantees shared-bifrost is started again even if a middle
  step throws, so a failed park never leaves the gateway down.

Stdlib only (urllib + http.client over the Docker unix socket + sqlite3) so the
stock python:3.12-slim image needs no pip install, matching vllm_wedge_monitor.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from http.client import HTTPConnection

# ── config ─────────────────────────────────────────────────────────────────
INTERVAL_S = int(os.environ.get("AUTOHEAL_INTERVAL_S", "60"))
WINDOW_S = int(os.environ.get("AUTOHEAL_WINDOW_S", "300"))     # lookback window
MIN_HITS = int(os.environ.get("AUTOHEAL_MIN_HITS", "6"))       # sustained floor
DRY_RUN = os.environ.get("AUTOHEAL_DRY_RUN", "false").lower() in ("1", "true", "yes")
# Repo policy (CLAUDE.md + bifrost/README.md) says LEAVE governance_model_pricing
# alone -- it is Bifrost's built-in pricing datasheet / reference data, and the
# manual Gemini park left its rows in place. Off by default; opt in to purge.
PURGE_PRICING = os.environ.get("AUTOHEAL_PURGE_PRICING", "false").lower() in ("1", "true", "yes")
PROTECTED = {
    p.strip()
    for p in os.environ.get("AUTOHEAL_PROTECTED", "vllm-local,embed-local").split(",")
    if p.strip()
}
BIFROST_CONTAINER = os.environ.get("AUTOHEAL_BIFROST_CONTAINER", "shared-bifrost")
STOP_TIMEOUT_S = int(os.environ.get("AUTOHEAL_STOP_TIMEOUT_S", "60"))
DISCORD_WEBHOOK = os.environ.get("AUTOHEAL_DISCORD_WEBHOOK", "").strip()

# ── hang detection (synthetic completion probe) ─────────────────────────────
# The auth pass above only reads LOG LINES. On 2026-07-25 the gateway exhausted
# its upstream connection pool and held every completion for ~4.9 hours (127 in
# one 10-minute window, durations past 17,700,000 ms and climbing) while vLLM
# itself sat idle at running=1/waiting=0 and answered a DIRECT call in 9.1s.
# It logged no auth failures, so this sidecar saw a "clean poll" throughout, and
# /health/liveliness kept returning 200 -- so every health check in the mesh
# (all three projects route through this gateway) reported green while the thing
# was functionally dead. A restart fixed it instantly.
#
# Liveliness cannot see that class of failure: the only thing that proves a
# gateway can complete a request is completing a request. So we send a real
# 1-token completion through the local vLLM lane on each pass.
PROBE_ENABLED = os.environ.get("AUTOHEAL_PROBE", "true").lower() in ("1", "true", "yes")
PROBE_BASE = os.environ.get("AUTOHEAL_PROBE_BASE", "http://shared-bifrost:8080").rstrip("/")
# MUST be provider/model. A bare model name is rejected by the router with
# 400 "provider is required in model field", which this probe would (correctly)
# classify as "responsive" -- so a bare name yields a probe that passes forever
# without ever exercising the completion path it exists to test.
PROBE_MODEL = os.environ.get("AUTOHEAL_PROBE_MODEL", "vllm-local/qwen3-chat")
PROBE_PROVIDER = os.environ.get("AUTOHEAL_PROBE_PROVIDER", "vllm-local")
# This threshold is NOT a latency SLO. It only has to separate "slow" from
# "never", because the failure it exists for runs to HOURS.
#
# 45s was tried first, on the theory that healthy local completions run 10-24s.
# They don't, reliably: an hour of probe history right after a restart recorded
# 0.3s, 13.3s, 19.9s, 21.2s, 32.5s and 36.6s successes interleaved with 45s
# timeouts -- so 45s sat inside the normal spread and the probe kept reaching its
# restart condition on a gateway that was merely slow. Only the cooldown stopped
# it restart-looping. 120s is well clear of the observed spread and still catches
# a real hang in ~6 minutes (120s x 3 consecutive failures).
PROBE_TIMEOUT_S = int(os.environ.get("AUTOHEAL_PROBE_TIMEOUT_S", "120"))
PROBE_FAIL_STREAK = int(os.environ.get("AUTOHEAL_PROBE_FAIL_STREAK", "3"))
PROBE_COOLDOWN_S = int(os.environ.get("AUTOHEAL_PROBE_COOLDOWN_S", "1800"))
# "alert" (default) or "restart".
#
# Detection is unambiguously safe and is the whole point: nothing in the mesh
# could previously see a gateway that answers /health/liveliness with 200 while
# completing nothing. The RESTART is deliberately not the default, because the
# 2026-07-29 investigation found the recurring stall is a CAPACITY problem, not
# a leak: vllm-chat runs max_num_seqs=1 and Bifrost's vllm-local lane is
# concurrency=1, so a single long generation (a 651-second completion was logged
# from ada-backend while vLLM healthily produced 54 tok/s) blocks the lane for
# every project. Restarting there aborts real in-flight work and buys ~15 minutes
# before the next long request does it again -- which is exactly the "restart
# fixed it" pattern the last two incidents recorded.
#
# Set AUTOHEAL_PROBE_ACTION=restart once the lane concurrency is settled.
PROBE_ACTION = os.environ.get("AUTOHEAL_PROBE_ACTION", "alert").strip().lower()
# Checked BEFORE any restart, by COMPLETING a request straight against the
# upstream. If the upstream cannot complete either, the lane is saturated or the
# upstream is down -- restarting the gateway fixes neither and would abort real
# in-flight work.
PROBE_UPSTREAM_BASE = os.environ.get("AUTOHEAL_PROBE_UPSTREAM_BASE", "http://vllm-chat:8000").rstrip("/")
PROBE_UPSTREAM_MODEL = os.environ.get("AUTOHEAL_PROBE_UPSTREAM_MODEL", "qwen3-chat")
PROBE_UPSTREAM_TIMEOUT_S = int(os.environ.get("AUTOHEAL_PROBE_UPSTREAM_TIMEOUT_S", "60"))
PROBE_VK_NAME = os.environ.get("AUTOHEAL_PROBE_VK", "").strip()  # "" = auto-pick an active VK

_probe_streak = 0
_probe_last_restart = 0.0

BIFROST_DIR = os.environ.get("AUTOHEAL_BIFROST_DIR", "/work/bifrost")
CONFIG_JSON = os.environ.get("AUTOHEAL_CONFIG_JSON", os.path.join(BIFROST_DIR, "config.json"))
DISABLED_JSON = os.environ.get("AUTOHEAL_DISABLED_JSON", os.path.join(BIFROST_DIR, "disabled-providers.json"))
CONFIG_DB = os.environ.get("AUTOHEAL_CONFIG_DB", os.path.join(BIFROST_DIR, "config.db"))
ALERT_LOG = os.environ.get("AUTOHEAL_ALERT_LOG", os.path.join(BIFROST_DIR, "autoheal.log"))
SYNC_SCRIPT = os.environ.get("AUTOHEAL_SYNC_SCRIPT", os.path.join(BIFROST_DIR, "sync_vk_allowlists.py"))

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

# ── detection patterns ──────────────────────────────────────────────────────
PROVIDER_LIST_RE = re.compile(r"failed to list models for provider ([\w.\-/]+)", re.I)
KEYED_RE = re.compile(r"failed to list models with key ([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})", re.I)
# strong, provider-auth-specific tokens (case-insensitive substring match)
AUTH_TOKENS = (
    "invalid authentication credential",
    "invalid api key",
    "api key not valid",
    "incorrect api key",
    "invalid x-api-key",
    "unauthorized",
    "unauthenticated",
    "no auth credentials",
    "authentication failed",
    "authentication error",
    "permission denied",
    "invalid token",
    "expired",
    "forbidden",
    "401",
    "403",
)


def log(msg: str) -> None:
    print(f"[bifrost-autoheal] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── docker unix-socket client (stdlib, unversioned API) ─────────────────────
class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, sock_path: str, timeout: int = 90) -> None:
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def _docker(method: str, path: str, read_body: bool = True) -> tuple[int, bytes, str]:
    conn = _UnixHTTPConnection(DOCKER_SOCK)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        ctype = resp.getheader("Content-Type", "") or ""
        body = resp.read() if read_body else b""
        return resp.status, body, ctype
    finally:
        conn.close()


def _demux(raw: bytes, ctype: str) -> str:
    """Demultiplex a non-TTY Docker log stream (8-byte frame headers).
    Falls back to raw decode for a TTY / already-flat stream."""
    if not raw:
        return ""
    framed = "multiplexed" in ctype.lower() or raw[0] in (0, 1, 2)
    if not framed:
        return raw.decode("utf-8", "replace")
    out = bytearray()
    i, n = 0, len(raw)
    while i + 8 <= n:
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        i += 8
        out += raw[i:i + size]
        i += size
    if i < n:  # trailing partial frame (shouldn't happen for a bounded window)
        out += raw[i:]
    return out.decode("utf-8", "replace")


def docker_logs(container: str, since_s: int) -> str:
    since_ts = int(time.time()) - since_s
    path = f"/containers/{container}/logs?stdout=1&stderr=1&timestamps=0&since={since_ts}"
    status, body, ctype = _docker("GET", path)
    if status != 200:
        raise RuntimeError(f"docker logs {container} -> HTTP {status}: {body[:200]!r}")
    return _demux(body, ctype)


def docker_stop(container: str) -> int:
    status, _, _ = _docker("POST", f"/containers/{container}/stop?t={STOP_TIMEOUT_S}")
    return status  # 204 stopped, 304 already stopped


def docker_start(container: str) -> int:
    status, _, _ = _docker("POST", f"/containers/{container}/start")
    return status  # 204 started, 304 already running


# ── config helpers ──────────────────────────────────────────────────────────
def load_active_providers() -> dict:
    with open(CONFIG_JSON, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("providers", {})


def key_provider_map() -> dict:
    """key_id (uuid) -> provider, read live from config.db (read-only)."""
    out: dict[str, str] = {}
    try:
        db = sqlite3.connect(f"file:{CONFIG_DB}?mode=ro", uri=True, timeout=10)
        try:
            for kid, prov in db.execute("SELECT key_id, provider FROM config_keys"):
                if kid:
                    out[str(kid)] = prov
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        log(f"warn: could not read key->provider map ({e!r}); keyed lines will be skipped")
    return out


# ── detection ────────────────────────────────────────────────────────────────
def count_auth_failures(logs_text: str, kmap: dict) -> dict:
    """Return {provider: hits} of provider-scoped auth failures in the window."""
    counts: dict[str, int] = {}
    for line in logs_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # bifrost logs are JSON (LOG_STYLE=json); tolerate a stray plain line.
        msg = line
        if line[0] == "{":
            try:
                msg = json.loads(line).get("message", "") or ""
            except Exception:  # noqa: BLE001
                msg = line
        low = msg.lower()
        if not any(tok in low for tok in AUTH_TOKENS):
            continue
        prov: str | None = None
        m = PROVIDER_LIST_RE.search(msg)
        if m:
            prov = m.group(1)
        else:
            k = KEYED_RE.search(msg)
            if k:
                prov = kmap.get(k.group(1))
        if prov:
            counts[prov] = counts.get(prov, 0) + 1
    return counts


def probe_vk() -> str | None:
    """A virtual key that can reach PROBE_PROVIDER, read live from config.db.

    Read-only, and reuses the mount this sidecar already has -- so the probe adds
    no new secret plumbing and cannot drift from the gateway's real key set.
    """
    try:
        db = sqlite3.connect(f"file:{CONFIG_DB}?mode=ro", uri=True, timeout=10)
    except Exception as e:  # noqa: BLE001
        log(f"probe: cannot open config.db read-only ({e!r})")
        return None
    try:
        if PROBE_VK_NAME:
            row = db.execute(
                "SELECT value FROM governance_virtual_keys WHERE name=? AND is_active=1",
                (PROBE_VK_NAME,),
            ).fetchone()
            if not row:
                log(f"probe: VK '{PROBE_VK_NAME}' not found or inactive; falling back to auto-pick")
            else:
                return row[0]
        row = db.execute(
            "SELECT k.value FROM governance_virtual_keys k "
            "JOIN governance_virtual_key_provider_configs c ON c.virtual_key_id = k.id "
            "WHERE k.is_active=1 AND c.provider=? ORDER BY k.name LIMIT 1",
            (PROBE_PROVIDER,),
        ).fetchone()
        return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        log(f"probe: VK lookup failed ({e!r})")
        return None
    finally:
        db.close()


def upstream_healthy() -> tuple[bool, str]:
    """Can the UPSTREAM complete, bypassing the gateway entirely?

    This is the discriminator, and a /v1/models check is not good enough for it.
    vllm-chat runs with max_num_seqs=1 ("Maximum concurrency for 32,768 tokens
    per request: 1.26x"), and Bifrost's vllm-local lane is concurrency=1 to
    match, so ONE long generation legitimately occupies the whole lane -- a 651
    SECOND completion was observed in the gateway log while vLLM was healthily
    producing 54 tok/s. During that window /v1/models answers instantly and the
    gateway cannot complete, which looks exactly like a hang and is not one.
    Restarting there would abort a real request that was working.

    What separated the genuine 2026-07-25 hang was that a DIRECT call to vLLM
    returned in 9.1s while the gateway held for hours. So probe the upstream the
    same way we probe the gateway: if the upstream completes quickly and the
    gateway cannot, the gateway is at fault. If the upstream is busy too, the
    lane is saturated and a restart fixes nothing.
    """
    body = json.dumps({
        "model": PROBE_UPSTREAM_MODEL,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{PROBE_UPSTREAM_BASE}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=PROBE_UPSTREAM_TIMEOUT_S) as r:
            r.read()
            return True, f"upstream completed in {time.time() - t0:.1f}s"
    except Exception as e:  # noqa: BLE001
        return False, f"upstream {type(e).__name__} after {time.time() - t0:.1f}s"


def synthetic_completion(vk: str) -> tuple[bool, str]:
    """Return (gateway_responded, detail).

    A 4xx counts as RESPONDED: an auth or governance rejection means the gateway
    is processing requests, which is the only thing this probe is asking. Only a
    timeout, a connection error or a 5xx indicate the pool-exhaustion hang.
    """
    body = json.dumps({
        "model": PROBE_MODEL,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{PROBE_BASE}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {vk}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as r:
            r.read()
            return True, f"HTTP {r.status} in {time.time() - t0:.1f}s"
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        if 500 <= e.code < 600:
            return False, f"HTTP {e.code} in {dt:.1f}s"
        return True, f"HTTP {e.code} in {dt:.1f}s (responsive; not a hang)"
    except Exception as e:  # noqa: BLE001  (timeout, conn refused, reset)
        return False, f"{type(e).__name__} after {time.time() - t0:.1f}s"


def restart_gateway(detail: str) -> None:
    log(f"HANG: {BIFROST_CONTAINER} failed {PROBE_FAIL_STREAK} consecutive completion probes -> restarting")
    try:
        log(f"docker stop {BIFROST_CONTAINER} -> HTTP {docker_stop(BIFROST_CONTAINER)}")
    finally:
        log(f"docker start {BIFROST_CONTAINER} -> HTTP {docker_start(BIFROST_CONTAINER)}")
    _alert(
        f"restarted {BIFROST_CONTAINER}: {PROBE_FAIL_STREAK} consecutive synthetic completions failed "
        f"({detail}) while the upstream completed fine - the gateway-side hang that "
        f"/health/liveliness cannot see."
    )


def check_hang(dry: bool) -> bool:
    """One completion-probe pass. Returns True if a restart happened (or would in dry mode)."""
    global _probe_streak, _probe_last_restart
    vk = probe_vk()
    if not vk:
        log(f"probe: no active virtual key for provider '{PROBE_PROVIDER}'; skipping hang check")
        return False
    ok, detail = synthetic_completion(vk)
    if ok:
        if _probe_streak:
            log(f"probe: recovered after {_probe_streak} failure(s) - {detail}")
        _probe_streak = 0
        log(f"probe: gateway completes ({detail})")
        return False

    _probe_streak += 1
    log(f"probe: FAILED {_probe_streak}/{PROBE_FAIL_STREAK} - {detail}")
    if _probe_streak < PROBE_FAIL_STREAK:
        return False
    up_ok, up_detail = upstream_healthy()
    if not up_ok:
        log(f"probe: {up_detail} - the lane is saturated or the upstream is down, "
            f"not a gateway hang; not restarting")
        _alert(
            f"{BIFROST_CONTAINER} cannot complete ({detail}) and neither can the upstream "
            f"({up_detail}). Saturation or upstream outage - gateway left alone."
        )
        _probe_streak = 0
        return False
    log(f"probe: {up_detail} while the gateway cannot - the gateway is at fault")
    since = time.time() - _probe_last_restart
    if _probe_last_restart and since < PROBE_COOLDOWN_S:
        log(f"probe: within {PROBE_COOLDOWN_S}s cooldown ({since:.0f}s since last restart); not restarting again")
        return False
    if PROBE_ACTION != "restart":
        log(f"probe: ACTION=alert - {BIFROST_CONTAINER} cannot complete but the upstream can; "
            f"alerting without restarting (set AUTOHEAL_PROBE_ACTION=restart to auto-heal)")
        _alert(
            f"{BIFROST_CONTAINER} failed {PROBE_FAIL_STREAK} consecutive completion probes "
            f"({detail}) while the upstream completed fine ({up_detail}). Gateway-side stall - "
            f"NOT restarted (ACTION=alert). Most likely the vllm-local lane (concurrency=1) is "
            f"held by a long generation."
        )
        _probe_last_restart = time.time()  # reuse the cooldown to rate-limit alerts
        _probe_streak = 0
        return True
    if dry:
        log(f"DRY-RUN: WOULD restart {BIFROST_CONTAINER} ({detail}) - no action taken")
        _probe_streak = 0
        return True
    _probe_last_restart = time.time()
    _probe_streak = 0
    restart_gateway(detail)
    return True


def parkworthy(counts: dict, active: dict) -> dict:
    """Filter raw counts to actionable parks: active, unprotected, >= MIN_HITS."""
    out: dict[str, int] = {}
    for prov, hits in counts.items():
        if hits < MIN_HITS:
            continue
        if prov in PROTECTED:
            log(f"skip {prov}: PROTECTED lane ({hits} auth hits ignored)")
            continue
        if prov not in active:
            log(f"skip {prov}: not an active provider (already parked / unknown) - {hits} stale auth hits")
            continue
        out[prov] = hits
    return out


# ── park action ──────────────────────────────────────────────────────────────
def _snapshot(path: str, tag: str) -> None:
    if os.path.exists(path):
        dst = f"{path}.bak.autoheal-{tag}"
        shutil.copy2(path, dst)
        log(f"snapshot {os.path.basename(path)} -> {os.path.basename(dst)}")


def _deregister(provider: str) -> dict:
    """Delete all config.db rows for `provider`. Returns per-table rowcounts.
    Child join rows are deleted before their parent PC rows."""
    db = sqlite3.connect(CONFIG_DB, timeout=30)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        counts: dict[str, int] = {}
        counts["vk_pc_keys"] = db.execute(
            "DELETE FROM governance_virtual_key_provider_config_keys "
            "WHERE table_virtual_key_provider_config_id IN "
            "(SELECT id FROM governance_virtual_key_provider_configs WHERE provider=?)",
            (provider,),
        ).rowcount
        counts["vk_pc"] = db.execute(
            "DELETE FROM governance_virtual_key_provider_configs WHERE provider=?",
            (provider,),
        ).rowcount
        if PURGE_PRICING:
            counts["model_pricing"] = db.execute(
                "DELETE FROM governance_model_pricing WHERE provider=?", (provider,)
            ).rowcount
        else:
            counts["model_pricing"] = "left-alone (repo policy; set AUTOHEAL_PURGE_PRICING=true to purge)"
        counts["config_keys"] = db.execute(
            "DELETE FROM config_keys WHERE provider=?", (provider,)
        ).rowcount
        counts["config_providers"] = db.execute(
            "DELETE FROM config_providers WHERE name=?", (provider,)
        ).rowcount
        db.commit()
        return counts
    finally:
        db.close()


def _move_block_to_disabled(provider: str, hits: int) -> None:
    with open(CONFIG_JSON, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    block = cfg.get("providers", {}).pop(provider, None)
    if block is None:
        raise RuntimeError(f"provider {provider} vanished from config.json before move")

    note = (
        f"auto-parked {provider} {_now_iso()} by bifrost-autoheal "
        f"(sustained auth failure: {hits} hits in {WINDOW_S}s). "
        f"TO RE-ENABLE: fix the key in shared-infra/.env, copy this block back into "
        f"config.json providers, run sync_vk_allowlists.py (bifrost stopped), restart."
    )
    if isinstance(block.get("_comment"), str):
        block["_prev_comment"] = block["_comment"]
    block["_comment"] = note

    with open(DISABLED_JSON, "r", encoding="utf-8") as f:
        disabled = json.load(f)
    disabled.setdefault("providers", {})[provider] = block

    # write disabled first (so the recipe is never lost), then config
    with open(DISABLED_JSON, "w", encoding="utf-8") as f:
        json.dump(disabled, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(CONFIG_JSON, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    log(f"moved providers[{provider}] config.json -> disabled-providers.json")


def _run_sync() -> None:
    env = {**os.environ, "BIFROST_CONFIG_DB": CONFIG_DB}
    r = subprocess.run(
        [sys.executable, SYNC_SCRIPT],
        env=env, capture_output=True, text=True, timeout=120,
    )
    for ln in (r.stdout or "").splitlines():
        log(f"sync: {ln}")
    if r.returncode != 0:
        raise RuntimeError(f"sync_vk_allowlists.py exit {r.returncode}: {r.stderr.strip()[:300]}")


def _alert(text: str) -> None:
    try:
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{_now_iso()} {text}\n")
    except Exception as e:  # noqa: BLE001
        log(f"warn: could not write {ALERT_LOG} ({e!r})")
    if DISCORD_WEBHOOK:
        try:
            data = json.dumps({"content": f":robot: bifrost-autoheal: {text}"}).encode()
            req = urllib.request.Request(
                DISCORD_WEBHOOK, data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15).read()
            log("discord alert posted")
        except Exception as e:  # noqa: BLE001
            log(f"warn: discord webhook failed ({e!r})")


def park(provider: str, hits: int) -> None:
    tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + f"-{provider}"
    log(f"PARK {provider}: {hits} auth hits >= {MIN_HITS} in {WINDOW_S}s -> parking")
    stopped = False
    try:
        # 2. snapshots
        _snapshot(CONFIG_JSON, tag)
        _snapshot(DISABLED_JSON, tag)
        _snapshot(CONFIG_DB, tag)
        # 3. stop bifrost (release config.db)
        log(f"docker stop {BIFROST_CONTAINER} -> HTTP {docker_stop(BIFROST_CONTAINER)}")
        stopped = True
        # 4. move provider block json->json
        _move_block_to_disabled(provider, hits)
        # 5. deregister from config.db
        counts = _deregister(provider)
        log(f"deregistered {provider} from config.db: {counts}")
        # 6. resync VK allowlists
        _run_sync()
    finally:
        # 7. always bring bifrost back up
        if stopped:
            try:
                log(f"docker start {BIFROST_CONTAINER} -> HTTP {docker_start(BIFROST_CONTAINER)}")
            except Exception as e:  # noqa: BLE001
                log(f"CRITICAL: failed to restart {BIFROST_CONTAINER} after park: {e!r}")
    # 8. alert
    _alert(
        f"auto-parked provider '{provider}' after {hits} sustained auth failures "
        f"in {WINDOW_S}s; {BIFROST_CONTAINER} restarted."
    )
    log(f"PARK {provider}: complete")


# ── passes ────────────────────────────────────────────────────────────────────
def run_once(dry: bool) -> int:
    """One detection pass. Returns number of providers parked (or that WOULD be
    parked in dry mode). Never raises - a transient docker/sqlite error is logged
    and treated as 'nothing to do'."""
    if PROBE_ENABLED:
        try:
            check_hang(dry)
        except Exception as e:  # noqa: BLE001
            log(f"probe error ({e!r}); auth pass continues")
    try:
        active = load_active_providers()
        kmap = key_provider_map()
        logs_text = docker_logs(BIFROST_CONTAINER, WINDOW_S)
        counts = count_auth_failures(logs_text, kmap)
        if counts:
            log(f"auth-failure tally (last {WINDOW_S}s): "
                + ", ".join(f"{p}={n}" for p, n in sorted(counts.items())))
        else:
            log(f"clean poll: 0 provider auth-failure lines in last {WINDOW_S}s")
        targets = parkworthy(counts, active)
        if not targets:
            log("0 providers to park")
            return 0
        for prov, hits in sorted(targets.items()):
            if dry:
                log(f"DRY-RUN: WOULD park {prov} ({hits} auth hits in {WINDOW_S}s) - no action taken")
            else:
                park(prov, hits)
        return len(targets)
    except Exception as e:  # noqa: BLE001
        log(f"pass error ({e!r}); skipping this cycle")
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Bifrost auth-autoheal sidecar")
    ap.add_argument("--once", action="store_true", help="single detection pass then exit")
    ap.add_argument("--dry-run", action="store_true", help="detect + log only, never park")
    ap.add_argument("--probe-only", action="store_true",
                    help="run only the synthetic completion probe once, then exit")
    args = ap.parse_args()
    dry = args.dry_run or DRY_RUN

    log(
        f"start: container={BIFROST_CONTAINER} interval={INTERVAL_S}s window={WINDOW_S}s "
        f"min_hits={MIN_HITS} dry_run={dry} protected={sorted(PROTECTED)} "
        f"discord={'on' if DISCORD_WEBHOOK else 'off'} "
        f"probe={'on' if PROBE_ENABLED else 'off'} probe_model={PROBE_MODEL} "
        f"probe_timeout={PROBE_TIMEOUT_S}s probe_streak={PROBE_FAIL_STREAK}"
    )
    if args.probe_only:
        check_hang(dry)
        return
    if args.once:
        n = run_once(dry)
        log(f"--once done: {n} provider(s) {'would be ' if dry else ''}parked")
        return
    while True:
        run_once(dry)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
