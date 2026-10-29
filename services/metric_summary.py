"""Formatting helpers for metric summaries."""
from __future__ import annotations

from collections.abc import Iterable
import math


def summarize_link_state(values: Iterable[float | None]) -> dict[str, int | str]:
    """Summarize operstate's numeric encoding without treating it as a scale."""
    rows = list(values)
    available = [float(value) for value in rows
                 if value is not None and not math.isnan(float(value))]
    latest_value = available[-1] if available else None
    return {
        "latest": "Up" if latest_value == 1 else "Down" if latest_value == 0 else "Unknown",
        "up": sum(value == 1 for value in available),
        "down": sum(value == 0 for value in available),
        "unknown": sum(value not in (0, 1) for value in available),
        "unavailable": len(rows) - len(available),
    }
