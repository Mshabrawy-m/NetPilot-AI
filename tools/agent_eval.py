"""Agent change-request evaluation CLI (backlog item B).

Compares:

* **A** - LLM only;
* **B** - LLM + deterministic Verifier;
* **C** - LLM + Verifier + Digital Twin.

Offline mode (default) replays the committed cache and needs no key, network
or provider call. Live mode records one proposal per intent through the
existing ``GroqAssistant`` and writes the cache; it never prints a credential.

The provider client is constructed here, outside ``evaluation/``, because the
B4 isolation gate forbids a provider constructor inside the evaluation
package.

Usage
-----
    python tools/agent_eval.py --mode offline
    python tools/agent_eval.py --mode live --dotenv .env --no-omniroute `
        --model openai/gpt-oss-120b

Exit codes
----------
    0  the evaluation completed and the outputs were written
    2  the dataset, cache or live provider setup was unusable
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.groq_client import GroqAssistant
from evaluation.agent_eval import main

#: Settings a live recording may read from an explicit .env file.
_DOTENV_KEYS = frozenset({
    "GROQ_API_KEY", "GROQ_MODEL", "GROQ_TIMEOUT", "GROQ_MAX_RETRIES",
    "OMNIROUTE_BASE_URL",
})


def _apply_dotenv(path: str | Path) -> None:
    """Load selected settings from a .env file without printing values."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f".env file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key in _DOTENV_KEYS:
            os.environ[key] = value.strip().strip('"').strip("'")


def assistant_factory(args: argparse.Namespace) -> GroqAssistant:
    """Build the live provider client after applying explicit CLI overrides."""
    if args.dotenv:
        _apply_dotenv(args.dotenv)
    if args.no_omniroute:
        os.environ.pop("OMNIROUTE_BASE_URL", None)
    if args.model:
        os.environ["GROQ_MODEL"] = args.model
    return GroqAssistant()


if __name__ == "__main__":
    raise SystemExit(main(assistant_factory=assistant_factory))
