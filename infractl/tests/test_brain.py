"""Unit tests for infractl.brain client, key rotation, and self-healing evaluator."""
import time
from unittest.mock import AsyncMock

import pytest

from infractl.brain.client import (
    BrainQuotaExceededError,
    GeminiBrainClient,
    KeyState,
)
from infractl.brain.evaluator import BrainEvaluator
from infractl.core.ledger import Ledger
from infractl.settings import Settings


def _clean_settings(**kwargs):
    defaults = {
        "INFRA_GEMINI_API_KEY_1": "",
        "INFRA_GEMINI_API_KEY_2": "",
        "INFRA_GEMINI_API_KEY_3": "",
        "INFRA_GEMINI_API_KEY_4": "",
        "INFRA_GEMINI_API_KEY_5": "",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def test_key_state_display():
    ks = KeyState(key="fake-test-key-not-a-real-credential-0000TEST")
    assert ks.display_name == "fake-tes...TEST"
    assert ks.status == "active"


def test_key_rotation_selection():
    settings = _clean_settings(
        INFRA_GEMINI_API_KEYS="key_alpha,key_beta,key_gamma",
        INFRA_GEMINI_MODEL="gemini-3.8-flash",
    )
    client = GeminiBrainClient(settings)
    assert len(client.key_states) == 3

    # Pick first key
    k1 = client.get_next_key()
    assert k1.key == "key_alpha"
    k1.last_used = time.time()

    # Next pick should be key_beta (least recently used)
    k2 = client.get_next_key()
    assert k2.key == "key_beta"
    k2.last_used = time.time()

    # Next pick should be key_gamma
    k3 = client.get_next_key()
    assert k3.key == "key_gamma"


def test_key_rotation_with_rate_limiting():
    settings = _clean_settings(
        INFRA_GEMINI_API_KEYS="key_alpha,key_beta",
        INFRA_GEMINI_MODEL="gemini-3.8-flash",
    )
    client = GeminiBrainClient(settings)

    # Rate limit key_alpha
    client.key_states["key_alpha"].status = "rate_limited"
    client.key_states["key_alpha"].rate_limited_until = time.time() + 100

    # Only key_beta should be available
    k = client.get_next_key()
    assert k.key == "key_beta"

    # Rate limit key_beta too
    client.key_states["key_beta"].status = "rate_limited"
    client.key_states["key_beta"].rate_limited_until = time.time() + 100

    # No keys available
    assert client.get_next_key() is None


def test_key_recovery_after_backoff():
    settings = _clean_settings(
        INFRA_GEMINI_API_KEYS="key_alpha",
        INFRA_GEMINI_MODEL="gemini-3.8-flash",
    )
    client = GeminiBrainClient(settings)

    # Key was rate limited in the past
    client.key_states["key_alpha"].status = "rate_limited"
    client.key_states["key_alpha"].rate_limited_until = time.time() - 10

    # Should be auto-restored
    k = client.get_next_key()
    assert k is not None
    assert k.status == "active"


@pytest.mark.asyncio
async def test_quota_enforcement(tmp_path):
    db_path = tmp_path / "test_ledger.db"
    ledger = Ledger(db_path)
    settings = _clean_settings(
        INFRA_GEMINI_API_KEYS="key_alpha",
        INFRA_BRAIN_MAX_CALLS_PER_DAY=2,
    )
    client = GeminiBrainClient(settings, ledger)

    # Record 2 calls
    ledger.record_brain_call("test1", "gemini-3.8-flash")
    ledger.record_brain_call("test2", "gemini-3.8-flash")

    # Next call must raise BrainQuotaExceededError
    with pytest.raises(BrainQuotaExceededError):
        await client.generate_content("hello")


@pytest.mark.asyncio
async def test_brain_evaluator_records_improvement(tmp_path):
    db_path = tmp_path / "test_ledger.db"
    ledger = Ledger(db_path)
    settings = _clean_settings(
        INFRA_GEMINI_API_KEYS="key_alpha",
        INFRA_GEMINI_MODEL="gemini-3.8-flash",
    )
    mock_client = AsyncMock()
    mock_client.generate_content.return_value = (
        '{"summary": "All systems nominal", "anomaly_detected": false, '
        '"root_cause": "none", "recommended_action": "none", '
        '"optimization_note": "Prefix cache hit rate stable"}'
    )

    evaluator = BrainEvaluator(settings, ledger, mock_client)
    res = await evaluator.evaluate_system_health()

    assert res["ok"] is True
    assert res["diagnosis"]["summary"] == "All systems nominal"

    # Verify improvement ledger has the recorded row
    rows = ledger.con.execute("SELECT * FROM improvement_ledger").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "All systems nominal"
    assert rows[0]["source"] == "gemini_brain"
