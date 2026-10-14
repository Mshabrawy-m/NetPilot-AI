"""Shared helpers for the read-only evidence tools."""
from __future__ import annotations

import datetime as dt


def truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Bound a content string; return (text, was_truncated)."""
    text = text or ""
    if len(text.encode("utf-8")) <= max_bytes:
        return text, False
    clipped = text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    return clipped + "…[truncated]", True


def make_record(*, kind: str, tool: str, content: str, data_source: str,
                device_name: str | None = None,
                observed_at: dt.datetime | None = None,
                unit: str | None = None,
                source_ref: list | None = None,
                stale: bool = False, missing: bool = False,
                max_bytes: int = 8192) -> dict:
    """Build one bounded evidence record with provenance fields.

    ``uid`` is assigned later by the aggregator (stable 'ev-N' reference the
    diagnosis/plan can cite)."""
    body, _ = truncate(content, max_bytes)
    return {
        "kind": kind,
        "tool": tool,
        "device_name": device_name,
        "data_source": data_source,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "unit": unit,
        "content": body,
        "source_ref": source_ref or [],
        "stale": bool(stale),
        "missing": bool(missing),
    }


def is_stale(observed_at: dt.datetime | None, now: dt.datetime,
             max_age_seconds: float) -> bool:
    """A sample older than ``max_age_seconds`` (or absent) is stale."""
    if observed_at is None:
        return True
    ref = now if observed_at.tzinfo else now.replace(tzinfo=None)
    return (ref - observed_at).total_seconds() > max_age_seconds
