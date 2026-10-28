"""Phase 2 Scope D tests: anomaly detection + event persistence lifecycle.

All fixtures are SYNTHETIC (documented so): they verify detector behaviour
and the event lifecycle - they are NOT evidence of detection performance
on real networks (no labelled ground truth exists for that).
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest

from analytics.anomaly_detection import (
    DEFAULT_DETECTORS,
    detect_frame,
    detect_isolation_forest,
    detect_rolling_mad,
    detect_rolling_zscore,
    detect_seasonal_residual,
)
from services import anomaly_service

BASE = dt.datetime(2026, 1, 1, 12, 0, 0)


def make_frame(values, step_s=60):
    n = len(values)
    return pd.DataFrame({
        "sample_id": list(range(1, n + 1)),
        "collected_at": pd.to_datetime(
            [BASE + dt.timedelta(seconds=i * step_s) for i in range(n)]),
        "value": [None if v is None else float(v) for v in values],
        "unit": "ms", "data_source": "live", "imputed": [False] * n,
    })


NORMAL = [10.0, 12.0, 11.0, 13.0, 10.5, 12.5, 11.5, 13.5, 10.2, 12.2,
          11.2, 13.2, 10.8, 12.8, 11.8, 13.8]          # 16 gentle samples
SPIKE_AT = 13                                          # index of the spike
SPIKE_SERIES = NORMAL[:SPIKE_AT] + [500.0] + NORMAL[SPIKE_AT + 1:]


# ----------------------------------------------------------------- detectors
def test_rolling_mad_flags_spike_not_normal_values():
    frame = make_frame(SPIKE_SERIES)
    points = detect_rolling_mad(
        frame["value"], frame["collected_at"],
        device_name="R1", metric="ping.avg_rtt_ms", window=8, threshold=3.5)
    assert len(points) >= 1
    flagged = [p for p in points if p.timestamp == frame.at[SPIKE_AT, "collected_at"]]
    assert flagged, "the synthetic spike must be flagged"
    p = flagged[0]
    assert p.observed == 500.0
    assert p.baseline is not None and p.baseline < 20.0
    assert p.threshold_upper is not None and p.observed > p.threshold_upper
    assert "rolling median" in p.explanation
    # normal values within the same series are not all flagged
    assert len(points) < len(SPIKE_SERIES) // 2


def test_rolling_zscore_flags_spike():
    frame = make_frame(SPIKE_SERIES)
    points = detect_rolling_zscore(
        frame["value"], frame["collected_at"],
        device_name="R1", metric="m", window=8, threshold=3.0)
    assert any(p.timestamp == frame.at[SPIKE_AT, "collected_at"]
               for p in points)
    assert all("z-score" in p.explanation for p in points)


def test_detectors_abstain_on_insufficient_or_flat_series():
    short = make_frame([10.0, 500.0, 10.0])
    assert detect_rolling_mad(short["value"], short["collected_at"],
                              device_name="R1", metric="m", window=8) == []
    flat = make_frame([7.0] * 30 + [math.nan] + [7.0] * 5)
    # constant history -> zero scale: MAD detector falls back to global
    # scale which is also 0 -> must abstain, not flag everything
    assert detect_rolling_mad(flat["value"], flat["collected_at"],
                              device_name="R1", metric="m", window=8) == []
    # z-score: constant window std -> abstain as well
    assert detect_rolling_zscore(flat["value"], flat["collected_at"],
                                 device_name="R1", metric="m", window=8) == []


def test_missing_samples_never_flagged_never_break_chain():
    values = SPIKE_SERIES.copy()
    values[5] = None
    values[SPIKE_AT - 1] = None
    frame = make_frame(values)
    points = detect_rolling_mad(frame["value"], frame["collected_at"],
                                device_name="R1", metric="m", window=8)
    flagged_ts = {p.timestamp for p in points}
    assert frame.at[5, "collected_at"] not in flagged_ts   # NULL not judged
    assert frame.at[SPIKE_AT, "collected_at"] in flagged_ts  # spike still found


def test_sustained_deviation_flags_multiple_points():
    values = NORMAL + [200.0] * 5
    frame = make_frame(values)
    points = detect_rolling_mad(frame["value"], frame["collected_at"],
                                device_name="R1", metric="m", window=6,
                                threshold=3.0)
    # at least the first sustained point must be flagged before the rolling
    # baseline adapts (documented behaviour of rolling detectors)
    assert len(points) >= 1
    assert points[0].observed == 200.0


def test_trailing_window_no_future_leakage():
    # A baseline computed at time t must be identical whether or not future
    # data exists: truncate after index k and the verdicts <= k unchanged.
    frame_full = make_frame(SPIKE_SERIES)
    frame_cut = make_frame(SPIKE_SERIES[:SPIKE_AT + 2])
    p_full = detect_rolling_mad(frame_full["value"], frame_full["collected_at"],
                                device_name="R1", metric="m", window=8)
    p_cut = detect_rolling_mad(frame_cut["value"], frame_cut["collected_at"],
                               device_name="R1", metric="m", window=8)
    ts = frame_cut.at[SPIKE_AT, "collected_at"]
    a = next(p for p in p_full if p.timestamp == ts)
    b = next(p for p in p_cut if p.timestamp == ts)
    assert a.baseline == b.baseline and a.observed == b.observed


def test_isolation_forest_is_seeded_and_reproducible():
    values = [float(10 + (i % 3)) for i in range(120)]
    values[60] = 900.0
    frame = make_frame(values)
    args = dict(device_name="R1", metric="m", window=40, random_seed=7)
    p1 = detect_isolation_forest(frame["value"], frame["collected_at"], **args)
    p2 = detect_isolation_forest(frame["value"], frame["collected_at"], **args)
    assert [(str(p.timestamp), p.observed) for p in p1] == \
           [(str(p.timestamp), p.observed) for p in p2]
    assert any(p.observed == 900.0 for p in p1)


def test_detector_configuration_changes_results():
    frame = make_frame(SPIKE_SERIES)
    strict = detect_rolling_mad(frame["value"], frame["collected_at"],
                                device_name="R1", metric="m", window=8,
                                threshold=50.0)
    loose = detect_rolling_mad(frame["value"], frame["collected_at"],
                               device_name="R1", metric="m", window=8,
                               threshold=2.0)
    assert len(strict) <= len(loose)
    assert len(loose) >= 1


def test_seasonal_residual_anchors_phase_by_time_on_a_regular_grid():
    # A 3-sample season (interval 1200s, season_hours=1); only the final
    # same-phase outlier deviates. On the regular grid the timestamp-derived
    # cycle position must equal the row's elapsed-time step.
    base = (10.0, 20.0, 30.0)
    vals = [1000.0 if s == 17 else base[s % 3] for s in range(18)]
    series = pd.Series(vals, dtype="float64")
    ts = pd.Series(pd.to_datetime(
        [BASE + dt.timedelta(seconds=s * 1200) for s in range(18)]))
    pts = detect_seasonal_residual(
        series, ts, device_name="R1", metric="m",
        interval_seconds=1200, season_hours=1.0)
    assert len(pts) == 1, "only the same-phase outlier should clear the bar"
    p = pts[0]
    assert p.timestamp == ts.iloc[17]
    assert p.detector_config["phase"] == 2       # 17 % 3, true cycle position
    assert p.detector_config["grid_step"] == 17
    assert p.detector_config["phase_basis"] == "timestamp"
    assert p.detector_version == "1.1"
    assert p.baseline is not None and p.baseline < 40


def test_seasonal_residual_phase_follows_time_not_row_index():
    # Core regression: dropping an interior row shifts every later ROW INDEX,
    # so an index-based phase (i % m) would map the final sample to phase 1.
    # Deriving phase from timestamps keeps it on its true cycle position (2)
    # even after the gap, provided the reset guard is widened for this test.
    base = (10.0, 20.0, 30.0)
    steps = [s for s in range(18) if s != 7]      # 17 rows, one removed
    vals = [1000.0 if s == 17 else base[s % 3] for s in steps]
    series = pd.Series(vals, dtype="float64")
    ts = pd.Series(pd.to_datetime(
        [BASE + dt.timedelta(seconds=s * 1200) for s in steps]))
    pts = detect_seasonal_residual(
        series, ts, device_name="R1", metric="m", interval_seconds=1200,
        season_hours=1.0, max_gap_intervals=5.0)
    assert pts
    p = [q for q in pts if q.timestamp == ts.iloc[-1]][0]
    assert p.detector_config["grid_step"] == 17, "time step, not row index 16"
    assert p.detector_config["phase"] == 2, "time-based phase, not (16 % 3 = 1)"


def test_seasonal_residual_skips_samples_off_the_assumed_grid():
    # A single resampled timestamp that lands half an interval off the grid is
    # not trusted to define a phase, so the deviating sample is skipped and the
    # detector emits no verdict rather than judging it against a bogus baseline.
    base = (10.0, 20.0, 30.0)
    vals = [1000.0 if s == 17 else base[s % 3] for s in range(18)]
    series = pd.Series(vals, dtype="float64")
    stamps = [BASE + dt.timedelta(seconds=s * 1200) for s in range(18)]
    stamps[17] = BASE + dt.timedelta(seconds=21000)   # 17.5 intervals: off-grid
    ts = pd.Series(pd.to_datetime(stamps))
    pts = detect_seasonal_residual(
        series, ts, device_name="R1", metric="m",
        interval_seconds=1200, season_hours=1.0)
    assert pts == [], "an off-grid sample must never define a phase"


def test_seasonal_residual_survives_missing_timestamps_and_values():
    # A NaN value and a NaT timestamp must not crash the walk or shift the
    # surviving samples off their true time-of-day; the final outlier is still
    # judged against its same-phase past.
    base = (10.0, 20.0, 30.0)
    vals = [1000.0 if s == 17 else base[s % 3] for s in range(18)]
    vals[5] = None                                 # missing value (phase 2)
    stamps = [BASE + dt.timedelta(seconds=s * 1200) for s in range(18)]
    stamps[10] = pd.NaT                            # missing clock (phase 1)
    series = pd.Series([float("nan") if v is None else float(v)
                        for v in vals])
    ts = pd.Series(pd.to_datetime(stamps))
    pts = detect_seasonal_residual(
        series, ts, device_name="R1", metric="m",
        interval_seconds=1200, season_hours=1.0)
    assert pts
    p = [q for q in pts if q.timestamp == ts.iloc[17]][0]
    assert p.detector_config["phase"] == 2
    assert p.detector_config["grid_step"] == 17


def test_seasonal_residual_resets_phase_history_after_a_collection_hole():
    # The v1.1 gap guard, isolated: identical data twice, only
    # max_gap_intervals differs, so any difference in the verdicts is caused by
    # the reset and by nothing else. A collector that was down for nine
    # intervals must not be judged by baselines built before the hole.
    base = (10.0, 20.0, 30.0)
    steps = list(range(12)) + [20]              # 11 -> 20: a nine-interval hole
    counts: dict[int, int] = {}
    vals = []
    for s in steps:
        phase = s % 3
        seen = counts.get(phase, 0)
        counts[phase] = seen + 1
        if s == 20:
            vals.append(1000.0)                 # wild, but right after the hole
        else:
            # Alternate +/-0.5 so each phase keeps a non-zero MAD spread.
            vals.append(base[phase] + (0.5 if seen % 2 else -0.5))
    series = pd.Series(vals, dtype="float64")
    ts = pd.Series(pd.to_datetime(
        [BASE + dt.timedelta(seconds=s * 1200) for s in steps]))
    kw = dict(device_name="R1", metric="m", interval_seconds=1200,
              season_hours=1.0)

    with_reset = detect_seasonal_residual(series, ts, **kw)
    guard_off = detect_seasonal_residual(series, ts, max_gap_intervals=20.0,
                                         **kw)

    assert with_reset == [], ("a post-hole spike must not be judged against "
                              "pre-gap history")
    assert len(guard_off) == 1, ("the very same sample does flag when the "
                                 "cadence is treated as continuous")
    point = guard_off[0]
    assert point.timestamp == ts.iloc[-1]
    assert point.detector_config["phase"] == 2   # 20 % 3: its true cycle position
    assert point.detector_config["grid_step"] == 20


def test_detect_frame_defaults_to_interpretable_pair():
    frame = make_frame(SPIKE_SERIES)
    points = detect_frame(frame, device_name="R1", metric="m")
    assert {p.detector for p in points} <= set(DEFAULT_DETECTORS)
    assert "isolation_forest" not in {p.detector for p in points}


# ------------------------------------------------------ persistence + dedupe
def test_ingest_groups_repeated_anomalies_into_one_event(session):
    frame = make_frame(NORMAL + [300.0, 320.0, 310.0])
    points = detect_frame(frame, device_name="R1", metric="ping.avg_rtt_ms",
                          window=8, mad_threshold=3.0, zscore_threshold=3.0)
    result = anomaly_service.ingest_anomaly_points(
        session, points, group_window_seconds=300)
    events = anomaly_service.list_events(session)
    per_detector = {}
    for e in events:
        per_detector.setdefault(e.detector, []).append(e)
    # Consecutive spike samples must never create several rows per detector.
    for detector, rows in per_detector.items():
        assert len(rows) == 1, f"{detector}: expected grouped single event"
        assert rows[0].status == "open"
    # The robust MAD detector flags the sustained deviation and groups it;
    # the z-score detector may adapt after the first large value (documented
    # rolling-detector behaviour), so grouping strength is asserted on MAD.
    assert per_detector["rolling_mad"][0].occurrences >= 2
    assert result["created"] == len(per_detector)
    assert result["updated"] >= 2


def test_ingest_is_deterministic_replay_updates_not_duplicates(session):
    frame = make_frame(SPIKE_SERIES)
    points = detect_frame(frame, device_name="R1", metric="m", window=8)
    anomaly_service.ingest_anomaly_points(session, points,
                                          group_window_seconds=300)
    before = len(anomaly_service.list_events(session))
    again = anomaly_service.ingest_anomaly_points(session, points,
                                                  group_window_seconds=300)
    after = len(anomaly_service.list_events(session))
    assert after == before          # replay creates no duplicate rows
    assert again["created"] == 0
    assert again["updated"] == len(points)  # every point merged into its event


def test_resolution_requires_normal_observation(session):
    frame = make_frame(SPIKE_SERIES)
    points = detect_frame(frame, device_name="R1", metric="m", window=8)
    anomaly_service.ingest_anomaly_points(session, points,
                                          group_window_seconds=300)
    events = anomaly_service.list_events(session)
    assert events and all(e.status == "open" for e in events)
    # A MISSING sample must not resolve anything: calling with no timestamp
    # (simulated by the UI never invoking it) leaves events open.
    # An actual normal observation AFTER the anomaly resolves:
    normal_ts = BASE + dt.timedelta(minutes=30)
    resolved = anomaly_service.resolve_events_on_normal_observation(
        session, device_name="R1", metric="m", interface_name=None,
        data_source="live", normal_timestamp=normal_ts)
    assert resolved == len(events)
    for e in anomaly_service.list_events(session):
        assert e.status == "resolved"
        assert e.resolved_at is not None


def test_event_stores_detector_config_and_source_link(session):
    frame = make_frame(SPIKE_SERIES)
    points = detect_rolling_mad(frame["value"], frame["collected_at"],
                                device_name="R1", metric="m", window=8)
    anomaly_service.ingest_anomaly_points(session, points,
                                          group_window_seconds=300)
    e = anomaly_service.list_events(session)[0]
    import json
    assert json.loads(e.detector_config_json)["window"] == 8
    assert e.data_source == "live"
    assert e.explanation and "median" in e.explanation
    ids = json.loads(e.source_samples_json)
    assert all(isinstance(i, int) for i in ids)


def test_run_detection_gates_insufficient_series(session):
    from database import repository
    for i, v in enumerate([1.0, None]):
        repository.add_metric_sample(
            session, "R1", "thin.metric", v, collected_at=
            BASE + dt.timedelta(seconds=i * 60))
    session.commit()
    result = anomaly_service.run_detection(
        session, device_name="R1", metric="thin.metric",
        interface_name=None, data_source="live")
    assert result["status"] == "insufficient_data"
    assert result["points"] == []
    assert anomaly_service.list_events(session) == []  # nothing persisted


def test_run_detection_happy_path_on_seeded_data(session):
    from database import repository
    for i, v in enumerate(SPIKE_SERIES):
        repository.add_metric_sample(
            session, "R1", "ping.avg_rtt_ms", v, unit="ms",
            data_source="live",
            collected_at=BASE + dt.timedelta(seconds=i * 60))
    session.commit()
    result = anomaly_service.run_detection(
        session, device_name="R1", metric="ping.avg_rtt_ms",
        interface_name=None, data_source="live")
    assert result["status"] == "ok"
    assert result["n_anomalies"] >= 1
    assert anomaly_service.list_events(session)


def test_run_detection_resolves_events_end_to_end(session):
    """Event stays OPEN while the anomaly is the newest observation and is
    resolved by run_detection once a later normal observation arrives."""
    from database import repository
    up_to_spike = SPIKE_SERIES[:SPIKE_AT + 1]     # ends ON the spike
    for i, v in enumerate(up_to_spike):
        repository.add_metric_sample(
            session, "R1", "rtt", v, unit="ms", data_source="live",
            collected_at=BASE + dt.timedelta(seconds=i * 60))
    session.commit()
    r1 = anomaly_service.run_detection(
        session, device_name="R1", metric="rtt",
        interface_name=None, data_source="live")
    assert r1["status"] == "ok" and r1["persisted"]["created"] >= 1
    assert r1["resolved"] == 0                    # no proof of normality yet
    events = anomaly_service.list_events(session, status="open")
    assert events, "event must remain open"
    # a real normal observation afterwards (next collection cycle):
    repository.add_metric_sample(
        session, "R1", "rtt", 11.0, unit="ms", data_source="live",
        collected_at=BASE + dt.timedelta(seconds=len(up_to_spike) * 60))
    session.commit()
    r2 = anomaly_service.run_detection(
        session, device_name="R1", metric="rtt",
        interface_name=None, data_source="live")
    assert r2["resolved"] >= 1
    assert anomaly_service.list_events(session, status="open") == []


def test_data_source_separation_end_to_end(session):
    from database import repository
    for i, v in enumerate(SPIKE_SERIES):
        repository.add_metric_sample(
            session, "R1", "m", v, data_source="demo",
            collected_at=BASE + dt.timedelta(seconds=i * 60))
    session.commit()
    anomaly_service.run_detection(session, device_name="R1", metric="m",
                                  interface_name=None, data_source="demo")
    events = anomaly_service.list_events(session, data_source="demo")
    assert events and all(e.data_source == "demo" for e in events)
    assert anomaly_service.list_events(session, data_source="live") == []
