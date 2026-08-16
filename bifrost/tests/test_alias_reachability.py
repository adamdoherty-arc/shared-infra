"""Bifrost vllm-local alias reachability — Feature-1000533 (Wave-5 WS-5, 2026-08-16).

CI regression preventer #2 from the Wave-5 monitoring plan
(`~/.claude/plans/i-am-not-happy-enchanted-donut.md`, WS-5 section). Iterates
every entry in `providers.vllm-local.keys[0].models` in `config.json` and
makes a live chat-completion call through Bifrost for each one. Fails if any
alias 403s with `virtual_key_not_found` in the body — the exact failure
class that broke `nemotron-3.5-lightning` and `local-chat` for 4 days
(added to vLLM at Pass-7, never synced into Bifrost's per-VK allow-list;
same bug class as the gpt-oss-20b/Qwen3.6-27B gap from Pass-6). Fixed
2026-08-15 by syncing the allow-list; this test exists so the 3rd
occurrence in 30 days doesn't become a 4th.

Runnable standalone (no CI wiring required to execute it locally):
    python -m pytest shared-infra/bifrost/tests/test_alias_reachability.py -v

Requires a live Bifrost gateway reachable at BIFROST_TEST_URL (default
http://localhost:4445, the host-mapped port for shared-bifrost) and a valid
virtual key. The key is resolved in this order:
    1. BIFROST_GATEWAY_KEY / BIFROST_TEST_KEY env var (host or CI runner).
    2. Read live from the running ada-backend container's own environment
       (`docker exec ada-backend printenv BIFROST_GATEWAY_KEY`) — ADA is a
       real Bifrost consumer, so its configured key is a valid VK by
       definition. This is the fallback for local/manual runs where the
       env var isn't exported in the calling shell.

If neither source produces a key, or the gateway is unreachable, every test
in this module is skipped (not failed) — this is a live-infra smoke test,
not a unit test; a missing dev environment is not a code regression.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import requests

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
_BIFROST_URL = os.getenv("BIFROST_TEST_URL", "http://localhost:4445")
_REQUEST_TIMEOUT_S = 30


def _load_vllm_local_aliases() -> list[str]:
    """Read `providers.vllm-local.keys[0].models` from config.json — the
    exact field the plan specifies, same one `.claude/rules/50-llm.md`
    documents as the live 8-alias list."""
    if not _CONFIG_PATH.exists():
        return []
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            config = json.load(f)
        return list(config["providers"]["vllm-local"]["keys"][0]["models"])
    except (KeyError, IndexError, json.JSONDecodeError, OSError):
        return []


def _resolve_bifrost_key() -> str | None:
    """Env var first, then a live read from ada-backend's own container env
    (ADA is a real Bifrost consumer, so its key is guaranteed to be a valid
    VK). Never raises -- returns None on any failure so the caller can skip
    cleanly instead of erroring out a dev environment that doesn't have
    ada-backend running."""
    key = os.getenv("BIFROST_GATEWAY_KEY") or os.getenv("BIFROST_TEST_KEY")
    if key:
        return key
    try:
        result = subprocess.run(
            ["docker", "exec", "ada-backend", "printenv", "BIFROST_GATEWAY_KEY"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            return value or None
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return None


_ALIASES = _load_vllm_local_aliases()
_BIFROST_KEY = _resolve_bifrost_key()

_SKIP_REASON_NO_ALIASES = f"Could not read providers.vllm-local.keys[0].models from {_CONFIG_PATH}"
_SKIP_REASON_NO_KEY = (
    "No Bifrost virtual key available (set BIFROST_GATEWAY_KEY/BIFROST_TEST_KEY, "
    "or run with ada-backend up so it can be read from that container)"
)


@pytest.mark.skipif(not _ALIASES, reason=_SKIP_REASON_NO_ALIASES)
@pytest.mark.skipif(_BIFROST_KEY is None, reason=_SKIP_REASON_NO_KEY)
@pytest.mark.parametrize("alias", _ALIASES or ["__no_aliases_found__"])
def test_vllm_local_alias_is_reachable_through_bifrost(alias: str) -> None:
    """Every alias in vllm-local.keys[0].models must resolve through Bifrost
    -- a 403 with virtual_key_not_found means the alias was added to vLLM's
    --served-model-name list but never synced into Bifrost's per-VK
    governance allow-list (run sync_vk_allowlists.py)."""
    model = f"vllm-local/{alias}"
    try:
        resp = requests.post(
            f"{_BIFROST_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_BIFROST_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "OK"}],
                "max_tokens": 5,
            },
            timeout=_REQUEST_TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        pytest.skip(f"Bifrost gateway unreachable at {_BIFROST_URL}: {exc}")
        return

    if resp.status_code == 403:
        body_text = resp.text
        assert "virtual_key_not_found" not in body_text, (
            f"Alias '{alias}' ({model}) is unreachable through Bifrost: 403 "
            f"virtual_key_not_found. It was added to vLLM's "
            f"--served-model-name list but never synced into Bifrost's "
            f"config.db per-VK allow-list. Fix: "
            f"python shared-infra/bifrost/sync_vk_allowlists.py"
        )
        # A 403 for a different reason (e.g. auth misconfiguration
        # unrelated to this alias) is not this test's concern.
        pytest.skip(f"Alias '{alias}' 403'd for a non-allowlist reason: {body_text[:200]}")

    assert resp.status_code == 200, (
        f"Alias '{alias}' ({model}) returned HTTP {resp.status_code}, expected 200: "
        f"{resp.text[:300]}"
    )
    payload = resp.json()
    assert payload.get("choices"), f"Alias '{alias}' returned 200 but no choices: {payload}"


def test_config_json_vllm_local_alias_count_matches_documented_eight() -> None:
    """Guardrail against silent alias-list drift: `.claude/rules/50-llm.md`
    documents exactly 8 live aliases (2 honest + 6 deprecated-compat names,
    see the "Alias honesty" section). If this count changes, the doc (and
    this test) need a deliberate update, not a silent divergence."""
    if not _ALIASES:
        pytest.skip(_SKIP_REASON_NO_ALIASES)
    assert len(_ALIASES) == 8, (
        f"Expected 8 vllm-local aliases per .claude/rules/50-llm.md, found "
        f"{len(_ALIASES)}: {_ALIASES}. Update the doc's alias table if this "
        f"is an intentional change."
    )
