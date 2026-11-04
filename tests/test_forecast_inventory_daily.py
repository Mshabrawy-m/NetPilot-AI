"""Stage A6-4 - live forecasting readiness: rollup-only discovery + daily cadence.

Three contracts are exercised offline on SQLite:

* the **daily cadence** loader reconciles raw samples and daily rollups onto one
  uniform UTC-day grid (rollup preferred on overlap, empty days omitted, an
  all-NULL day kept as NaN, never 0) so a window that the native cadence must
  refuse for mixing granularities becomes genuinely forecastable - *without*
  removing the safety gate for the native cadence;
* the **unified inventory** discovers series that exist only in rollups, never
  lists a raw+rollup series twice, and keeps Demo/Live isolated;
* **forecastability** is decided by the real data-quality gate, not a count.
"""
from __future__ import annotations

import datetime as dt
import math

import pytest

from analytics import time_series
from database import repository
from database.models import MetricDailyRollup
from services import forecast_service

BASE = dt.datetime(2026, 1, 1)


def _raw(session, *, device="R1", metric="rtt", value, source="live",
         iface=None, at=BASE, unit="ms"):
    repository.add_metric_sample(
        session, device_name=device, metric=metric, value=value, unit=unit,
        interface_name=iface, data_source=source, collected_at=at)


def _rollup(session, *, day, device="R1", metric="rtt", value, source="live",
            iface=None, unit="ms", sample_count=5):
    session.add(MetricDailyRollup(
        bucket_date=day, device_name=device, interface_name=iface,
        interface_key=iface or "", metric=metric, data_source=source,
        sample_count=sample_count, avg_value=value,
        min_value=(None if value is None else float(value) - 1),
        max_value=(None if value is None else float(value) + 1), unit=unit,
        first_seen=dt.datetime.combine(day, dt.time(0, 5)),
        last_seen=dt.datetime.combine(day, dt.time(23, 55))))


def seed_raw(session, values, *, device="R1", metric="rtt", source="live",
             iface=None, step_s=1200, start=BASE):
    for i, v in enumerate(values):
        _raw(session, device=device, metric=metric, value=v, source=source,
             iface=iface, at=start + dt.timedelta(seconds=i * step_s))
    session.commit()


def seed_rollups(session, values, *, device="R1", metric="rtt", source="live",
                 iface=None, start_day=dt.date(2026, 1, 1)):
    for i, v in enumerate(values):
        _rollup(session, day=start_day + dt.timedelta(days=i), device=device,
                metric=metric, value=v, source=source, iface=iface)
    session.commit()


# ----------------------------------------------------------- daily loader
def test_daily_raw_only_aggregates_to_uniform_utc_days(session):
    # Two readings on Jan 1, two on Jan 2, one on Jan 3 -> three day means.
    for at, v in [(dt.datetime(2026, 1, 1, 2), 10.0),
                  (dt.datetime(2026, 1, 1, 10), 20.0),
                  (dt.datetime(2026, 1, 2, 6), 30.0),
                  (dt.datetime(2026, 1, 2, 18), 40.0),
                  (dt.datetime(2026, 1, 3, 5), 50.0)]:
        _raw(session, value=v, at=at)
    session.commit()
    frame = time_series.load_metric_frame_daily(
        session, "R1", "rtt", data_source="live")
    assert len(frame) == 3
    assert [c.to_pydatetime() for c in frame["collected_at"]] == [
        dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2), dt.datetime(2026, 1, 3)]
    assert list(frame["value"]) == [15.0, 35.0, 50.0]
    assert all(p == time_series.ORIGIN_RAW for p in frame["provenance"])


def test_daily_rollup_only_uses_stored_means(session):
    seed_rollups(session, [40.0, 41.0, 42.0, 43.0, 44.0])
    frame = time_series.load_metric_frame_daily(
        session, "R1", "rtt", data_source="live")
    assert len(frame) == 5
    assert list(frame["value"]) == [40.0, 41.0, 42.0, 43.0, 44.0]
    assert all(p == time_series.ORIGIN_ROLLUP for p in frame["provenance"])


def test_daily_overlap_prefers_rollup_one_row_per_day(session):
    # Rollups Jan 1..5; raw also lives on Jan 4 and Jan 6.
    seed_rollups(session, [10.0, 20.0, 30.0, 44.0, 50.0],
                 start_day=dt.date(2026, 1, 1))
    seed_raw(session, [100.0, 200.0, 700.0],
             start=dt.datetime(2026, 1, 4, 1))
    # put the 3rd raw sample on Jan 6 (spans past the rollup window)
    session.commit()
    frame = time_series.load_metric_frame_daily(
        session, "R1", "rtt", data_source="live")
    days = [c.to_pydatetime() for c in frame["collected_at"]]
    # exactly one observation per UTC day, no duplicates
    assert len(days) == len(set(days))
    assert dt.datetime(2026, 1, 4) in days
    # Jan 4 has both -> the stored rollup mean (44.0) wins, not the raw mean
    jan4 = frame[frame["collected_at"] == dt.datetime(2026, 1, 4)].iloc[0]
    assert jan4["value"] == pytest.approx(44.0)
    assert jan4["provenance"] == time_series.ORIGIN_ROLLUP


def test_daily_empty_day_omitted_all_null_day_is_nan(session):
    _raw(session, value=10.0, at=dt.datetime(2026, 1, 1, 3))
    _raw(session, value=20.0, at=dt.datetime(2026, 1, 1, 9))
    # Jan 2 has no rows at all -> omitted; Jan 3 rows exist but are all NULL.
    _raw(session, value=None, at=dt.datetime(2026, 1, 3, 4))
    session.commit()
    frame = time_series.load_metric_frame_daily(
        session, "R1", "rtt", data_source="live")
    days = {c.to_pydatetime() for c in frame["collected_at"]}
    assert dt.datetime(2026, 1, 2) not in days            # nothing fabricated
    assert dt.datetime(2026, 1, 3) in days                 # a real (missing) day
    jan3 = frame[frame["collected_at"] == dt.datetime(2026, 1, 3)].iloc[0]
    assert math.isnan(float(jan3["value"]))                # NaN, never 0


def test_daily_demo_never_reads_live_rollups(session):
    seed_rollups(session, [100.0, 200.0], source="live")
    seed_raw(session, [1.0, 2.0, 3.0], source="demo",
             start=dt.datetime(2026, 3, 1))
    frame = time_series.load_metric_frame_daily(
        session, "R1", "rtt", data_source="demo")
    assert all(p == time_series.ORIGIN_RAW for p in frame["provenance"])
    assert frame["value"].max() == pytest.approx(2.0)      # demo day mean only


# ------------------------------------------------- daily cadence unlocks mixed
def test_mixed_history_is_refused_raw_but_forecastable_daily(session):
    # 45 older daily rollups + a fine recent raw grid (dense 5-min sampling on
    # the most recent day) - the mixed window the native cadence must refuse.
    seed_rollups(session, [50.0 + (i % 7) for i in range(45)],
                 start_day=dt.date(2026, 1, 1))
    seed_raw(session, [80.0 + (i % 20) for i in range(250)],
             start=dt.datetime(2026, 2, 15), step_s=300)

    raw_cadence = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live", horizon=3, models=("naive",), persist=False,
        cadence="raw")
    assert raw_cadence["status"] == "insufficient_data"
    assert any("granularit" in r for r in raw_cadence["reasons"])

    daily_cadence = forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live", horizon=3, models=("naive", "holt_damped"),
        persist=False, cadence="daily")
    assert daily_cadence["status"] == "ok"
    assert daily_cadence["cadence"] == "daily"
    assert daily_cadence["quality"].observed_interval_seconds == 86400.0
    # provenance still records that both sources contributed
    prov = daily_cadence["history_provenance"]
    assert prov["rollup"] > 0 and prov["raw"] > 0


# --------------------------------------------------------- rollup inventory
def test_rollup_series_inventory_counts_and_latest(session):
    seed_rollups(session, [10.0, 20.0, 30.0, 40.0, 50.0])
    rows = repository.get_rollup_series_inventory(session, data_source="live")
    assert len(rows) == 1
    row = rows[0]
    assert row["device_name"] == "R1" and row["metric"] == "rtt"
    assert row["interface_name"] is None
    assert row["n_days"] == 5
    assert row["n_source_samples"] == 25                    # 5 * sample_count 5
    assert row["latest_date"] == dt.date(2026, 1, 5)
    # demo filter returns nothing: rollups are live-only
    assert repository.get_rollup_series_inventory(
        session, data_source="demo") == []


# --------------------------------------------------------- unified inventory
def test_inventory_discovers_rollup_only_series(session):
    seed_rollups(session, [10.0 + i for i in range(35)],
                 metric="pktloss")
    # The raw inventory cannot see it at all (no raw samples exist).
    assert repository.get_series_inventory(session, data_source="live") == []
    inv = forecast_service.list_forecast_series(
        session, data_source="live")
    match = [r for r in inv if r["metric"] == "pktloss"]
    assert len(match) == 1
    row = match[0]
    assert row["coverage"] == "rollup"
    assert row["n_raw_samples"] == 0 and row["n_rollup_days"] == 35


def test_inventory_has_raw_only_row(session):
    seed_raw(session, [50.0 + i for i in range(35)])
    inv = forecast_service.list_forecast_series(session, data_source="live")
    row = next(r for r in inv if r["metric"] == "rtt")
    assert row["coverage"] == "raw"
    assert row["n_raw_samples"] == 35 and row["n_rollup_days"] == 0


def test_inventory_merges_both_without_duplicates(session):
    # Same (device, interface, metric, source) present in raw AND rollups.
    seed_raw(session, [60.0 + i for i in range(20)], iface="eth0",
             start=dt.datetime(2026, 2, 1))
    seed_rollups(session, [10.0, 20.0, 30.0], iface="eth0",
                 start_day=dt.date(2026, 1, 1))
    inv = forecast_service.list_forecast_series(session, data_source="live")
    eth0 = [r for r in inv if r["interface_name"] == "eth0"]
    assert len(eth0) == 1                                   # never listed twice
    row = eth0[0]
    assert row["coverage"] == "both"
    assert row["n_raw_samples"] == 20 and row["n_rollup_days"] == 3


def test_inventory_demo_live_isolation(session):
    seed_rollups(session, [10.0, 20.0], source="live", metric="pktloss")
    seed_raw(session, [1.0, 2.0, 3.0], source="demo", metric="rtt")
    live = forecast_service.list_forecast_series(session, data_source="live")
    assert any(r["coverage"] == "rollup" for r in live)
    assert all(r["data_source"] == "live" for r in live)
    demo = forecast_service.list_forecast_series(session, data_source="demo")
    assert demo and all(r["data_source"] == "demo" for r in demo)
    # rollups are live-only -> no demo row ever claims rollup coverage
    assert all(r["coverage"] != "rollup" and r["n_rollup_days"] == 0
               for r in demo)


# ---------------------------------------------------- forecastability gate
def test_forecastability_uses_the_real_gate(session):
    seed_raw(session, [50.0 + (i % 6) for i in range(40)])  # fine, regular grid
    ready = forecast_service.check_series_forecastability(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live")
    assert ready["ready"] is True
    assert ready["n_observations"] >= 30
    assert ready["observed_interval_seconds"] == pytest.approx(1200.0)

    thin = forecast_service.check_series_forecastability(
        session, device_name="R9", metric="rtt", interface_name=None,
        data_source="live")
    assert thin["ready"] is False
    assert thin["reasons"]


def test_forecastability_daily_cadence_unlocks_mixed_window(session):
    seed_rollups(session, [50.0 + (i % 7) for i in range(40)],
                 start_day=dt.date(2026, 1, 1))
    seed_raw(session, [80.0 + (i % 20) for i in range(250)],
             start=dt.datetime(2026, 2, 10), step_s=300)
    common = {"device_name": "R1", "metric": "rtt",
              "interface_name": None, "data_source": "live"}
    raw_view = forecast_service.check_series_forecastability(
        session, cadence="raw", **common)
    assert raw_view["ready"] is False
    daily_view = forecast_service.check_series_forecastability(
        session, cadence="daily", **common)
    assert daily_view["ready"] is True
    assert daily_view["observed_interval_seconds"] == pytest.approx(86400.0)


def test_forecastability_rollup_only_ready_in_both_cadences(session):
    seed_rollups(session, [50.0 + (i % 5) for i in range(40)])
    for cadence in ("raw", "daily"):
        view = forecast_service.check_series_forecastability(
            session, device_name="R1", metric="rtt", interface_name=None,
            data_source="live", cadence=cadence)
        assert view["ready"] is True, cadence
        assert view["provenance"]["rollup"] == 40
