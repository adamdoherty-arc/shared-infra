"""Container health probe — one `GET /containers/json` call (Docker already
annotates each entry's `Status` string with `(healthy)`/`(unhealthy)` for
every container that defines a HEALTHCHECK), so this never pays for N
`inspect` round trips the way a per-container health.Status walk would.

Watches ONLY containers declared in THIS repo's own compose files (mounted
read-only under `settings.infractl_compose_dir`), never every container on
the shared Docker daemon. Found live 2026-09-15: with no filter this probe
flagged ada-init/zero-api/erpnext-create-site-1/erpnext-configurator-1/
odysseus-odysseus-1 as "stopped" — every one of those is another project's
legitimate one-shot init container that's SUPPOSED to exit 0 after doing its
job. infractl's job is the shared-infra stack, not policing every other
project's container lifecycle; the compose-derived allowlist is also a more
accurate answer than any hand-maintained list, since it moves automatically
when a compose file gains/loses a service."""
from __future__ import annotations

import time
from pathlib import Path

import yaml

from infractl.core import docker as docker_client
from infractl.core.docker import DockerError
from infractl.probes.base import result
from infractl.settings import Settings

SELF_CONTAINER = "shared-infra-control"


def watched_container_names(compose_dir: Path) -> set[str]:
    """Parses `container_name:` out of every `docker-compose*.yml` under
    compose_dir. Never raises — an unreadable/missing compose dir yields an
    empty set, and the caller then falls back to watching nothing rather
    than silently reverting to "watch everything" (a fail-closed default:
    an empty watch list is visibly wrong in the probe detail string, an
    accidental "watch everything on the host" would not be)."""
    names: set[str] = set()
    if not compose_dir.exists():
        return names
    for path in sorted(compose_dir.glob("docker-compose*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        for svc in (data or {}).get("services", {}).values():
            if isinstance(svc, dict) and svc.get("container_name"):
                names.add(svc["container_name"])
    return names


def check_raw(settings: Settings) -> dict:
    """Returns {unhealthy: [name...], stopped: [name:state...], total: int,
    watched: int} — the per-container breakdown heal/rules.py needs to pick
    a restart target, separate from probe()'s aggregate
    {name,ok,detail,...} shape."""
    watch_set = watched_container_names(settings.infractl_compose_dir)
    containers = docker_client.list_containers(all_=True)
    unhealthy: list[str] = []
    stopped: list[str] = []
    total = 0
    for c in containers:
        names = c.get("Names") or []
        name = names[0].lstrip("/") if names else (c.get("Id", "?")[:12])
        if name == SELF_CONTAINER or name not in watch_set:
            continue
        total += 1
        status_text = c.get("Status", "") or ""
        state = c.get("State", "") or ""
        if "(unhealthy)" in status_text:
            unhealthy.append(name)
        elif state not in ("running",):
            stopped.append(f"{name}:{state}")
    return {"unhealthy": unhealthy, "stopped": stopped, "total": total, "watched": len(watch_set)}


def probe(settings: Settings) -> dict:
    t0 = time.time()
    try:
        raw = check_raw(settings)
    except DockerError as exc:
        return result("containers", False, f"docker socket error: {exc}", t0)

    if raw["watched"] == 0:
        return result("containers", False,
                       f"0 shared-infra containers resolved from {settings.infractl_compose_dir} — "
                       f"compose files missing/unmounted/unparseable", t0)

    ok = not raw["unhealthy"] and not raw["stopped"]
    detail = (
        f"{raw['total']}/{raw['watched']} shared-infra containers watched, all running/healthy"
        if ok else f"unhealthy={raw['unhealthy']} stopped={raw['stopped']}"
    )
    return result("containers", ok, detail, t0)
