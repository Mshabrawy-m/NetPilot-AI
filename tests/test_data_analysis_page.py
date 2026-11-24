"""AppTest coverage for the read-only Data Analysis page."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from database import repository
from database.connection import init_db

ROOT = Path(__file__).resolve().parents[1]
PAGE = "app_pages/7_Data_Analysis.py"
BASE = dt.datetime(2026, 3, 1, 0, 0, 0)


def _seed_series(session) -> None:
    for index in range(60):
        stamp = BASE + dt.timedelta(minutes=index)
        repository.add_metric_sample(
            session, "R1", "ping.avg_rtt_ms", 10.0 + (index % 7),
            unit="ms", data_source="live", collected_at=stamp)
        repository.add_metric_sample(
            session, "R1", "ping.loss_pct", float(index % 3),
            unit="%", data_source="live", collected_at=stamp)
        repository.add_metric_sample(
            session, "R1", "if.rx_bytes", 1000.0 + index * 60,
            unit="bytes", interface_name="eth0", data_source="live",
            collected_at=stamp)
        repository.add_metric_sample(
            session, "R1", "if.rx_bytes", 2000.0 + index * 30,
            unit="bytes", interface_name="eth1", data_source="live",
            collected_at=stamp)
    session.commit()


def _run(db_url):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / PAGE), default_timeout=180)
    at.run()
    return at


def _texts(at) -> str:
    values: list[str] = []
    for name in ("markdown", "info", "warning", "error", "caption",
                 "title", "header", "subheader", "text", "metric"):
        for element in getattr(at, name, []):
            value = getattr(element, "value", None)
            if value is not None:
                values.append(str(value))
    return "\n".join(values)


def _select_metric(at, metric: str, unit: str) -> None:
    options = sorted({("if.rx_bytes", "bytes"), ("ping.avg_rtt_ms", "ms"),
                      ("ping.loss_pct", "%")})
    box = next(next_box for next_box in at.selectbox
               if (next_box.label or "") == "Metric")
    box.select(options.index((metric, unit)))
    at.run()


def test_page_reports_empty_state_without_charts(db_url, monkeypatch):
    import streamlit as st

    st.cache_data.clear()
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    at = _run(db_url)
    assert not [str(exc.value) for exc in at.exception]
    assert "No measurements are stored yet" in _texts(at)
    assert len(at.get("plotly_chart")) == 0


def test_page_renders_real_analysis_and_exports(db_url, monkeypatch, session):
    import streamlit as st

    st.cache_data.clear()
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    init_db()
    _seed_series(session)

    at = _run(db_url)
    _select_metric(at, "ping.avg_rtt_ms", "ms")
    exceptions = [str(exc.value) for exc in at.exception]
    assert not exceptions, exceptions[0][:800]

    text = _texts(at)
    assert "R1 · (device-level) · ping.avg_rtt_ms" in text
    assert "Readiness gates" in text
    assert "Summary statistics" in text
    assert "Rolling band and anomalies" in text
    assert len(at.get("plotly_chart")) >= 3
    assert len(at.dataframe) >= 2
    downloads = [button.label or "" for button in at.get("download_button")]
    assert {"Series", "Summary", "Quality", "Anomalies"}.issubset(downloads)


def test_page_honours_range_resample_and_correlation_controls(
        db_url, monkeypatch, session):
    import streamlit as st

    st.cache_data.clear()
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    init_db()
    _seed_series(session)

    at = _run(db_url)
    _select_metric(at, "ping.avg_rtt_ms", "ms")
    resample = next(box for box in at.selectbox
                    if (box.label or "") == "Resample")
    resample.select("5min")
    at.run()
    assert not [str(exc.value) for exc in at.exception]

    related = next(box for box in at.multiselect
                   if (box.label or "") == "Compare with metric(s)")
    related.select("ping.loss_pct")
    at.run()
    assert not [str(exc.value) for exc in at.exception]
    text = _texts(at)
    assert "ping.avg_rtt_ms ↔ ping.loss_pct" in text

    time_range = next(box for box in at.selectbox
                      if (box.label or "") == "Time range")
    time_range.select("All stored history")
    at.run()
    assert not [str(exc.value) for exc in at.exception]
    chosen = next(box for box in at.selectbox
                  if (box.label or "") == "Time range")
    assert chosen.value == "All stored history"


@pytest.mark.parametrize("mode", ["demo", "simulated"])
def test_page_renders_in_demo_and_simulated_modes(
        mode, tmp_path, monkeypatch):
    import streamlit as st

    st.cache_data.clear()
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite:///{(tmp_path / f'{mode}.db').as_posix()}")
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "true" if mode == "demo" else "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    if mode == "simulated":
        init_db()

    at = _run(f"sqlite:///{(tmp_path / f'{mode}.db').as_posix()}")
    exceptions = [str(exc.value) for exc in at.exception]
    assert not exceptions, exceptions[0][:800]
    assert "Data Analysis" in _texts(at)
