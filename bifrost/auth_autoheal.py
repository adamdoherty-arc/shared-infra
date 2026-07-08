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
    args = ap.parse_args()
    dry = args.dry_run or DRY_RUN

    log(
        f"start: container={BIFROST_CONTAINER} interval={INTERVAL_S}s window={WINDOW_S}s "
        f"min_hits={MIN_HITS} dry_run={dry} protected={sorted(PROTECTED)} "
        f"discord={'on' if DISCORD_WEBHOOK else 'off'}"
    )
    if args.once:
        n = run_once(dry)
        log(f"--once done: {n} provider(s) {'would be ' if dry else ''}parked")
        return
    while True:
        run_once(dry)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
