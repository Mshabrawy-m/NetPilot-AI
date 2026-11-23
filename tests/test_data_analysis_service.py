"""Pure Data Analysis computations: no database, Streamlit or rendering."""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest

from analytics import anomaly_detection, data_quality
from services import data_analysis_service as das

BASE = dt.datetime(2026, 1, 1, 12, 0, 0)


def make_frame(values, *, step_s=60, start=BASE, source="live", ids=None):
    timestamps = [start + dt.timedelta(seconds=i * step_s)
                  for i in range(len(values))]
    return pd.DataFrame({
        "sample_id": ids or list(range(1, len(values) + 1)),
        "collected_at": pd.to_datetime(timestamps),
        "value": [float(v) if v is not None else float("nan") for v in values],
        "unit": "ms",
        "data_source": [source] * len(values),
        "imputed": [False] * len(values),
    })


def test_prepare_frame_dedupes_resamples_and_bounds_chart_rows():
    frame = make_frame(range(6000))
    result = das.prepare_frame(frame, resample="raw", max_points=500)

    assert result.info["input_rows"] == 6000
    assert result.info["deduped_rows"] == 6000
    assert result.info["plot_rows"] == 500
    assert result.info["downsampled"] is True
    assert result.info["downsample_step"] == 12
    assert len(result.frame) == 6000
    assert result.plot["value"].iloc[0] == 0.0
    assert result.plot["value"].iloc[-1] == 5999.0

    grouped = das.prepare_frame(frame, resample="5min", max_points=5000)
    assert 0 < len(grouped.frame) < len(frame)
    assert grouped.info["resample"] == "5min"


def test_prepare_frame_dedupes_exact_timestamps():
    frame = make_frame([1, 2], ids=[1, 2])
    duplicate = frame.iloc[[1]].assign(value=9.0)
    deduped = das.prepare_frame(
        pd.concat([frame, duplicate], ignore_index=True)).source
    assert len(deduped) == 2
    assert deduped["value"].tolist() == [1.0, 9.0]


def test_summaries_and_quality_report_real_gates():
    frame = make_frame([10 + (i % 7) for i in range(40)])
    summaries = das.summarize(frame)
    assert summaries["stats"]["available"] is True
    assert summaries["histogram"]["available"] is True
    assert summaries["profile"]["available"] is False  # under six UTC hours

    quality = das.quality_report(
        frame, device_name="R1", metric="ping.avg_rtt_ms",
        interface_name=None, data_source="live", unit="ms",
        expected_interval_seconds=60.0, now=BASE + dt.timedelta(hours=1))
    assert quality.ready(data_quality.ANALYSIS) is True
    assert quality.ready(data_quality.DETECTION) is True
    assert quality.n_rows == 40
    assert quality.gap_count == 0
    assert das.gap_report(quality)


def test_rolling_band_is_trailing_and_never_looks_ahead():
    values = [float(i) for i in range(30)]
    frame = make_frame(values)
    band = das.rolling_band(frame, window=10, sigma=2.0)
    assert len(band) == len(frame)
    assert band["rolling_mean"].notna().sum() == 26

    for row in (10, 20, 29):
        expected = sum(values[row - 9:row + 1]) / 10
        assert band.loc[row, "rolling_mean"] == pytest.approx(expected)

    changed = frame.copy()
    changed.loc[29, "value"] = 1000.0
    band_after = das.rolling_band(changed, window=10, sigma=2.0)
    assert band_after.loc[28, "rolling_mean"] == band.loc[28, "rolling_mean"]
    assert band_after.loc[29, "upper"] > band.loc[29, "upper"]


def test_anomaly_report_uses_real_detectors_and_keeps_explanations():
    values = [10.0 + (i % 3) for i in range(80)]
    values[55] = 180.0
    frame = make_frame(values)
    quality = das.quality_report(
        frame, device_name="R1", metric="ping.avg_rtt_ms",
        interface_name=None, data_source="live", unit="ms",
        expected_interval_seconds=60.0, now=BASE + dt.timedelta(hours=2))
    anomalies, notes = das.anomaly_report(frame, quality=quality,
                                          device_name="R1",
                                          metric="ping.avg_rtt_ms",
                                          interface_name=None)
    assert set(anomalies.columns) == set(das.ANOMALY_COLUMNS)
    assert not anomalies.empty
    assert set(anomalies["detector"]).issubset(set(
        anomaly_detection.DEFAULT_DETECTORS))
    assert anomalies["explanation"].str.len().gt(0).all()
    assert "prepared sample(s)" in notes[0]


def test_anomaly_report_refuses_when_quality_gate_blocks():
    frame = make_frame([1, 2, 3])
    quality = das.quality_report(
        frame, device_name="R1", metric="m", interface_name=None,
        data_source="live", unit="ms", expected_interval_seconds=60.0,
        now=BASE)
    anomalies, notes = das.anomaly_report(frame, quality=quality,
                                          device_name="R1", metric="m",
                                          interface_name=None)
    assert anomalies.empty
    assert notes[0].startswith("Anomaly detection skipped:")


def test_correlations_use_aligned_pairs_and_report_refusals():
    primary = make_frame([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14])
    related = make_frame([2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28])
    rows = das.correlation_report("a", primary, {"b": related})
    assert len(rows) == 1
    assert rows[0]["available"] is True
    assert rows[0]["r"] == pytest.approx(1.0)
    assert rows[0]["n"] == 14

    short = das.correlation_report("a", primary.iloc[:4],
                                   {"b": related.iloc[:4]})
    assert short[0]["available"] is False
    assert "aligned" in short[0]["reason"]


def test_counter_report_and_exports_cover_all_computed_tables():
    values = [100, 160, 220, 280, 340, 400]
    frame = make_frame(values, step_s=60)
    frame["unit"] = "bytes"
    result = das.analyze(
        frame=frame, device_name="R1", metric="if.rx_bytes",
        interface_name="eth0", data_source="live", unit="bytes",
        expected_interval_seconds=60.0, now=BASE + dt.timedelta(hours=1))
    assert result.counter is not None
    assert result.counter["rate_metric"] == "if.rx_bps"

    exports = das.export_frames(result)
    assert {"series.csv", "summary.csv", "quality.csv", "anomalies.csv",
            "derived_rates.csv"}.issubset(exports)
    assert not exports["series.csv"].empty
    assert not exports["quality.csv"].empty
    assert math.isfinite(exports["series.csv"]["value"].astype(float).sum())
