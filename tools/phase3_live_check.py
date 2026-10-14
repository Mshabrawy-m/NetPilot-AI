"""Optional LIVE smoke for the Groq integration (manual, network required).

Not part of the automated suite (the suite runs fully offline with mocks).
Run only when a real GROQ_API_KEY is configured in the local .env:

    .venv/Scripts/python tools/phase3_live_check.py

It prints classification outcomes ONLY - the API key is never shown.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai import prompts, schemas
from ai.groq_client import GroqError, GroqAssistant
from config import get_settings


def main() -> int:
    settings = get_settings()
    if not settings.groq_api_key:
        print("GROQ_API_KEY not configured - live check skipped.")
        return 2
    assistant = GroqAssistant(settings)
    print(f"model={assistant.model} timeout={assistant.timeout}s "
          f"retries={assistant.max_retries} available={assistant.available}")

    for text in ("Why can't PC1 reach PC2?",
                 "اعزل شبكة HR عن الخوادم"):
        try:
            intent = assistant.complete_json(
                prompts.INTENT_SYSTEM,
                prompts.user_payload("Classify this request.", {"text": text}),
                schemas.Intent)
            print(f"  input={text!r}\n    -> type={intent.request_type} "
                  f"lang={intent.source_language} targets={intent.targets} "
                  f"risk={intent.risk_level}")
        except GroqError as exc:
            print(f"  input={text!r}\n    -> provider error: {type(exc).__name__}: {exc}")
            return 1
    print("LIVE Groq integration OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
