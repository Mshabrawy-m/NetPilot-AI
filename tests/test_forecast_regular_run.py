"""A bursty live collector must not open Forecasting on an unforecastable span.

The reported live shape was a 24-minute burst of 30-second samples followed by
a single isolated sample hours later. Any time span that covers both has one
enormous inter-observation interval, so the unchanged data-quality gate reports
``interval CV 5.98 > 1.0`` and refuses the run - correctly, because the quick
comparison treats the observations as a regular grid.

This seeds exactly that shape and asserts:

* the pure ``latest_regular_run`` helper returns the newest gap-free run and
  never the trailing one-sample run;
* the service still refuses the full time window (the gate is not weakened) but
  accepts the explicit gap-free run bounds;
* the page defaults to "Most recent regular run" and is forecastable there,
  while choosing "24 hours" still surfaces the honest CV refusal.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

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


def _seed_burst_with_trailing_sample() -> tuple[dt.datetime, dt.datetime]:
    """49 samples 30 s apart, then one isolated sample ~2.4 h later."""
    from database.connection import session_scope
    from database.models import MetricSample, db_utcnow

    now = db_utcnow()
    run_start = now - dt.timedelta(hours=3, minutes=30)
    run_stamps = [run_start + dt.timedelta(seconds=30 * i) for i in range(49)]
    trailing = run_stamps[-1] + dt.timedelta(seconds=8677)
    with session_scope() as session:
        session.add_all([
            MetricSample(
                device_name="burst-node", metric="ping.avg_rtt_ms",
                value=12.0 + (i % 5) * 0.1, unit="ms", data_source="live",
                collected_at=stamp)
            for i, stamp in enumerate(run_stamps)
        ])
        session.add(MetricSample(
            device_name="burst-node", metric="ping.avg_rtt_ms", value=12.5,
            unit="ms", data_source="live", collected_at=trailing))
    return run_stamps[0], run_stamps[-1]


def test_latest_regular_run_skips_trailing_isolated_sample():
    from services.forecast_service import latest_regular_run

    base = dt.datetime(2026, 11, 11, 13, 3, 39)
    stamps = [base + dt.timedelta(seconds=30 * i) for i in range(49)]
    stamps.append(stamps[-1] + dt.timedelta(seconds=8677))
    frame = pd.DataFrame({"collected_at": stamps})

    run = latest_regular_run(frame, 30.0, min_points=30)
    assert run == {"start": base, "end": stamps[48], "n_points": 49}
    # A run shorter than the floor is never returned, even if it is the newest.
    assert latest_regular_run(frame, 30.0, min_points=50) is None
    # No interval => cannot define a gap-free run.
    assert latest_regular_run(frame, None, min_points=1) is None


def test_gate_refuses_full_span_but_accepts_explicit_run(db_url):
    run_start, run_end = _seed_burst_with_trailing_sample()

    from database.connection import get_session
    from services import forecast_service

    session = get_session()
    try:
        run = forecast_service.latest_regular_run_window(
            session, device_name="burst-node", metric="ping.avg_rtt_ms",
            interface_name=None, data_source="live")
        assert run is not None
        assert run["n_points"] == 49
        assert run["start"] == run_start
        assert run["end"] == run_end

        # The full window (no bounds) still hits the irregular-sampling gate.
        full = forecast_service.check_series_forecastability(
            session, device_name="burst-node", metric="ping.avg_rtt_ms",
            interface_name=None, data_source="live",
            device_level_only=True)
        assert full["ready"] is False
        assert any("interval CV" in reason for reason in full["reasons"])

        # Bounding both ends to the run changes the data, never the gate.
        bounded = forecast_service.check_series_forecastability(
            session, device_name="burst-node", metric="ping.avg_rtt_ms",
            interface_name=None, data_source="live",
            since=run_start, until=run_end, device_level_only=True)
        assert bounded["ready"] is True
        assert bounded["n_observations"] == 49
    finally:
        session.close()


def test_page_defaults_to_the_gap_free_run_and_keeps_the_gate(
        db_url, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    _seed_burst_with_trailing_sample()

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app_pages/9_Forecasting.py"),
                           default_timeout=180)
    at.run()

    exceptions = [str(e.value) for e in at.exception]
    assert not exceptions, exceptions[0][:500]

    # Every time span spans the collection gap; the default must fall back to
    # the explicit gap-free run, not open on a refused window.
    assert _selectbox(at, "np_forecast_range").value == "Most recent regular run"
    joined = "\n".join(_texts(at))
    assert "Forecastable:" in joined
    assert "49 usable observation(s)" in joined
    assert "gap-free sample(s)" in joined
    assert "interval CV" not in joined

    # The gate is unchanged: an explicit 24 h span that includes the gap is
    # still refused with the real reason.
    _selectbox(at, "np_forecast_range").select("24 hours")
    at.run()
    assert not [str(e.value) for e in at.exception]
    refused = "\n".join(_texts(at))
    assert "Not forecastable yet" in refused
    assert "interval CV" in refused
    assert "Most recent regular run" in refused      # actionable tip shown
