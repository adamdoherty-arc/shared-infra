"""infractl settings — every INFRACTL_*/INFRA_* env var lives here, nowhere else.

WRITE_MODE default is dry_run for the first three days after Wave 1 ships
(2026-09-15 -> 2026-09-18); flip to `apply` in .env once the write-path proof
has run clean in production for that window. dry_run means every T1/T2 action
still runs its full lock -> drift-guard -> snapshot -> apply -> verify chain
against a SCRATCH COPY of config.json (never the live file) and records the
would-be diff in the ledger with dry_run=1 -- it is not a no-op, it is a
real dry run of the real code path (NOTHING IS MOCKED: no separate
"simulate" branch, same functions, same pure-function config edits, only the
final os.replace onto the live file is skipped).

T1 actions (core/actions.py's REGISTRY) are the one exception: they never
touch config.json/config.db by definition (restart_sidecar, vk_resync,
wal_checkpoint, logsdb_quick_check, probe_lanes_regenerate), so dry_run has
nothing to fake a scratch copy of and T1 kinds always run for real. This is
a narrower guarantee than "nothing mutates during dry_run" -- it is "nothing
that could corrupt or misroute Bifrost's routing config runs for real during
dry_run"; a sidecar restart or a WAL checkpoint carries no such risk.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # ---- auth ----
    infractl_token: str = Field(default="", alias="INFRACTL_TOKEN")

    # ---- write mode ----
    infractl_write_mode: Literal["dry_run", "apply"] = Field(
        default="dry_run", alias="INFRACTL_WRITE_MODE"
    )

    # ---- paths (container-internal; overridable for tests) ----
    infractl_bifrost_dir: Path = Field(default=Path("/data/bifrost"), alias="INFRACTL_BIFROST_DIR")
    infractl_state_dir: Path = Field(default=Path("/state"), alias="INFRACTL_STATE_DIR")
    infractl_reports_dir: Path = Field(default=Path("/reports"), alias="INFRACTL_REPORTS_DIR")
    infractl_observability_dir: Path = Field(
        default=Path("/observability"), alias="INFRACTL_OBSERVABILITY_DIR"
    )
    infractl_consumers_yaml: Path = Field(
        default=Path("/app/consumers.yaml"), alias="INFRACTL_CONSUMERS_YAML"
    )
    infractl_compose_dir: Path = Field(default=Path("/compose"), alias="INFRACTL_COMPOSE_DIR")

    # ---- server ----
    infractl_host: str = Field(default="0.0.0.0", alias="INFRACTL_HOST")
    infractl_port: int = Field(default=8095, alias="INFRACTL_PORT")

    # ---- docker ----
    infractl_docker_socket: str = Field(
        default="/var/run/docker.sock", alias="INFRACTL_DOCKER_SOCKET"
    )

    # ---- probes ----
    infra_probe_vk: str = Field(default="", alias="INFRA_PROBE_VK")
    infractl_pulse_interval_s: int = Field(default=300, alias="INFRACTL_PULSE_INTERVAL_S")
    infractl_synthetic_interval_s: int = Field(
        default=900, alias="INFRACTL_SYNTHETIC_INTERVAL_S"
    )
    infractl_probe_lanes_interval_s: int = Field(
        default=3600, alias="INFRACTL_PROBE_LANES_INTERVAL_S"
    )
    infractl_probe_base: str = Field(
        default="http://shared-bifrost:8080", alias="INFRACTL_PROBE_BASE"
    )
    infractl_bifrost_metrics_url: str = Field(
        default="http://bifrost-metrics:9100/metrics", alias="INFRACTL_BIFROST_METRICS_URL"
    )
    infractl_prometheus_url: str = Field(
        default="http://shared-prometheus:9090", alias="INFRACTL_PROMETHEUS_URL"
    )
    infractl_alertmanager_url: str = Field(
        default="http://shared-alertmanager:9093", alias="INFRACTL_ALERTMANAGER_URL"
    )
    infractl_loki_url: str = Field(default="http://loki:3100", alias="INFRACTL_LOKI_URL")
    infractl_qwen38_url: str = Field(
        default="http://qwen38-chat:18020", alias="INFRACTL_QWEN38_URL"
    )
    infractl_vllm_embed_url: str = Field(
        default="http://vllm-embed:8001", alias="INFRACTL_VLLM_EMBED_URL"
    )
    infractl_dcgm_url: str = Field(
        default="http://dcgm-exporter:9400/metrics", alias="INFRACTL_DCGM_URL"
    )
    # Default "/" — infractl runs inside a Linux container (python:3.12-slim);
    # statvfs("/") reports the container's rootfs mount, which on Docker
    # Desktop for Windows reflects the shared VM disk's usage, not a literal
    # "C:/" path (that Windows-host spelling would raise FileNotFoundError
    # under os.statvfs on Linux — fixed here rather than shipped broken).
    infractl_disk_path: str = Field(default="/", alias="INFRACTL_DISK_PATH")
    infractl_wal_warn_mb: float = Field(default=512.0, alias="INFRACTL_WAL_WARN_MB")

    # ---- infra brain (Gemini, direct — NEVER via Bifrost) ----
    infra_gemini_api_keys: str = Field(default="", alias="INFRA_GEMINI_API_KEYS")
    infra_gemini_api_key_1: str = Field(default="", alias="INFRA_GEMINI_API_KEY_1")
    infra_gemini_api_key_2: str = Field(default="", alias="INFRA_GEMINI_API_KEY_2")
    infra_gemini_api_key_3: str = Field(default="", alias="INFRA_GEMINI_API_KEY_3")
    infra_gemini_api_key_4: str = Field(default="", alias="INFRA_GEMINI_API_KEY_4")
    infra_gemini_api_key_5: str = Field(default="", alias="INFRA_GEMINI_API_KEY_5")
    infra_gemini_model: str = Field(default="gemini-3.8-flash", alias="INFRA_GEMINI_MODEL")
    infra_brain_max_calls_per_day: int = Field(
        default=60, alias="INFRA_BRAIN_MAX_CALLS_PER_DAY"
    )

    # ---- discord ----
    discord_infra_webhook: str = Field(default="", alias="DISCORD_INFRA_WEBHOOK")
    discord_infra_channel_id: str = Field(default="", alias="DISCORD_INFRA_CHANNEL_ID")
    discord_bot_token: str = Field(default="", alias="DISCORD_BOT_TOKEN")

    # ---- legion ----
    legion_api_base: str = Field(
        default="http://host.docker.internal:8005", alias="LEGION_API_BASE"
    )
    infractl_legion_sprint_id: int = Field(default=14975, alias="INFRACTL_LEGION_SPRINT_ID")

    # ---- lock / drift guard ----
    infractl_lock_timeout_s: int = Field(default=60, alias="INFRACTL_LOCK_TIMEOUT_S")
    infractl_drift_window_s: int = Field(default=30, alias="INFRACTL_DRIFT_WINDOW_S")

    @field_validator("infractl_bifrost_dir", "infractl_state_dir", "infractl_reports_dir",
                      "infractl_observability_dir", mode="before")
    @classmethod
    def _coerce_path(cls, v):
        return Path(v) if v else v

    @property
    def config_json_path(self) -> Path:
        return self.infractl_bifrost_dir / "config.json"

    @property
    def config_db_path(self) -> Path:
        return self.infractl_bifrost_dir / "config.db"

    @property
    def disabled_providers_path(self) -> Path:
        return self.infractl_bifrost_dir / "disabled-providers.json"

    @property
    def ledger_db_path(self) -> Path:
        return self.infractl_state_dir / "infractl.db"

    @property
    def snapshots_dir(self) -> Path:
        return self.infractl_state_dir / "snapshots"

    @property
    def lock_path(self) -> Path:
        return self.infractl_bifrost_dir / ".infractl.lock"

    @property
    def dry_run(self) -> bool:
        return self.infractl_write_mode != "apply"

    @property
    def gemini_keys(self) -> list[str]:
        keys: list[str] = []
        if self.infra_gemini_api_keys:
            for k in self.infra_gemini_api_keys.split(","):
                k = k.strip()
                if k and k not in keys:
                    keys.append(k)
        for slot in (
            self.infra_gemini_api_key_1,
            self.infra_gemini_api_key_2,
            self.infra_gemini_api_key_3,
            self.infra_gemini_api_key_4,
            self.infra_gemini_api_key_5,
        ):
            slot = (slot or "").strip()
            if slot and slot not in keys:
                keys.append(slot)
        return keys


def get_settings() -> Settings:
    """Fresh read each call (not a module-level singleton) so tests can
    monkeypatch env vars per-test without import-order fragility."""
    return Settings()
