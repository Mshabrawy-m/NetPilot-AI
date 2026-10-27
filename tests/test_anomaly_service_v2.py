"""Service-level tests for the enhanced anomaly pipeline (Phase 2 v2).

These cover what only exists at the service boundary: the stored-series
inventory a batch scan walks, the new detector knobs flowing through
``run_detection``, the bounded scan itself, the event aggregate and label-driven
calibration. Every fixture is SYNTHETIC and documented as such - none of it is
evidence of detection performance on a real network.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from analytics.data_quality import DETECTION, SeriesQuality
from database import repository
from services import anomaly_service

BASE = dt.datetime(2026, 1, 1, 12, 0, 0)
STEP_S = 1200          # 20-minute grid: season_hours=1 -> a 3-sample season
SEASON_HOURS = "1"
METRIC = "ping.avg_rtt_ms"

# Deterministic zero-mean jitter, so expected flags stay reproducible.
JITTER = [0.3, -0.4, 0.2, -0.3, 0.5, -0.2, 0.4, -0.5, 0.1, -0.1]


def steady(n, level=10.0):
    return [level + JITTER[i % len(JITTER)] for i in range(n)]


SPIKE_AT = 30
SPIKED = steady(40)[:SPIKE_AT] + [500.0] + steady(40)[SPIKE_AT + 1:]
# Same spike with nothing normal after it: nothing can resolve this episode.
SPIKE_LAST = steady(39) + [500.0]
# A sustained +0.5 shift: invisible to a 3-sigma z-score, obvious to CUSUM.
SHIFT_AT = 30
SHIFTED = steady(SHIFT_AT) + steady(10, level=10.5)

OUTLIER_AT = 21
SEASONAL = [[10.0, 20.0, 30.0][i % 3] + 0.5 * (i // 3) for i in range(36)]
SEASONAL_OUTLIER = (SEASONAL[:OUTLIER_AT] + [100.0]
                    + SEASONAL[OUTLIER_AT + 1:])


def seed(session, values, *, device="R1", metric=METRIC, interface=None,
         source="live", unit="ms", step_s=STEP_S):
    for i, v in enumerate(values):
        repository.add_metric_sample(
            session, device, metric, v, unit=unit, interface_name=interface,
            data_source=source,
            collected_at=BASE + dt.timedelta(seconds=i * step_s))
    session.commit()


def stamp(index, step_s=STEP_S) -> dt.datetime:
    return BASE + dt.timedelta(seconds=index * step_s)


def make_frame(values):
    n = len(values)
    return pd.DataFrame({
        "sample_id": list(range(1, n + 1)),
        "collected_at": pd.to_datetime([stamp(i) for i in range(n)]),
        "value": [None if v is None else float(v) for v in values],
        "unit": "ms", "data_source": "live", "imputed": [False] * n,
    })


def detect(session, monkeypatch, values, detectors, *, metric=METRIC,
           device="R1", **kwargs):
    """Run one detection over a freshly seeded device-level series."""
    monkeypatch.setenv("NETPILOT_SEASON_HOURS", SEASON_HOURS)
    seed(session, values, device=device, metric=metric)
    return anomaly_service.run_detection(
        session, device_name=device, metric=metric,
        interface_name=None, data_source="live", detectors=detectors, **kwargs)


# ------------------------------------------------------------- series inventory
def test_inventory_groups_series_and_counts_only_usable_samples(session):
    values = steady(40)
    values[4] = None
    values[9] = None
    seed(session, values)
    seed(session, steady(40), interface="eth0")
    seed(session, steady(15), device="R2", metric="cpu.pct")
    rows = repository.get_series_inventory(session)
    keys = {(r["device_name"], r["metric"], r["interface_name"]) for r in rows}
    assert keys == {("R1", METRIC, None), ("R1", METRIC, "eth0"),
                    ("R2", "cpu.pct", None)}
    device_level = next(r for r in rows
                        if r["device_name"] == "R1" and r["interface_name"] is None)
    assert device_level["n_samples"] == 38, "NULL values are not evidence"
    assert device_level["data_source"] == "live"
    assert device_level["unit"] == "ms"
    # richest first, so a capped scan spends its budget on real histories
    counts = [r["n_samples"] for r in rows]
    assert counts == sorted(counts, reverse=True)


def test_inventory_filters_by_source_device_and_sample_floor(session):
    seed(session, steady(40))
    seed(session, steady(20), device="R2")
    seed(session, steady(30), source="demo", device="R3")
    assert {r["device_name"] for r in repository.get_series_inventory(
        session, data_source="demo")} == {"R3"}
    assert {r["device_name"] for r in repository.get_series_inventory(
        session, device_name="R2")} == {"R2"}
    assert len(repository.get_series_inventory(session)) == 3
    assert len(repository.get_series_inventory(session, min_samples=25)) == 2
    assert repository.get_series_inventory(session, min_samples=10 ** 6) == []
    assert repository.get_series_inventory(
        session, data_source="live", device_name="R3") == []


def test_inventory_of_an_empty_database_is_empty(session):
    assert repository.get_series_inventory(session) == []


# --------------------------------------------------------------- detector knobs
def test_run_detection_rejects_an_unknown_detector_without_touching_the_db(
        session, monkeypatch):
    result = detect(session, monkeypatch, SPIKED, ("not_a_detector",))
    assert result["status"] == "unknown_detector"
    assert result["points"] == [] and result["persisted"] == {}
    assert "not_a_detector" in result["reasons"][0]
    assert repository.get_anomaly_events(session) == []


def test_the_new_change_point_detectors_run_and_persist(session, monkeypatch):
    result = detect(session, monkeypatch, SHIFTED, ("cusum", "ewma"))
    assert result["status"] == "ok"
    assert result["detectors"] == ["cusum", "ewma"]
    assert "cusum" in result["by_detector"], (
        "a sustained 2-sigma shift must accumulate a CUSUM verdict")
    assert set(result["by_detector"]) <= {"cusum", "ewma"}
    events = repository.get_anomaly_events(session)
    assert events and {e.detector for e in events} <= {"cusum", "ewma"}
    assert all(e.data_source == "live" for e in events)


def test_sensitivity_knobs_reach_the_detector_and_are_stored(session, monkeypatch):
    result = detect(session, monkeypatch, SPIKED, ("ewma",),
                    ewma_threshold=2.0)
    assert result["points"]
    for p in result["points"]:
        assert p.detector_config["threshold"] == 2.0
    cusum_result = detect(session, monkeypatch, SHIFTED, ("cusum",),
                          metric="cpu.pct", cusum_drift=0.2, cusum_limit=2.5)
    for p in cusum_result["points"]:
        assert p.detector_config["drift"] == 0.2
        assert p.detector_config["limit"] == 2.5


def test_sensitivity_defaults_come_from_the_environment(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_EWMA_THRESHOLD", "4.5")
    monkeypatch.setenv("NETPILOT_CUSUM_DRIFT", "0.25")
    monkeypatch.setenv("NETPILOT_CUSUM_THRESHOLD", "5.5")
    result = detect(session, monkeypatch, SPIKED, ("ewma",))
    assert result["points"]
    assert all(p.detector_config["threshold"] == 4.5 for p in result["points"])
    cusum = detect(session, monkeypatch, SHIFTED, ("cusum",), metric="cpu.pct")
    for p in cusum["points"]:
        assert p.detector_config["drift"] == 0.25
        assert p.detector_config["limit"] == 5.5


def test_the_seasonal_detector_needs_no_abstention_note_when_the_season_fits(
        session, monkeypatch):
    result = detect(session, monkeypatch, SEASONAL_OUTLIER,
                    ("seasonal_residual",))
    assert result["status"] == "ok"
    assert result["interval_seconds"] == pytest.approx(STEP_S)
    assert result["notes"] == []
    flagged = [p for p in result["points"]
               if p.timestamp == pd.Timestamp(stamp(OUTLIER_AT))]
    assert flagged, "a phase outlier must be caught by the seasonal detector"
    p = flagged[0]
    assert p.detector == "seasonal_residual"
    assert p.baseline < 20.0, "the baseline is the same-phase past, not the mean"
    events = repository.get_anomaly_events(session)
    assert [e.detector for e in events] == ["seasonal_residual"]


def test_a_detector_without_an_interval_abstains_instead_of_reporting_clean(
        session, monkeypatch):
    """The interval gate cannot be reached through a real stored series (two
    samples already define one), so the readiness stub is injected: the service
    must still separate "no opinion" from "nothing wrong"."""
    frame = make_frame(SPIKED)
    quality = SeriesQuality(device_name="R1", metric=METRIC,
                            interface_name=None, data_source="live", unit="ms",
                            n_rows=len(SPIKED), n_non_null=len(SPIKED),
                            observed_interval_seconds=None,
                            ready_for={DETECTION: True})
    monkeypatch.setattr(anomaly_service, "load_ready_series",
                        lambda *a, **k: (frame, quality))
    result = anomaly_service.run_detection(
        session, device_name="R1", metric=METRIC, interface_name=None,
        data_source="live", detectors=("rolling_mad", "seasonal_residual"))
    assert result["status"] == "ok"
    assert len(result["notes"]) == 1
    note = result["notes"][0]
    assert "seasonal_residual" in note and "abstained" in note
    assert "not a clean result" in note
    assert result["by_detector"] == {"rolling_mad": len(result["points"])}


# ------------------------------------------------------------------ batch scan
def test_scan_walks_device_level_series_only(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_SEASON_HOURS", SEASON_HOURS)
    seed(session, SPIKED)
    # Interface-level rows of the SAME device+metric, with a far bigger spike at
    # another index: if they leaked into the device-level scan they would show
    # up as a 5000.0 observation.
    interface_values = steady(40)
    interface_values[5] = 5000.0
    seed(session, interface_values, interface="eth0")
    seed(session, SHIFTED, device="R2", metric="cpu.pct")
    seed(session, steady(5), device="R3", metric="bgp.sessions")

    res = anomaly_service.scan_ready_series(session, data_source="live")
    assert res["status"] == "ok"
    assert {r["series"] for r in res["results"]} == {
        f"R1/{METRIC}", "R2/cpu.pct"}
    # three series are stored for this source, but only the two device-level
    # ones are scannable and the 5-sample series is not even a candidate
    assert res["candidates"] == 2 and res["scanned"] == 2
    assert res["skipped"] == [], "thin series never enter the inventory"
    assert res["totals"]["series_ok"] == 2
    assert res["totals"]["points"] == sum(r["n_points"] for r in res["results"])
    events = repository.get_anomaly_events(session)
    assert events
    assert all(e.observed_value != 5000.0 for e in events), (
        "the scan is device-level: interface rows must not contaminate it")
    assert any(e.observed_value == 500.0 for e in events), (
        "the device-level spike is still found")


def test_scan_reports_its_own_cap_instead_of_quietly_doing_less(session,
                                                                monkeypatch):
    monkeypatch.setenv("NETPILOT_SCAN_MAX_SERIES", "2")
    for i, metric in enumerate(("m1", "m2", "m3", "m4")):
        seed(session, SPIKED, metric=metric, device=f"R{i + 1}")
    res = anomaly_service.scan_ready_series(session, data_source="live")
    assert res["limit"] == 2 and res["candidates"] == 4
    assert res["capped"] is True
    assert len(res["results"]) + len(res["skipped"]) == 2
    assert any("scan capped at 2 of 4" in n for n in res["notes"])
    assert any("NETPILOT_SCAN_MAX_SERIES" in n for n in res["notes"])
    # an explicit smaller budget is honoured, a bigger one is not
    assert anomaly_service.scan_ready_series(
        session, data_source="live", max_series=1)["limit"] == 1
    assert anomaly_service.scan_ready_series(
        session, data_source="live", max_series=99)["limit"] == 2


def test_scan_filters_by_device_and_passes_detectors_down(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_SEASON_HOURS", SEASON_HOURS)
    seed(session, SHIFTED)
    seed(session, SPIKED, device="R2", metric="cpu.pct")
    res = anomaly_service.scan_ready_series(session, data_source="live",
                                           device_name="R2")
    assert [r["series"] for r in res["results"]] == ["R2/cpu.pct"]
    res2 = anomaly_service.scan_ready_series(session, data_source="live",
                                            detectors=("cusum",))
    assert set(res2["by_detector"]) == {"cusum"}
    assert all(set(r["by_detector"]) <= {"cusum"} for r in res2["results"])


def test_a_series_that_survives_the_inventory_but_not_the_gate_is_skipped(
        session, monkeypatch):
    """The inventory counts stored rows, the gate counts DEDUPLICATED ones: 20
    rows sharing six timestamps are not 20 observations."""
    monkeypatch.setenv("NETPILOT_SEASON_HOURS", SEASON_HOURS)
    values = steady(20)
    for i in range(5, 20):
        repository.add_metric_sample(session, "R1", "thin.metric", values[i],
                                     unit="ms", data_source="live",
                                     collected_at=stamp(i % 6))
    for i in range(5):
        repository.add_metric_sample(session, "R1", "thin.metric", values[i],
                                     unit="ms", data_source="live",
                                     collected_at=stamp(i))
    session.commit()
    rows = repository.get_series_inventory(session, data_source="live")
    assert [r["n_samples"] for r in rows if r["metric"] == "thin.metric"] == [20]
    res = anomaly_service.scan_ready_series(session, data_source="live")
    assert res["results"] == []
    assert [s["series"] for s in res["skipped"]] == ["R1/thin.metric"]
    assert res["skipped"][0]["status"] == "insufficient_data"
    assert res["skipped"][0]["reasons"]
    assert repository.get_anomaly_events(session, metric="thin.metric") == []


# --------------------------------------------------------------- event summary
def test_event_summary_counts_nothing_as_nothing(session):
    res = anomaly_service.event_summary(session)
    assert res["n_events"] == 0 and res["truncated"] is False
    assert res["by_severity"] == {} and res["by_detector"] == {}
    assert res["open"] == 0 and res["resolved"] == 0
    assert res["critical_open"] == 0 and res["total_occurrences"] == 0
    assert res["median_episode_seconds"] is None, (
        "no events is not the same as a zero-second episode")
    assert res["max_episode_seconds"] is None
    assert res["median_resolution_seconds"] is None
    assert res["newest_open_at"] is None
    assert res["top_recurring"] == []


def test_event_summary_aggregates_a_real_detection_run(session, monkeypatch):
    result = detect(session, monkeypatch, SPIKE_LAST,
                    ("rolling_mad", "rolling_zscore", "ewma"))
    assert result["points"]
    events = repository.get_anomaly_events(session)
    assert events, "the run stored events"
    res = anomaly_service.event_summary(session, data_source="live")
    assert res["n_events"] == len(events)
    # the spike is the newest sample, so no normal observation has resolved it
    assert res["open"] == len(events) and res["resolved"] == 0
    assert res["truncated"] is False
    assert sum(res["by_severity"].values()) == len(events)
    assert set(res["by_detector"]) <= {"rolling_mad", "rolling_zscore", "ewma"}
    for cell in res["by_detector"].values():
        assert set(cell) == {"events", "open", "occurrences"}
        assert cell["events"] >= cell["open"]
    assert res["by_metric"][METRIC]["events"] == len(events)
    assert res["total_occurrences"] == sum(int(e.occurrences or 1)
                                          for e in events)
    # single-point episodes have a real duration of zero, which is a measurement
    assert res["median_episode_seconds"] == 0.0
    assert res["max_episode_seconds"] == 0.0
    assert res["median_resolution_seconds"] is None
    assert res["newest_open_at"] == max(e.last_seen_at for e in events)
    assert res["top_recurring"] == [], "one occurrence is not a recurring event"
    assert res["filters"]["data_source"] == "live"


def test_only_a_real_normal_observation_resolves_an_event(session, monkeypatch):
    """The same run that stored the event also proved normality afterwards: the
    episode is resolved by that observation, not by the scan finishing."""
    detect(session, monkeypatch, SPIKED, ("rolling_mad",))
    events = repository.get_anomaly_events(session)
    assert events
    res = anomaly_service.event_summary(session)
    assert res["open"] == 0 and res["resolved"] == len(events)
    assert res["newest_open_at"] is None
    assert res["critical_open"] == 0
    # first_seen = the spike, resolved_at = the newest unflagged sample
    assert res["median_episode_seconds"] == 0.0
    assert res["median_resolution_seconds"] == pytest.approx(
        (stamp(39) - stamp(SPIKE_AT)).total_seconds())
    assert res["median_resolution_seconds"] > res["median_episode_seconds"]
    # and a later run over the same evidence must not re-open anything
    again = anomaly_service.run_detection(
        session, device_name="R1", metric=METRIC, interface_name=None,
        data_source="live", detectors=("rolling_mad",))
    assert again["status"] == "ok"
    assert anomaly_service.event_summary(session)["open"] == 0


def test_event_summary_applies_every_filter_and_flags_truncation(
        session, monkeypatch):
    detect(session, monkeypatch, SPIKED, ("rolling_mad", "ewma"))
    mad = anomaly_service.event_summary(session, detector="rolling_mad")
    assert set(mad["by_detector"]) == {"rolling_mad"}
    assert mad["n_events"] == len(
        repository.get_anomaly_events(session, detector="rolling_mad"))
    assert anomaly_service.event_summary(session, metric="cpu.pct")[
        "n_events"] == 0
    assert anomaly_service.event_summary(session, device_name="R2")[
        "n_events"] == 0
    assert anomaly_service.event_summary(
        session, since=BASE + dt.timedelta(days=1))["n_events"] == 0
    assert anomaly_service.event_summary(session, limit=1)["truncated"] is True


# ----------------------------------------------------------------- calibration
def test_calibration_scores_against_the_labels_the_caller_owns(session):
    frame = make_frame(SPIKED)
    labels = [stamp(SPIKE_AT)]
    rows = anomaly_service.calibrate_detectors(
        frame, device_name="R1", metric=METRIC,
        labelled_anomalies=labels, detectors=("rolling_mad", "ewma"),
        thresholds=(2.0, 3.5), window=8)
    assert len(rows) == 4
    assert all(r["scoreable"] is True for r in rows)
    assert all(r["labels_available"] is True for r in rows)
    for r in rows:
        assert "not real-world accuracy" in r["note"]
        assert r["f1"] is None or 0.0 <= r["f1"] <= 1.0
    hit = [r for r in rows if r["detector"] == "rolling_mad"]
    assert any(r["true_positives"] == 1 for r in hit), (
        "the labelled spike must be recoverable by the baseline detector")
    ranked = sorted((r for r in rows if r.get("rank")),
                    key=lambda r: r["rank"])
    assert ranked and ranked[0]["rank"] == 1
    assert ranked[0]["f1"] == max(r["f1"] for r in rows if r["f1"] is not None)


def test_calibration_names_what_it_cannot_calibrate(session):
    frame = make_frame(SPIKED)
    rows = anomaly_service.calibrate_detectors(
        frame, device_name="R1", metric=METRIC, labelled_anomalies=[stamp(SPIKE_AT)],
        detectors=("not_a_detector", "isolation_forest"))
    assert len(rows) == 2
    unknown, forest = rows
    assert unknown["scoreable"] is False and "unknown detector" in unknown["note"]
    assert unknown["f1"] is None
    assert forest["scoreable"] is False
    assert "no single sensitivity knob" in forest["note"]
    assert all("rank" not in r for r in rows)


def test_a_seasonal_detector_without_an_interval_abstains_at_every_threshold(
        session):
    frame = make_frame(SEASONAL_OUTLIER)
    rows = anomaly_service.calibrate_detectors(
        frame, device_name="R1", metric=METRIC,
        labelled_anomalies=[stamp(OUTLIER_AT)],
        detectors=("seasonal_residual",), thresholds=(3.0,), window=8,
        interval_seconds=None, season_hours=0.05)
    assert len(rows) == 1
    r = rows[0]
    assert r["scoreable"] is False
    assert r["f1"] is None
    assert "abstained at every threshold" in r["note"]
    assert "not a clean result" in r["note"]


def test_calibration_without_labels_reports_counts_not_accuracy(session):
    frame = make_frame(SPIKED)
    rows = anomaly_service.calibrate_detectors(
        frame, device_name="R1", metric=METRIC, labelled_anomalies=[],
        detectors=("rolling_mad",), thresholds=(3.5,), window=8)
    assert len(rows) == 1
    r = rows[0]
    assert r["labels_available"] is False
    assert r["f1"] is None and r["recall"] is None
    assert r["true_positives"] == 0
    assert "no labels supplied" in r["note"]
    assert r["n_flagged"] > 0, "the detector still ran, it just cannot be scored"


def test_calibrate_series_gates_loads_and_picks_the_best_setting(
        session, monkeypatch):
    monkeypatch.setenv("NETPILOT_SEASON_HOURS", SEASON_HOURS)
    seed(session, SPIKED)
    res = anomaly_service.calibrate_series(
        session, device_name="R1", metric=METRIC, interface_name=None,
        data_source="live", labelled_anomalies=[stamp(SPIKE_AT)],
        detectors=("rolling_mad", "ewma"), thresholds=(2.0, 3.5), window=8)
    assert res["status"] == "ok"
    assert res["interval_seconds"] == pytest.approx(STEP_S)
    assert len(res["rows"]) == 4
    assert res["best"] is not None
    assert res["best"]["f1"] == max(r["f1"] for r in res["rows"]
                                    if r["f1"] is not None)
    assert res["quality"].ready(DETECTION)


def test_calibrate_series_refuses_thin_data(session, monkeypatch):
    monkeypatch.setenv("NETPILOT_SEASON_HOURS", SEASON_HOURS)
    seed(session, steady(6), metric="thin.metric")
    res = anomaly_service.calibrate_series(
        session, device_name="R1", metric="thin.metric", interface_name=None,
        data_source="live", labelled_anomalies=[], detectors=("rolling_mad",))
    assert res["status"] == "insufficient_data"
    assert res["rows"] == []
    assert res["reasons"], "the refusal says why"
    assert "best" not in res, "nothing was scored, so nothing was best"
