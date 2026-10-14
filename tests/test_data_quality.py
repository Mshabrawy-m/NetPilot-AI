"""Phase 2 Scope A tests: time-series preparation and data quality.

Fixtures are SYNTHETIC in-memory/database samples used only to verify the
documented behaviour - they are not evidence of real-network data quality.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest

from analytics import data_quality, time_series
from config import get_settings


BASE = dt.datetime(2026, 1, 1, 12, 0, 0)  # naive UTC, Phase 1 convention


def make_frame(values, start=BASE, step_s=60, sources=None, ids=None):
    n = len(values)
    return pd.DataFrame({
        "sample_id": ids or list(range(1, n + 1)),
        "collected_at": pd.to_datetime(
            [start + dt.timedelta(seconds=i * step_s) for i in range(n)]),
        "value": [float("nan") if v is None else float(v) for v in values],
        "unit": "test-unit",
        "data_source": (sources or ["live"] * n),
        "imputed": [False] * n,
    })


# ---------------------------------------------------------------- load/query
def test_load_metric_frame_chronological_and_nullable(session):
    from database import repository
    for i, v in enumerate([10.0, None, 30.0]):
        repository.add_metric_sample(
            session, "R1", "ping.avg_rtt_ms", v, unit="ms",
            data_source="live", collected_at=BASE + dt.timedelta(seconds=i * 60))
    session.commit()
    frame = time_series.load_metric_frame(session, "R1", "ping.avg_rtt_ms",
                                          data_source="live")
    assert len(frame) == 3
    assert frame["collected_at"].is_monotonic_increasing
    # NULL stays missing (NaN) - never replaced by a measured zero.
    assert math.isnan(frame.loc[1, "value"])
    assert frame.loc[0, "value"] == 10.0


def test_load_metric_frame_empty_query(session):
    frame = time_series.load_metric_frame(session, "Nope", "no.such.metric")
    assert frame.empty
    assert list(frame.columns) == time_series.FRAME_COLUMNS


def test_load_metric_frame_bounds_query_size(session):
    from database import repository
    for i in range(50):
        repository.add_metric_sample(
            session, "R1", "m", float(i), collected_at=BASE
            + dt.timedelta(seconds=i * 60))
    session.commit()
    frame = time_series.load_metric_frame(session, "R1", "m", max_points=10)
    assert len(frame) == 10
    assert frame["value"].iloc[-1] == 49.0  # most recent kept


def test_load_metric_frame_source_separation(session):
    from database import repository
    repository.add_metric_sample(session, "R1", "m", 1.0,
                                 data_source="live", collected_at=BASE)
    repository.add_metric_sample(session, "R1", "m", 2.0,
                                 data_source="demo", collected_at=BASE)
    session.commit()
    live = time_series.load_metric_frame(session, "R1", "m",
                                         data_source="live")
    demo = time_series.load_metric_frame(session, "R1", "m",
                                         data_source="demo")
    assert live["value"].tolist() == [1.0]
    assert demo["value"].tolist() == [2.0]


# ------------------------------------------------------------------ dedupe
def test_dedupe_keeps_last_deterministically():
    frame = make_frame([1.0, 2.0, 3.0])
    dup = frame.copy()
    dup.loc[len(dup)] = frame.iloc[0]  # duplicate oldest timestamp, id 1
    dup.loc[len(dup)] = {**frame.iloc[0].to_dict(),
                         "sample_id": 99, "value": 42.0}
    out = time_series.dedupe_frame(dup, keep="last")
    assert len(out) == 3
    # highest sample_id wins at the tied timestamp
    first = out.sort_values("collected_at").iloc[0]
    assert first["value"] == 42.0


def test_median_interval_and_resample():
    frame = make_frame([1.0, 3.0, 5.0, 7.0], step_s=60)
    assert time_series.median_interval_seconds(frame) == 60.0
    resampled = time_series.resample_frame(frame, rule="2min", agg="mean")
    assert resampled["value"].tolist() == [2.0, 6.0]
    # Empty bins are NOT filled.
    sparse = make_frame([1.0, None, None, 9.0], step_s=60)
    r2 = time_series.resample_frame(sparse, rule="2min", agg="mean")
    assert len(r2) == 2  # only bins containing valid values survive


def test_resample_all_null_returns_empty():
    frame = make_frame([None, None, None])
    assert time_series.resample_frame(frame, "5min").empty


# ------------------------------------------------------------- interpolation
def test_interpolate_short_gap_labelled_long_gap_untouched():
    # step 60s: hole at idx 2 (bracketed by 120 s) and idxs 5-7 (bracketed
    # by 240 s, over the 180 s limit -> must stay missing)
    values = [10.0, 20.0, None, 40.0, 50.0, None, None, None, 999.0]
    frame = make_frame(values, step_s=60)
    out = time_series.interpolate_short_gaps(frame, max_gap_seconds=180)
    # short bracketed gap (20 -> 40): time-linear fill, labelled imputed
    assert out.loc[2, "value"] == pytest.approx(30.0)
    assert bool(out.loc[2, "imputed"]) is True
    # long bracketed gap (50 @240s -> 999 @480s): never drawn across
    for i in (5, 6, 7):
        assert math.isnan(out.loc[i, "value"])
        assert bool(out.loc[i, "imputed"]) is False
    # real observations are never marked imputed
    assert bool(out.loc[8, "imputed"]) is False


def test_interpolate_does_not_invent_edge_or_zero_values():
    frame = make_frame([None, 5.0, 5.0, None])
    out = time_series.interpolate_short_gaps(frame, max_gap_seconds=300)
    assert math.isnan(out.loc[0, "value"])      # no left neighbour
    assert math.isnan(out.loc[3, "value"])      # no right neighbour
    # zeros are measured values, not missing
    zframe = make_frame([0.0, None, 0.0])
    zout = time_series.interpolate_short_gaps(zframe, max_gap_seconds=300)
    assert zout.loc[0, "value"] == 0.0 and zout.loc[2, "value"] == 0.0


# ------------------------------------------------------------------ quality
def test_quality_empty_series_not_ready_with_reason():
    q = data_quality.summarize_quality(
        make_frame([]), "R1", "ping.avg_rtt_ms")
    assert q.n_rows == 0
    assert not q.ready(data_quality.ANALYSIS)
    assert any("no samples" in r for r in q.reasons)


def test_quality_short_series_reports_insufficient():
    q = data_quality.summarize_quality(
        make_frame([1.0, 2.0, 3.0]), "R1", "m")
    s = get_settings()
    assert not q.ready(data_quality.FORECAST)
    reasons = " ".join(q.reasons)
    assert "insufficient data" in reasons
    assert str(s.min_points_forecast) in reasons


def test_quality_counts_duplicates_gaps_and_nulls():
    frame = make_frame([1.0, 2.0, None, 4.0, 5.0], step_s=60)
    # punch a 10-minute hole between the 3rd and 4th observations
    shift = pd.Timedelta(minutes=10)
    frame.loc[3, "collected_at"] = frame.loc[3, "collected_at"] + shift
    frame.loc[4, "collected_at"] = frame.loc[4, "collected_at"] + shift
    # exact duplicate of the first row (same timestamp)
    dup = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    q = data_quality.summarize_quality(dup, "R1", "m",
                                       now=BASE + dt.timedelta(minutes=20))
    assert q.n_duplicates == 1
    assert q.n_null_values == 1
    assert q.gap_count >= 1
    # 540 s = the 120 s -> 660 s jump created by the 10-minute hole
    assert q.longest_gap_seconds is not None and q.longest_gap_seconds >= 500


def test_quality_stale_detection():
    frame = make_frame([float(i) for i in range(8)], step_s=60)
    old = data_quality.summarize_quality(
        frame, "R1", "m", now=BASE + dt.timedelta(hours=5),
        stale_after_seconds=600)
    assert old.is_stale and old.stale_seconds > 600
    fresh = data_quality.summarize_quality(
        frame, "R1", "m", now=frame["collected_at"].max()
        + dt.timedelta(seconds=90), stale_after_seconds=600)
    assert not fresh.is_stale


def test_quality_flags_mixed_sources():
    frame = make_frame([1.0, 2.0], sources=["demo", "live"])
    q = data_quality.summarize_quality(frame, "R1", "m")
    assert q.mixed_sources
    assert any("mixes data sources" in r for r in q.reasons)


def test_quality_readiness_matrix():
    good = make_frame([float(i % 5) for i in range(40)], step_s=60)
    q = data_quality.summarize_quality(good, "R1", "m")
    assert q.ready(data_quality.ANALYSIS)
    assert q.ready(data_quality.DETECTION)
    assert q.ready(data_quality.FORECAST)


def test_quality_irregular_sampling_blocks_forecast_only():
    # 40 samples but wildly irregular spacing (CV > 1.0)
    times = [BASE] + [BASE + dt.timedelta(seconds=s) for s in
                      [5, 600, 10, 900, 20, 300, 15, 1200, 8, 700] * 4]
    frame = pd.DataFrame({
        "sample_id": range(1, len(times) + 1),
        "collected_at": pd.to_datetime(sorted(times)),
        "value": [float(i) for i in range(len(times))],
        "unit": "u", "data_source": "live", "imputed": False,
    })
    q = data_quality.summarize_quality(frame, "R1", "m")
    assert q.ready(data_quality.ANALYSIS)      # enough points
    assert not q.ready(data_quality.FORECAST)  # but grid is indefensible
    assert any("irregular" in r for r in q.reasons)
