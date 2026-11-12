"""Shared, pure helpers for the analysis time-range selectors.

Pages used to default every selection to "6 hours", which could hide a stored
series whose newest sample was older than that window. The helpers here pick a
range that actually contains the newest stored observation, or offer the full
history when even the largest fixed window would miss it. They never invent or
extend data - they only choose which stored rows to read.
"""
from __future__ import annotations

import datetime as dt

ALL_HISTORY = "All stored history"

#: Ordered label -> hours. ``None`` means no lower bound (all stored history).
RANGE_OPTIONS: dict[str, int | None] = {
    "1 hour": 1,
    "6 hours": 6,
    "24 hours": 24,
    "7 days": 168,
    ALL_HISTORY: None,
}

DEFAULT_RANGE = "6 hours"


def smart_range_label(*, latest: dt.datetime | None,
                      now: dt.datetime | None = None) -> str:
    """Smallest fixed range that contains ``latest``, else all history.

    ``latest`` is the newest stored observation for the selected series. A
    series whose newest sample is 30 hours old therefore defaults to "7 days"
    (or "All stored history" beyond a week) instead of the old empty 6-hour
    window. A series with no observations keeps the documented default.
    """
    if latest is None:
        return DEFAULT_RANGE
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    try:
        lag_hours = (now - latest).total_seconds() / 3600.0
    except TypeError:  # naive/aware mismatch - never guess a range
        return ALL_HISTORY
    if lag_hours <= 1:
        return "1 hour"
    if lag_hours <= 6:
        return "6 hours"
    if lag_hours <= 24:
        return "24 hours"
    if lag_hours <= 168:
        return "7 days"
    return ALL_HISTORY


def since_for(label: str, *, now: dt.datetime | None = None
              ) -> dt.datetime | None:
    """Lower bound for a range label; ``None`` means all stored history."""
    hours = RANGE_OPTIONS.get(label, RANGE_OPTIONS[DEFAULT_RANGE])
    if hours is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return now - dt.timedelta(hours=hours)


def describe_bounds(earliest: dt.datetime | None,
                    latest: dt.datetime | None) -> str:
    """Human-readable stored-history bounds for empty-state diagnostics."""
    if earliest is None or latest is None:
        return "no stored samples"
    return (f"stored samples run {earliest:%Y-%m-%d %H:%M:%S} → "
            f"{latest:%Y-%m-%d %H:%M:%S} UTC")


def ensure_default(state, key: str, signature, label: str) -> None:
    """Apply a smart default only when the underlying selection changed.

    ``state`` is ``st.session_state`` (duck-typed so this stays import-free).
    A user's explicit range choice survives reruns of the same selection; a
    new device/metric/source gets a fresh default that can actually contain
    its stored samples.
    """
    signature_key = f"_np_range_sig_{key}"
    if state.get(signature_key) != signature or key not in state:
        state[key] = label
        state[signature_key] = signature


def select_all_history(key: str) -> None:
    """Widget callback that switches a range selector to all stored history.

    Streamlit forbids assigning to a widget-backed session key after that
    widget is instantiated in the current run, so pages must pass this as the
    button's ``on_click`` (callbacks run before the rerun) instead of assigning
    inline next to ``st.button``.
    """
    import streamlit as st

    st.session_state[key] = ALL_HISTORY
