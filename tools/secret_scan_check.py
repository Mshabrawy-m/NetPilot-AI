"""Secret hygiene scan (v2 Slice 8) - the single source of truth for CI.

Flags anything that looks like a real credential landing in the tree:
  * hard-coded Groq keys (``gsk_`` + 20+ alphanumerics)
  * long quoted literals assigned to api_key/token/password names

The git-ignored local ``.env``/``.env.local`` config files are skipped by
name (they legitimately hold a developer key and are never in a CI checkout
- the workflow gates a *committed* env file separately via ``git ls-files``).
``tests/`` is skipped: it deliberately contains obviously-fake sentinels
(e.g. ``gsk_SENTINEL_SHOULD_NEVER_BE_PERSISTED``) to assert the code never
persists a real key. Exits non-zero on any hit so it can gate a pipeline.
Run: python tools/secret_scan_check.py
"""
from __future__ import annotations

import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache",
              "tests", "data"}
# A real Groq key: 'gsk_' then a long run of alphanumerics (no underscores).
_GROQ = re.compile(r"gsk_[A-Za-z0-9]{20,}")
# name = "…32+ chars…" or '…32+ chars…' for secret-ish names.
_ASSIGNED = re.compile(
    r"(?:api[_-]?key|token|password)\s*=\s*[\"'][A-Za-z0-9+/=_-]{32,}[\"']")
# Local, git-ignored config files (never in a CI checkout; a *committed* one
# is caught by the workflow's git gate, not here).
_LOCAL_ENV = {".env", ".env.local"}


def _scan():
    hits = []
    for root, dirs, files in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), _ROOT)
            rel_posix = rel.replace("\\", "/")
            if rel_posix in _LOCAL_ENV:
                continue
            path = os.path.join(root, name)
            try:
                text = io.open(path, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if _GROQ.search(line) or (name.endswith(".py")
                                          and _ASSIGNED.search(line)):
                    hits.append((rel_posix, i, line.strip()[:80]))
    return hits


def main() -> int:
    hits = _scan()
    if hits:
        print("Secret scan FAILED:")
        for path, line, snippet in hits:
            print(f"  {path}:{line}: {snippet}")
        return 1
    print("Secret scan clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
