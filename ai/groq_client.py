"""Groq provider adapter (Phase 3 spec §6).

A single, small choke point for every LLM call - the rest of the app never
touches the SDK directly. Responsibilities:

* read config from ``config.Settings`` (``GROQ_API_KEY`` comes from the
  environment / git-ignored ``.env``; it is **never** hard-coded or logged);
* per-call timeout and **bounded** retries for transient failures;
* explicit rate-limit / quota handling;
* return **schema-validated** objects or raise a typed error - never fabricate.

The transport client is injectable so automated tests can run fully offline
with a mocked response (no real API key or network call required).
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from config import Settings, get_settings


class GroqError(Exception):
    """Base for all provider failures (safe to surface a short message)."""


class MissingGroqKey(GroqError):
    """No API key configured - the assistant reports this and fails safe."""


class GroqRateLimit(GroqError):
    """Rate-limited / quota exhausted (transient - retried, then surfaced)."""


class GroqTimeout(GroqError):
    """Request timed out or connection dropped (transient - retried)."""


class GroqBadResponse(GroqError):
    """Provider returned something we could not parse as JSON."""


class GroqSchemaError(GroqError):
    """Model output did not satisfy the required schema (never fabricated)."""


#: transient classes that are retried (bounded by max_retries)
_TRANSIENT = (GroqRateLimit, GroqTimeout)


def _classify(exc: Exception) -> GroqError:
    """Map any SDK/transport exception to a NetPilot typed error.

    Uses duck-typing (status code / class name) so it works with the real
    Groq SDK *and* with lightweight fakes used in tests.
    """
    name = type(exc).__name__.lower()
    status = getattr(exc, "status_code", None)
    if status == 429 or "ratelimit" in name:
        return GroqRateLimit("Groq rate limit / quota reached")
    if status is not None and 500 <= status < 600:
        return GroqTimeout("Groq server error (transient)")
    if "timeout" in name or "timedout" in name:
        return GroqTimeout("Groq request timed out")
    if "connection" in name:
        return GroqTimeout("Groq connection error")
    if "authentication" in name or status in (401, 403):
        return MissingGroqKey("Groq rejected the configured API key")
    return GroqBadResponse(f"Groq provider error: {type(exc).__name__}")


class GroqAssistant:
    """Thin, safe wrapper around the Groq chat-completions API."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.groq_model
        self.timeout = self.settings.groq_timeout_seconds
        self.max_retries = self.settings.groq_max_retries
        self.api_key = self.settings.groq_api_key
        self._sleeper = sleeper
        self._client = client
        self._client_built = client is not None

    # -- availability -----------------------------------------------------
    @property
    def available(self) -> bool:
        """True when a transport client is usable (injected for tests, or a
        key is configured for the real SDK)."""
        if self._client is not None:
            return True
        return bool(self.api_key) and self._ensure_client() is not None

    def _ensure_client(self) -> Any | None:
        if self._client_built:
            return self._client
        if not self.api_key:
            return None
        try:
            from groq import Groq
            self._client = Groq(
                api_key=self.api_key,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        except Exception as exc:  # pragma: no cover - import/env dependent
            raise GroqBadResponse(f"Groq SDK unavailable: {type(exc).__name__}")
        self._client_built = True
        return self._client

    # -- low-level raw call ----------------------------------------------
    def _raw_chat(self, messages: list[dict]) -> str:
        client = self._ensure_client()
        if client is None:
            raise MissingGroqKey("GROQ_API_KEY is not configured")
        try:
            completion = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
                timeout=self.timeout,
            )
        except GroqError:
            raise
        except Exception as exc:
            raise _classify(exc) from exc
        try:
            return completion.choices[0].message.content or ""
        except (AttributeError, IndexError) as exc:
            raise GroqBadResponse("Empty or malformed completion") from exc

    # -- high-level: JSON + schema validation ----------------------------
    def complete_json(self, system: str, user: str,
                      schema: type[BaseModel]) -> BaseModel:
        """Run one prompt and return an instance of ``schema``.

        Bounded retries on transient errors. Malformed JSON is retried within
        the same budget; schema violations are retried once then raised so a
        later node never sees unchecked output.
        """
        if not self.available and self._client is None:
            raise MissingGroqKey("GROQ_API_KEY is not configured")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        attempts = self.max_retries + 1
        last_error: GroqError | None = None
        for attempt in range(attempts):
            try:
                content = self._raw_chat(messages)
                payload = self._parse_json(content)
                return schema.model_validate(payload)
            except _TRANSIENT as exc:
                last_error = exc
                if attempt < attempts - 1:
                    self._sleeper(min(2.0 * (attempt + 1), 5.0))
                    continue
                raise
            except ValidationError as exc:
                last_error = GroqSchemaError(
                    f"Model output failed schema validation: {type(exc).__name__}")
                if attempt < attempts - 1:
                    continue
                raise last_error
            except GroqBadResponse as exc:
                last_error = exc
                if attempt < attempts - 1:
                    continue
                raise
        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse_json(content: str) -> dict:
        text = (content or "").strip()
        # Tolerate accidental ```json fences; never fabricate on failure.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GroqBadResponse("Model did not return valid JSON") from exc
        if not isinstance(obj, dict):
            raise GroqBadResponse("Model JSON must be an object")
        return obj
