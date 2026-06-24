#!/usr/bin/env python3
"""vLLM wedge monitor - load-immune detection of a stuck engine.

Companion to the cheap /health healthcheck shipped 2026-06-24 (docker-compose.vllm.yml).
That change ended the generation-ping restart STORM (the ping false-positived whenever
the 5090 was busy under concurrent ADA/Legion/Zero load), but it no longer catches the
rare "engine loop alive, /health 200, but 0 tok/s with requests queued" wedge (observed
once 2026-06-12). This monitor restores that detection WITHOUT the false-positive storm
by reading vLLM's Prometheus /metrics instead of timing a generation:

  WEDGE := for WEDGE_SAMPLES consecutive polls,
           vllm:num_requests_running > 0
           AND neither vllm:generation_tokens_total NOR vllm:prompt_tokens_total advanced.

Why this is load-immune where the generation ping was not:
  - A busy-but-progressing engine increments generation_tokens_total (decode) or
    prompt_tokens_total (prefill of a long prompt) every poll -> NEVER trips.
  - An idle engine has num_requests_running == 0 -> never trips.
  - Only a genuine stall (work present, zero forward progress on BOTH counters) fires,
    and only after WEDGE_SAMPLES consecutive confirmations. Then we restart the target
    via the Docker socket - exactly what vllm-autoheal used to do off the generation ping,
    minus the false positives.

Stdlib only (urllib for /metrics, raw http.client over the Docker unix socket for the
restart) so the stock python:slim image needs no pip install.
"""
from __future__ import annotations

import os
import socket
import time
import urllib.request
from http.client import HTTPConnection

METRICS_URL = os.environ.get("VLLM_METRICS_URL", "http://host.docker.internal:18801/metrics")
TARGET = os.environ.get("VLLM_TARGET_CONTAINER", "vllm-chat")
POLL_S = int(os.environ.get("POLL_INTERVAL_S", "30"))
WEDGE_N = int(os.environ.get("WEDGE_SAMPLES", "4"))            # 4 x 30s = ~2 min stall
GRACE_S = int(os.environ.get("STARTUP_GRACE_S", "120"))       # ignore right after (re)start
RESTART_COOLDOWN_S = int(os.environ.get("RESTART_COOLDOWN_S", "1800"))  # >=30 min between restarts
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
DOCKER_API = os.environ.get("DOCKER_API_VERSION", "v1.41")


def log(msg: str) -> None:
    print(f"[wedge-monitor] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def fetch_metrics() -> str:
    with urllib.request.urlopen(METRICS_URL, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def parse(metrics: str) -> tuple[float, float, float]:
    """Return (num_requests_running, generation_tokens_total, prompt_tokens_total),
    summed across label sets (engine/model_name). Exact metric-name match so a
    *_by_reason variant can never be mistaken for the base counter."""
    running = gen = prompt = 0.0
    for line in metrics.splitlines():
        if not line or line[0] == "#":
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        try:
            val = float(line.rsplit(" ", 1)[-1])
        except ValueError:
            continue
        if name == "vllm:num_requests_running":
            running += val
        elif name == "vllm:generation_tokens_total":
            gen += val
        elif name == "vllm:prompt_tokens_total":
            prompt += val
    return running, gen, prompt


class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, sock_path: str) -> None:
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(70)
        s.connect(self._sock_path)
        self.sock = s


def docker_restart(name: str) -> int:
    conn = _UnixHTTPConnection(DOCKER_SOCK)
    try:
        conn.request("POST", f"/{DOCKER_API}/containers/{name}/restart?t=60")
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def main() -> None:
    log(f"start: metrics={METRICS_URL} target={TARGET} poll={POLL_S}s "
        f"wedge={WEDGE_N} samples grace={GRACE_S}s cooldown={RESTART_COOLDOWN_S}s")
    last_gen: float | None = None
    last_prompt: float | None = None
    flat = 0
    last_restart = 0.0
    epoch = time.time()  # (re)start grace anchor

    while True:
        try:
            running, gen, prompt = parse(fetch_metrics())
            progressed = (
                last_gen is None
                or gen > last_gen + 0.5
                or last_prompt is None
                or prompt > last_prompt + 0.5
            )
            if last_gen is not None and running > 0 and not progressed:
                flat += 1
                log(f"stall sample {flat}/{WEDGE_N}: running={running:.0f} "
                    f"gen={gen:.0f} prompt={prompt:.0f} (no forward progress)")
            else:
                if flat:
                    log(f"cleared after {flat} stall sample(s): running={running:.0f} progressed={progressed}")
                flat = 0
            last_gen, last_prompt = gen, prompt

            now = time.time()
            if (flat >= WEDGE_N
                    and now - epoch > GRACE_S
                    and now - last_restart > RESTART_COOLDOWN_S):
                log(f"WEDGE DETECTED: {WEDGE_N} consecutive polls with requests running and "
                    f"zero token progress -> restarting {TARGET}")
                try:
                    status = docker_restart(TARGET)
                    log(f"restart {TARGET} -> HTTP {status}")
                except Exception as e:  # noqa: BLE001
                    log(f"restart FAILED: {e!r}")
                last_restart = now
                flat = 0
                last_gen = last_prompt = None
                epoch = now  # re-grace after restart so the reload doesn't read as a wedge
        except Exception as e:  # noqa: BLE001 - metrics unreachable = loading/down (that is /health's job)
            log(f"poll skipped ({e!r}) - metrics unreachable; resetting stall counter")
            flat = 0
            last_gen = last_prompt = None
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
