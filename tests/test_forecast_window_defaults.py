"""Forecasting defaults must match the window the page actually reads.

The reported live shape was a series with 200+ stored points that still opened
on a "10 point" selection: ranking counted all history, while readiness counted
only the selected 24 h window. This seeds exactly that shape plus a series whose
raw and rollup counts overlap (sum 40, usable 20), then asserts:

* the inventory exposes ``usable_points = max(raw, rollup)`` and does not call
  the rollup-inflated series forecastable;
* ranking puts the forecastable ping series first even though the inflated
  series has a larger raw+rollup total and a non-placeholder traceroute hop has
  more usable points;
* the page chooses a window/cadence that gives the selected series at least
  the 30-point forecast floor.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _texts(at) -> list[str]:
    values: list[str] = []
    for name in ("markdown", "warning", "info", "error", "success",
                 "caption", "title", "header", "subheader", "text"):
        for element in getattr(at, name, []):
            value = getattr(element, "value", None)
            if value is not None:
                values.append(str(value))
    return values


def _selectbox(at, key: str):
    return next(box for box in at.selectbox if box.key == key)


def _seed_bursty_and_inflated() -> None:
    """10 points in 24 h / 30 in 7 d, plus a raw+rollup overlap series."""
    from database.connection import session_scope
    from database.models import MetricDailyRollup, MetricSample, db_utcnow

    now = db_utcnow()
    # 10 recent points (0..18 h ago, every 2 h) + 20 older points (24..100 h
    # ago, every 4 h): 24 h sees 10, 72 h sees 22, 7 d sees all 30.
    recent = [now - dt.timedelta(hours=2 * i) for i in range(10)]
    older = [now - dt.timedelta(hours=24 + 4 * i) for i in range(20)]
    with session_scope() as session:
        session.add_all([
            MetricSample(
                device_name="bursty", metric="ping.avg_rtt_ms",
                value=10.0 + i * 0.1, unit="ms", data_source="live",
                collected_at=stamp)
            for i, stamp in enumerate(sorted(recent + older))
        ])
        # A non-placeholder traceroute hop with twice the usable points: the
        # ping metric must still win the metric preference.
        session.add_all([
            MetricSample(
                device_name="path-hop", metric="traceroute.hop_1_rtt_ms",
                value=20.0 + i * 0.1, unit="ms", data_source="live",
                collected_at=now - dt.timedelta(minutes=30 * i))
            for i in range(60)
        ])

    # 20 raw samples and 20 daily rollups on the SAME days: raw+rollup = 40,
    # but the exact series has only 20 usable points on either cadence.
    today = now.date()
    days = [today - dt.timedelta(days=i) for i in range(20)]
    with session_scope() as session:
        session.add_all([
            MetricSample(
                device_name="rollup-inflated", metric="ping.avg_rtt_ms",
                value=50.0 + i * 0.1, unit="ms", data_source="live",
                collected_at=dt.datetime.combine(day, dt.time(12, 0)))
            for i, day in enumerate(days)
        ])
        session.add_all([
            MetricDailyRollup(
                bucket_date=day, device_name="rollup-inflated",
                interface_name=None, interface_key="",
                metric="ping.avg_rtt_ms", data_source="live",
                sample_count=2, avg_value=50.0 + i * 0.1,
                min_value=49.0 + i * 0.1, max_value=51.0 + i * 0.1,
                unit="ms",
                first_seen=dt.datetime.combine(day, dt.time(0, 5)),
                last_seen=dt.datetime.combine(day, dt.time(23, 55)))
            for i, day in enumerate(days)
        ])


def test_ranking_uses_usable_points_not_raw_plus_rollup(db_url):
    _seed_bursty_and_inflated()

    from database.connection import get_session
    from services import forecast_service

    session = get_session()
    try:
        inventory = forecast_service.list_forecast_series(
            session, data_source="live")
        inflated = next(row for row in inventory
                        if row["device_name"] == "rollup-inflated")
        assert inflated["n_raw_samples"] == 20
        assert inflated["n_rollup_days"] == 20
        assert inflated["usable_points"] == 20       # not 40
        assert inflated["total_points"] == 40        # kept only as a raw sum
        assert inflated["forecastable"] is False

        ranked = forecast_service.rank_forecast_series(
            inventory, data_source="live")
        assert ranked[0]["device_name"] == "bursty"
        assert ranked[0]["usable_points"] == 30
        assert ranked[0]["forecastable"] is True
        # The 60-point traceroute hop is forecastable but still behind the
        # 30-point ping series; the inflated series is not forecastable at all.
        assert ranked[1]["device_name"] == "path-hop"
        assert ranked[1]["usable_points"] == 60
        assert ranked[-1]["device_name"] == "rollup-inflated"
    finally:
        session.close()


def test_page_defaults_to_a_window_with_at_least_30_points(
        db_url, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    _seed_bursty_and_inflated()

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app_pages/9_Forecasting.py"),
                           default_timeout=180)
    at.run()

    exceptions = [str(e.value) for e in at.exception]
    assert not exceptions, exceptions[0][:500]

    # The forecastable 30-point series wins over the inflated 40-total one.
    assert _selectbox(at, "np_forecast_device").value == "bursty"
    assert _selectbox(at, "np_forecast_metric").value == "ping.avg_rtt_ms"
    # 24 h only holds 10 points; the page must widen to the first window that
    # reaches the floor (7 d holds all 30) instead of opening on 10.
    assert _selectbox(at, "np_forecast_range").value == "7 days"

    joined = "\n".join(_texts(at))
    assert "Forecastable:" in joined
    assert "30 usable observation(s)" in joined
    assert "10 usable observation(s)" not in joined
