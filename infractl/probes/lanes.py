"""Derives one probe lane per active provider from config.json — ported from
`bifrost-metrics-exporter/exporter.py`'s `_load_probe_lanes()` (that
exporter re-derives every `BIFROST_PROBE_TICK_S` [60s] already and writes
its OWN `probe_lanes.json` into the `bifrost-metrics-state` volume, which
infractl does not mount). This is a deliberate parallel implementation, not
a duplicate-for-duplication's-sake: infractl has its own `/state` volume
and its own `/api/probes` + UI surface, and the exporter's private state
volume is not something infractl can read without mounting a second
container's volume into a third container — the ported function is the same
well-defined algorithm (same field order, same model-preference table, same
critical/fallback tiering) kept in sync by comment, called via the
`probe_lanes_regenerate` T1 action (core/actions.py) and infractl's own
hourly scheduler tick, and written to infractl's `/state/probe_lanes.json`.
This module does NOT touch the exporter's own copy."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from infractl.bifrost import config as bifrost_config
from infractl.settings import Settings

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

DEFAULT_CRITICAL_PROVIDERS = {"vllm-local", "embed-local", "nvidia-nim", "groq"}
PERIOD_CRITICAL_S = 600
PERIOD_FALLBACK_S = 3600
TIMEOUT_LOCAL_S = 120
TIMEOUT_S = 60


def _probe_model_prefs() -> dict[str, list[str]]:
    try:
        override = json.loads(os.environ.get("BIFROST_PROBE_MODELS", "{}"))
    except (ValueError, TypeError):
        override = {}
    return {**_DEFAULT_PROBE_MODEL_PREFS, **{k: list(v) for k, v in override.items()}}


def _critical_providers() -> set[str]:
    raw = os.environ.get("BIFROST_PROBE_CRITICAL_PROVIDERS", "")
    if not raw:
        return set(DEFAULT_CRITICAL_PROVIDERS)
    return {p.strip() for p in raw.split(",") if p.strip()}


def load_probe_lanes(config_json: Path) -> tuple[list[dict], dict[str, str]]:
    """Returns (lanes, skipped). Never raises — an unreadable config.json
    yields ([], {}); the caller decides whether that is fatal."""
    try:
        cfg = bifrost_config.read_config(config_json)
    except (OSError, ValueError):
        return [], {}

    prefs = _probe_model_prefs()
    critical = _critical_providers()
    lanes: list[dict] = []
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
        model_prefs = prefs.get(provider, [])
        model = next((m for m in model_prefs if m in models), models[0])
        kind = "embed" if "embed" in provider.lower() else "chat"
        tier = "critical" if provider in critical else "fallback"
        period = PERIOD_CRITICAL_S if tier == "critical" else PERIOD_FALLBACK_S
        timeout = TIMEOUT_LOCAL_S if provider.endswith("-local") else TIMEOUT_S
        lanes.append({
            "model": f"{provider}/{model}", "kind": kind, "tier": tier,
            "period_s": period, "timeout_s": timeout,
        })
    return lanes, skipped


def regenerate_probe_lanes(settings: Settings) -> dict:
    lanes, skipped = load_probe_lanes(settings.config_json_path)
    out_path = settings.infractl_state_dir / "probe_lanes.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": str(settings.config_json_path),
        "lanes": lanes,
        "skipped": skipped,
    }
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, out_path)
    return {"lane_count": len(lanes), "skipped": skipped, "written_to": str(out_path)}
