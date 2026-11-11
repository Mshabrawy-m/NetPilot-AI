"""Forecasting first load must select a usable, non-placeholder series.

The page used to take the alphabetically first device/metric, which on a live
deployment can be a two-sample series (the reported "needs >= 30 non-null
samples, has 2") or a placeholder device label. This seeds one long ping
series, a same-sized traceroute-hop series, several short series and a
100-point placeholder, then asserts the real ranking picks the long ping.

It also drives the below-floor path: selecting a short series must keep the
honest floor message, show the real count, and offer the three best available
series as clickable choices that actually switch the selection.
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


def _seed_series() -> None:
    from database.connection import session_scope
    from database.models import MetricSample, db_utcnow

    now = db_utcnow()

    def add(device: str, metric: str, count: int, start_minutes: int,
            unit: str = "ms") -> None:
        rows = [
            MetricSample(
                device_name=device, metric=metric, value=10.0 + i * 0.1,
                unit=unit, data_source="live",
                collected_at=now - dt.timedelta(
                    minutes=start_minutes - i))
            for i in range(count)
        ]
        with session_scope() as session:
            session.add_all(rows)

    # Long, regular, forecastable ping history (40 points, one minute apart).
    add("core-router", "ping.avg_rtt_ms", 40, start_minutes=40)
    # Same number of points, but a derived traceroute-hop metric: ping wins
    # the tie-break.
    add("hop-node", "traceroute.hop_1_rtt_ms", 40, start_minutes=40)
    # Short series that reproduce the reported below-floor shape.
    add("edge-a", "ping.avg_rtt_ms", 3, start_minutes=3)
    add("edge-b", "ping.avg_rtt_ms", 2, start_minutes=2)
    add("edge-c", "if.in_octets", 2, start_minutes=2, unit="bps")
    # A placeholder label with the most points of all: it must never become
    # the default even though it is the richest series in storage.
    add("target-1", "ping.avg_rtt_ms", 100, start_minutes=100)


def _selectbox(at, key: str):
    return next(box for box in at.selectbox if box.key == key)


def test_forecast_first_load_picks_long_ping_series(db_url, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    _seed_series()

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app_pages/9_Forecasting.py"),
                           default_timeout=180)
    at.run()

    exceptions = [str(e.value) for e in at.exception]
    assert not exceptions, exceptions[0][:500]

    assert _selectbox(at, "np_forecast_device").value == "core-router"
    assert _selectbox(at, "np_forecast_metric").value == "ping.avg_rtt_ms"
    assert _selectbox(at, "np_forecast_scope").value is None
    assert "Forecastable:" in "\n".join(_texts(at))


def test_below_floor_selection_offers_three_clickable_alternatives(
        db_url, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    _seed_series()

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app_pages/9_Forecasting.py"),
                           default_timeout=180)
    at.run()

    # Switch to the three-sample series; the page must stay honest and help.
    _selectbox(at, "np_forecast_device").select("edge-a")
    at.run()
    assert not [str(e.value) for e in at.exception]

    joined = "\n".join(_texts(at))
    assert "Not forecastable yet" in joined
    assert "30" in joined                       # the floor is still stated
    assert "This selection has **3** usable observation(s)" in joined

    suggestions = [button for button in at.button
                   if str(button.key).startswith("np_fc_suggest_")]
    assert len(suggestions) == 3

    # Clicking a suggestion switches all three selectors to that series.
    suggestions[0].click()
    at.run()
    assert not [str(e.value) for e in at.exception]
    assert _selectbox(at, "np_forecast_device").value == "core-router"
    assert _selectbox(at, "np_forecast_metric").value == "ping.avg_rtt_ms"
