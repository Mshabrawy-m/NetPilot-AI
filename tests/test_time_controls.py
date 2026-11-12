"""Shared time-range helpers: smart defaults, bounds, and widget callbacks."""
from __future__ import annotations

import datetime as dt

import streamlit

from ui import time_controls


NOW = dt.datetime(2026, 11, 12, 12, 0, 0)


def test_smart_range_picks_the_smallest_window_that_contains_latest():
    assert time_controls.smart_range_label(
        latest=NOW - dt.timedelta(minutes=20), now=NOW) == "1 hour"
    assert time_controls.smart_range_label(
        latest=NOW - dt.timedelta(hours=4), now=NOW) == "6 hours"
    assert time_controls.smart_range_label(
        latest=NOW - dt.timedelta(hours=20), now=NOW) == "24 hours"
    assert time_controls.smart_range_label(
        latest=NOW - dt.timedelta(days=3), now=NOW) == "7 days"
    assert time_controls.smart_range_label(
        latest=NOW - dt.timedelta(days=30), now=NOW) == time_controls.ALL_HISTORY


def test_smart_range_without_a_sample_keeps_the_documented_default():
    assert time_controls.smart_range_label(latest=None) == time_controls.DEFAULT_RANGE


def test_since_for_all_history_has_no_lower_bound():
    assert time_controls.since_for(time_controls.ALL_HISTORY) is None
    assert time_controls.since_for("1 hour", now=NOW) == NOW - dt.timedelta(hours=1)


def test_ensure_default_only_rewrites_when_the_selection_changes():
    state: dict = {}
    time_controls.ensure_default(state, "np_range", ("R1", "cpu"), "6 hours")
    assert state["np_range"] == "6 hours"

    # A manual override survives a rerun of the same selection.
    state["np_range"] = time_controls.ALL_HISTORY
    time_controls.ensure_default(state, "np_range", ("R1", "cpu"), "6 hours")
    assert state["np_range"] == time_controls.ALL_HISTORY

    # A new selection gets a fresh smart default.
    time_controls.ensure_default(state, "np_range", ("R1", "mem"), "24 hours")
    assert state["np_range"] == "24 hours"


def test_select_all_history_is_a_callback_safe_switch(monkeypatch):
    """Regression: assigning to a widget key after instantiation raises.

    The pages pass this helper as ``on_click`` so the write happens before the
    range widget is re-created.
    """
    monkeypatch.setattr(streamlit, "session_state", {"np_range": "6 hours"})

    time_controls.select_all_history("np_range")

    assert streamlit.session_state["np_range"] == time_controls.ALL_HISTORY
