"""Per-consumer health probe — GETs each consumer's `health_url` from
consumers.yaml when one is set (ada, legion; zero is intentionally off,
a-finance/fortressos/hermes have no known health URL and are reported as
`no_health_url`, never as down). This does NOT attempt the fortressos/hermes
VK-last-seen check (their own admin surfaces are outside this repo) — a red
`no_health_url` entry is informational only and never counts against `ok`."""
from __future__ import annotations

import time

import httpx

from infractl import consumers as consumers_pkg
from infractl.probes.base import result
from infractl.settings import Settings

SELF_CONTAINER = "shared-infra-control"


def _containerize_url(url: str) -> str:
    """consumers.yaml's health_url values are host-perspective
    (127.0.0.1:<port>) — written for a human/script running ON THE HOST,
    same convention as this repo's README/CLAUDE.md port tables. infractl
    itself always runs inside a container, where 127.0.0.1 is its OWN
    loopback, not the host's (found live 2026-09-15: every consumer health
    check ConnectError'd until this rewrite). docker-compose.control.yml
    adds `extra_hosts: host.docker.internal:host-gateway` specifically so
    this rewrite has somewhere to resolve to."""
    return url.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")


def probe(settings: Settings) -> dict:
    t0 = time.time()
    try:
        consumers = consumers_pkg.list_consumers(settings)
    except consumers_pkg.ConsumersSchemaError as exc:
        return result("consumers", False, f"consumers.yaml invalid: {exc}", t0)

    down: list[str] = []
    unknown: list[str] = []
    checked = 0
    for c in consumers:
        url = c.get("health_url")
        if not url:
            unknown.append(c["name"])
            continue
        if SELF_CONTAINER in (c.get("containers") or []):
            # This consumer IS infractl itself (claude-code-local's
            # health_url points at :8095/healthz). Routing that request
            # out through host.docker.internal back to our own published
            # port is a Docker-Desktop-for-Windows hairpin-NAT round trip
            # that measured a consistent ReadTimeout live 2026-09-15 even
            # though the process answering it is this exact process —
            # skip the network hop and report it directly instead of
            # chasing a NAT quirk to probe something already known.
            checked += 1
            continue
        checked += 1
        try:
            resp = httpx.get(_containerize_url(url), timeout=5.0)
            if resp.status_code >= 500:
                down.append(f"{c['name']}:HTTP{resp.status_code}")
        except httpx.HTTPError as exc:
            down.append(f"{c['name']}:{type(exc).__name__}")

    ok = not down
    detail = f"{checked} checked, {len(down)} down, {len(unknown)} no_health_url ({unknown})"
    if down:
        detail += f" down={down}"
    return result("consumers", ok, detail, t0)
