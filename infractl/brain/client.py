"""Direct Gemini Brain Client with multi-key rotation, 429 backoff, and model auto-resolution.
Directly interacts with Google AI Studio API (never through Bifrost).
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

import httpx

from infractl.core.ledger import Ledger
from infractl.settings import Settings

logger = logging.getLogger("infractl.brain")

BASE_API_URL = "https://generativelanguage.googleapis.com/v1beta"
FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]
DEFAULT_BACKOFF_S = 60.0


class BrainError(RuntimeError):
    pass


class BrainQuotaExceededError(BrainError):
    pass


class NoActiveKeysError(BrainError):
    pass


@dataclasses.dataclass
class KeyState:
    key: str
    status: str = "active"  # "active" | "rate_limited" | "disabled"
    rate_limited_until: float = 0.0
    failure_count: int = 0
    success_count: int = 0
    last_used: float = 0.0
    last_error: str = ""

    @property
    def display_name(self) -> str:
        if len(self.key) <= 12:
            return self.key
        return f"{self.key[:8]}...{self.key[-4:]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.display_name,
            "status": self.status,
            "rate_limited_until": self.rate_limited_until,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_used": self.last_used,
            "last_error": self.last_error,
        }


class GeminiBrainClient:
    """Client for the autonomous infra brain that manages rotation across multiple Google AI keys."""

    def __init__(self, settings: Settings, ledger: Ledger | None = None):
        self.settings = settings
        self.ledger = ledger
        self.key_states: dict[str, KeyState] = {
            k: KeyState(key=k) for k in settings.gemini_keys
        }
        self.preferred_model = settings.infra_gemini_model or "gemini-3.8-flash"

    def refresh_keys(self, new_keys: list[str]) -> None:
        """Add any newly configured keys without losing existing rotation state."""
        for k in new_keys:
            if k not in self.key_states:
                self.key_states[k] = KeyState(key=k)

    def get_next_key(self) -> KeyState | None:
        now = time.time()
        # Recover expired rate-limited keys
        for ks in self.key_states.values():
            if ks.status == "rate_limited" and now >= ks.rate_limited_until:
                logger.info("Restoring key %s from rate_limited to active", ks.display_name)
                ks.status = "active"
                ks.rate_limited_until = 0.0

        available = [ks for ks in self.key_states.values() if ks.status == "active"]
        if not available:
            return None
        # Pick least recently used active key
        return min(available, key=lambda ks: ks.last_used)

    def get_pool_status(self) -> list[dict[str, Any]]:
        return [ks.to_dict() for ks in self.key_states.values()]

    async def list_available_models(self) -> list[dict[str, Any]]:
        """Queries Google AI Studio for active models that support generateContent."""
        key_state = self.get_next_key()
        if not key_state:
            raise NoActiveKeysError("No active Gemini keys available for list_models")

        url = f"{BASE_API_URL}/models?key={key_state.key}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 401:
                key_state.status = "disabled"
                key_state.last_error = "401 UNAUTHENTICATED"
                raise BrainError(f"Key {key_state.display_name} returned 401 UNAUTHENTICATED")
            resp.raise_for_status()
            data = resp.json()
            models = data.get("models", [])
            valid = []
            for m in models:
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    name = m.get("name", "").replace("models/", "")
                    valid.append({
                        "name": name,
                        "display_name": m.get("displayName", ""),
                        "version": m.get("version", ""),
                        "input_limit": m.get("inputTokenLimit"),
                        "output_limit": m.get("outputTokenLimit"),
                    })
            return valid

    async def generate_content(
        self,
        prompt: str,
        system_instruction: str = "",
        model: str | None = None,
        purpose: str = "evaluation",
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
    ) -> str:
        """Generates content using the key pool with rotation, 429 backoff, and model fallback."""
        if self.ledger:
            used_today = self.ledger.brain_calls_today()
            max_calls = self.settings.infra_brain_max_calls_per_day
            if used_today >= max_calls:
                raise BrainQuotaExceededError(
                    f"Daily brain call quota reached ({used_today}/{max_calls})"
                )

        candidate_models = []
        target_model = model or self.preferred_model
        if target_model:
            candidate_models.append(target_model)
        for fb in FALLBACK_MODELS:
            if fb not in candidate_models:
                candidate_models.append(fb)

        attempts_left = max(len(self.key_states) * 2, 3)
        last_error = "Unknown error"

        while attempts_left > 0:
            attempts_left -= 1
            key_state = self.get_next_key()
            if not key_state:
                raise NoActiveKeysError(
                    f"All Gemini keys exhausted or rate-limited. Last error: {last_error}"
                )

            key_state.last_used = time.time()
            current_model = candidate_models[0]

            url = f"{BASE_API_URL}/models/{current_model}:generateContent?key={key_state.key}"
            payload: dict[str, Any] = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_output_tokens,
                },
            }
            if system_instruction:
                payload["systemInstruction"] = {
                    "parts": [{"text": system_instruction}]
                }

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(url, json=payload)

                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise BrainError("Gemini returned empty candidate response")
                    text_parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in text_parts).strip()
                    key_state.success_count += 1
                    key_state.last_error = ""

                    if self.ledger:
                        self.ledger.record_brain_call(purpose, current_model)
                    return text

                if resp.status_code == 401:
                    logger.warning("Gemini key %s returned 401 — disabling", key_state.display_name)
                    key_state.status = "disabled"
                    key_state.last_error = "401 UNAUTHENTICATED"
                    key_state.failure_count += 1
                    last_error = f"{key_state.display_name}: 401 Unauthenticated"
                    continue

                if resp.status_code == 429:
                    logger.warning("Gemini key %s hit 429 rate limit — backing off %ss", key_state.display_name, DEFAULT_BACKOFF_S)
                    key_state.status = "rate_limited"
                    key_state.rate_limited_until = time.time() + DEFAULT_BACKOFF_S
                    key_state.last_error = "429 Too Many Requests"
                    key_state.failure_count += 1
                    last_error = f"{key_state.display_name}: 429 Rate limited"
                    continue

                if resp.status_code in (404, 410):
                    # Model deprecated or not found, advance to next candidate model
                    err_msg = resp.text[:200]
                    logger.warning("Model %s returned %s (%s) — falling back", current_model, resp.status_code, err_msg)
                    if len(candidate_models) > 1:
                        candidate_models.pop(0)
                        last_error = f"Model {current_model} returned {resp.status_code}"
                        continue

                err_text = resp.text[:300]
                key_state.failure_count += 1
                key_state.last_error = f"HTTP {resp.status_code}: {err_text}"
                last_error = key_state.last_error
                logger.error("Gemini API error (%s): %s", resp.status_code, err_text)

            except httpx.RequestError as exc:
                key_state.failure_count += 1
                key_state.last_error = str(exc)
                last_error = str(exc)
                logger.warning("HTTP connection error to Gemini API on %s: %s", key_state.display_name, exc)

        raise BrainError(f"Failed to generate content after retries. Last error: {last_error}")
