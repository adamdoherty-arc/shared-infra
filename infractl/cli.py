"""infractl CLI — a thin httpx client over the running API, authenticated
with the same INFRACTL_TOKEN env var the server reads. Never talks to
bifrost/ledger files directly, so `infractl` run from anywhere (host,
another container, an operator's shell) behaves identically to a curl call
against the API, and the single-writer lock is never bypassed."""
from __future__ import annotations

import json
import os
import sys

import httpx
import typer

app = typer.Typer(add_completion=False, help="infractl — shared-infra control plane CLI")
config_app = typer.Typer(help="config lint/snapshot")
actions_app = typer.Typer(help="action list/approve/rollback")
probes_app = typer.Typer(help="probe operations")
vk_app = typer.Typer(help="virtual-key operations")
app.add_typer(config_app, name="config")
app.add_typer(actions_app, name="actions")
app.add_typer(probes_app, name="probes")
app.add_typer(vk_app, name="vk")


def _base_url() -> str:
    return os.environ.get("INFRACTL_CLI_BASE_URL", "http://127.0.0.1:8095")


def _token() -> str:
    token = os.environ.get("INFRACTL_TOKEN", "")
    if not token:
        typer.secho("INFRACTL_TOKEN not set in environment", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return token


def _client() -> httpx.Client:
    return httpx.Client(base_url=_base_url(), headers={"X-Infractl-Token": _token()}, timeout=30.0)


def _print(resp: httpx.Response) -> None:
    try:
        body = resp.json()
    except ValueError:
        typer.echo(resp.text)
        return
    typer.echo(json.dumps(body, indent=2))
    if not body.get("ok", True):
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """GET /api/status"""
    with _client() as c:
        _print(c.get("/api/status"))


@app.command()
def health() -> None:
    """GET /api/health (runs all probes live)"""
    with _client() as c:
        _print(c.get("/api/health"))


@app.command()
def consumers() -> None:
    """GET /api/consumers"""
    with _client() as c:
        _print(c.get("/api/consumers"))


@probes_app.command("run")
def probes_run() -> None:
    with _client() as c:
        _print(c.post("/api/probes/run"))


@actions_app.command("list")
def actions_list(since: float | None = None, status: str | None = None) -> None:
    params = {}
    if since is not None:
        params["since"] = since
    if status is not None:
        params["status"] = status
    with _client() as c:
        _print(c.get("/api/audit", params=params))


@actions_app.command("approve")
def actions_approve(action_id: str) -> None:
    with _client() as c:
        _print(c.post(f"/api/actions/{action_id}/approve"))


@actions_app.command("rollback")
def actions_rollback(action_id: str) -> None:
    with _client() as c:
        _print(c.post(f"/api/actions/{action_id}/rollback"))


@app.command()
def park(provider: str, reason: str = typer.Option(..., "--reason"),
          dry_run: bool = typer.Option(None, "--dry-run/--apply")) -> None:
    body: dict = {"reason": reason, "requested_by": f"cli:{os.environ.get('USERNAME', 'unknown')}"}
    if dry_run is not None:
        body["dry_run"] = dry_run
    with _client() as c:
        _print(c.post(f"/api/config/providers/{provider}/park", json=body))


@app.command()
def unpark(provider: str, dry_run: bool = typer.Option(None, "--dry-run/--apply")) -> None:
    body: dict = {"requested_by": f"cli:{os.environ.get('USERNAME', 'unknown')}"}
    if dry_run is not None:
        body["dry_run"] = dry_run
    with _client() as c:
        _print(c.post(f"/api/config/providers/{provider}/unpark", json=body))


@vk_app.command("resync")
def vk_resync(dry_run: bool = typer.Option(None, "--dry-run/--apply")) -> None:
    body: dict = {"requested_by": f"cli:{os.environ.get('USERNAME', 'unknown')}"}
    if dry_run is not None:
        body["dry_run"] = dry_run
    with _client() as c:
        _print(c.post("/api/vk/resync", json=body))


@config_app.command("lint")
def config_lint() -> None:
    with _client() as c:
        _print(c.get("/api/config/lint"))


@config_app.command("snapshot")
def config_snapshot() -> None:
    with _client() as c:
        _print(c.get("/api/config/snapshot"))


if __name__ == "__main__":
    app()
