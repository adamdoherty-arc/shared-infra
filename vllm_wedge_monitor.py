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

import json
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
# Fix-100787 (2026-06-29): cooldown lowered 1800s -> 600s. The 30-min cooldown was
# leaving vllm-chat wedged for HOURS because samples 5..N kept incrementing flat
# without restart firing (cycle-2 incident: 26 consecutive stall samples). 10 min
# is plenty of headroom between restarts but short enough that a same-day re-wedge
# gets healed automatically. Per-poll heartbeat below shows the 3 gate conditions
# so the next cooldown-related blockage is debuggable from the log alone.
RESTART_COOLDOWN_S = int(os.environ.get("RESTART_COOLDOWN_S", "600"))
# Fix-1000282 (2026-07-24): a fetch_metrics() exception (DNS/connect failure to
# METRICS_URL) used to unconditionally reset `flat` to 0 every poll, so a
# vllm-chat that's unreachable (crashed, network partition, name-resolution
# failure post-recreate) could NEVER accumulate to a restart -- the monitor
# went blind exactly when the wedge signal mattered most. This tracks
# consecutive-unreachable polls on its OWN counter (default 2x WEDGE_N, i.e.
# ~4 min of true unreachability) so a short DNS blip during a normal restart
# doesn't over-fire, but a genuinely stuck/unreachable vllm-chat still gets
# healed instead of silently sitting dark forever.
UNREACHABLE_SAMPLES = int(os.environ.get("UNREACHABLE_SAMPLES", str(WEDGE_N * 2)))
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
    def __init__(self, sock_path: str, timeout: float = 70) -> None:
        super().__init__("localhost")
        self._sock_path = sock_path
        self._timeout = timeout

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self._timeout)
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


# --- py-spy auto-capture (daily-review run 17, 2026-07-10) --------------------
# The single-request engine freeze (running=1, both token counters flat) keeps
# recurring under seqs=1 (07-04, 07-08, and a 6-wedge burst 07-09) — seqs=1 caps
# the RATE but does not eliminate it. The ONLY path to the kernel/engine root
# cause is a stack of the EngineCore process DURING the freeze. That was said to
# be "armed" via CAP_SYS_PTRACE on vllm-chat, but was never actually usable:
# py-spy was never installed AND nobody is present at 03:58Z. This grabs the
# dump AUTOMATICALLY in the ~2-min detection window BEFORE the restart wipes it,
# so every future wedge leaves an EngineCore stack in this container's log.
# Fully guarded: any failure/timeout logs and falls through to the (unchanged)
# restart — it can never delay the heal.
PYSPY_ON_WEDGE = os.environ.get("PYSPY_ON_WEDGE", "1") == "1"
PYSPY_TIMEOUT_S = int(os.environ.get("PYSPY_DUMP_TIMEOUT_S", "60"))
_PYSPY_CMD = (
    "command -v py-spy >/dev/null 2>&1 || pip install -q py-spy >/dev/null 2>&1; "
    "pid=$(ps -eo pid,args | grep -i EngineCore | grep -v grep "
    "| awk '{print $1}' | head -1); "
    "[ -n \"$pid\" ] && py-spy dump --pid \"$pid\" --nonblocking 2>&1 "
    "|| echo 'py-spy: no EngineCore pid found'"
)


def _docker_exec_capture(name: str, cmd: list[str], timeout: int) -> str:
    """Run cmd inside container `name` via the Docker Exec API and return its
    combined output. Tty:true gives a raw (un-multiplexed) stream. Bounded by
    the socket timeout so a hung exec can never delay the caller."""
    conn = _UnixHTTPConnection(DOCKER_SOCK)
    try:
        body = json.dumps({
            "AttachStdout": True, "AttachStderr": True, "Tty": True, "Cmd": cmd,
        }).encode()
        conn.request("POST", f"/{DOCKER_API}/containers/{name}/exec", body=body,
                     headers={"Content-Type": "application/json"})
        exec_id = json.loads(conn.getresponse().read()).get("Id")
    finally:
        conn.close()
    if not exec_id:
        return "py-spy: exec create returned no Id"
    conn = _UnixHTTPConnection(DOCKER_SOCK, timeout)
    try:
        conn.request("POST", f"/{DOCKER_API}/exec/{exec_id}/start",
                     body=json.dumps({"Detach": False, "Tty": True}).encode(),
                     headers={"Content-Type": "application/json"})
        return conn.getresponse().read(65536).decode("utf-8", "replace")
    finally:
        conn.close()


def capture_pyspy_dump(name: str) -> None:
    """Best-effort EngineCore stack dump into this monitor's log, before the
    restart. Never raises."""
    if not PYSPY_ON_WEDGE:
        return
    try:
        out = _docker_exec_capture(name, ["sh", "-lc", _PYSPY_CMD], PYSPY_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        log(f"py-spy capture failed: {e!r} (restart proceeds)")
        return
    log(f"py-spy EngineCore dump BEGIN ({name}) >>>>>")
    for line in (out or "(empty)").splitlines():
        print(f"[wedge-monitor:pyspy] {line}", flush=True)
    log("py-spy EngineCore dump END <<<<<")


def main() -> None:
    log(f"start: metrics={METRICS_URL} target={TARGET} poll={POLL_S}s "
        f"wedge={WEDGE_N} samples grace={GRACE_S}s cooldown={RESTART_COOLDOWN_S}s "
        f"unreachable={UNREACHABLE_SAMPLES} samples")
    last_gen: float | None = None
    last_prompt: float | None = None
    flat = 0
    unreachable = 0
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
            if unreachable:
                log(f"metrics reachable again after {unreachable} unreachable poll(s)")
                unreachable = 0
            last_gen, last_prompt = gen, prompt

            now = time.time()
            # Fix-100787 (2026-06-29): per-poll heartbeat when at-or-past
            # threshold. Cycle-2 forensics showed samples 5..26 logged with
            # flat>=4 but NO "WEDGE DETECTED" — and no diagnostic of why the
            # 3-gate failed. With this heartbeat, every poll at threshold
            # logs whether each gate is open/closed, making the next
            # cooldown-related blockage debuggable from the log alone.
            since_epoch = now - epoch
            since_last_restart = now - last_restart
            gate_flat = flat >= WEDGE_N
            gate_grace = since_epoch > GRACE_S
            gate_cooldown = since_last_restart > RESTART_COOLDOWN_S
            if gate_flat:
                log(
                    f"restart-gate: flat={flat}/{WEDGE_N} ({'OPEN' if gate_flat else 'CLOSED'}) "
                    f"since_epoch={int(since_epoch)}s/{GRACE_S}s ({'OPEN' if gate_grace else 'CLOSED'}) "
                    f"since_last_restart={int(since_last_restart)}s/{RESTART_COOLDOWN_S}s "
                    f"({'OPEN' if gate_cooldown else 'CLOSED'})"
                )
            if gate_flat and gate_grace and gate_cooldown:
                log(f"WEDGE DETECTED: {WEDGE_N} consecutive polls with requests running and "
                    f"zero token progress -> restarting {TARGET}")
                capture_pyspy_dump(TARGET)  # stack the frozen EngineCore before we wipe it
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
            # Fix-1000282 (2026-07-24): unreachable metrics is ALSO a wedge signal
            # (a truly-stuck/dead vllm-chat can stop answering /metrics entirely) and
            # must accumulate on its own counter instead of resetting `flat` every
            # poll -- the old behavior meant sustained unreachability could NEVER
            # trip the stall-wedge branch above, leaving the monitor blind exactly
            # when detection mattered most. `flat` is deliberately preserved here
            # (not reset) so a metrics blip during an active stall streak doesn't
            # erase real progress toward that gate either.
            unreachable += 1
            log(f"poll UNREACHABLE {unreachable}/{UNREACHABLE_SAMPLES} ({e!r}) - "
                f"preserving stall counter (flat={flat})")
            now = time.time()
            gate_unreachable = unreachable >= UNREACHABLE_SAMPLES
            gate_grace = (now - epoch) > GRACE_S
            gate_cooldown = (now - last_restart) > RESTART_COOLDOWN_S
            if gate_unreachable and gate_grace and gate_cooldown:
                log(f"WEDGE DETECTED (unreachable): {unreachable} consecutive polls could not "
                    f"reach {METRICS_URL} -> restarting {TARGET}")
                capture_pyspy_dump(TARGET)  # best-effort; likely 'no EngineCore pid' if engine is truly gone
                try:
                    status = docker_restart(TARGET)
                    log(f"restart {TARGET} -> HTTP {status}")
                except Exception as exc:  # noqa: BLE001
                    log(f"restart FAILED: {exc!r}")
                last_restart = now
                unreachable = 0
                flat = 0
                last_gen = last_prompt = None
                epoch = now
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
