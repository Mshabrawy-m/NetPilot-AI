"""Service-level tests for the enhanced forecast pipeline (Phase 2 v2).

Synthetic DB fixtures only. What is verified here is the *orchestration*
contract rather than the maths: which numbers reach the database, which stay an
in-memory comparison result, and what is reported as unavailable instead of
being filled in with a plausible-looking value.
"""
from __future__ import annotations

import datetime as dt
import json
import math

import pytest

from database import repository
from services import forecast_service

BASE = dt.datetime(2026, 1, 1, 0, 0, 0)
STEP_S = 1200                     # 20-minute sampling grid
#: NETPILOT_SEASON_HOURS is clamped to >= 1.0 by config, so on this grid one
#: season = 3 samples - the shortest season a model will accept.
SEASON_HOURS = "1"
MODELS = ("naive", "seasonal_naive", "holt_damped", "holt_winters")

#: 12 cycles of a level trend (+2 per cycle) plus a 3-phase rhythm: long enough
#: for the FORECAST readiness gate (30 samples) and for a 7-sample holdout.
SEASONAL = [50.0 + 2.0 * (i // 3) + [0.0, 8.0, -4.0][i % 3] for i in range(36)]


def seed(session, values, *, device="R1", metric="rtt", source="live",
         interface=None, step_s=STEP_S, unit="ms"):
    for i, v in enumerate(values):
        repository.add_metric_sample(
            session, device, metric, v, unit=unit, interface_name=interface,
            data_source=source,
            collected_at=BASE + dt.timedelta(seconds=i * step_s))
    session.commit()


def compare(session, monkeypatch, **kwargs):
    """One comparison run on the seasonal fixture, with the season length that
    grid can actually support."""
    monkeypatch.setenv("NETPILOT_SEASON_HOURS", kwargs.pop("season_hours",
                                                           SEASON_HOURS))
    kwargs.setdefault("horizon", 6)
    kwargs.setdefault("models", MODELS)
    return forecast_service.run_forecast_comparison(
        session, device_name="R1", metric="rtt", interface_name=None,
        data_source="live", **kwargs)


def run_for(result, name) -> dict:
    rows = [r for r in result["runs"] if r.get("model") == name]
    assert len(rows) == 1, f"{name} must appear exactly once"
    return rows[0]


def stored(session) -> dict[str, object]:
    return {r.model_name: r for r in repository.get_forecast_runs(
        session, limit=50)}


# ------------------------------------------------------------ new metrics
def test_scale_free_metrics_are_persisted_for_every_model(session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch)
    assert result["status"] == "ok"
    ok = [r for r in result["runs"] if r["status"] == "ok"]
    assert len(ok) == len(MODELS), "all four models are defensible here"
    rows = stored(session)
    assert set(rows) == set(MODELS)
    for r in ok:
        row = rows[r["model"]]
        assert row.mape == pytest.approx(r["mape"])
        assert row.smape == pytest.approx(r["smape"])
        # values ~50-100 with real errors: every metric is defensible, so none
        # of these may be NULL and none may be a fabricated zero
        assert row.mase is not None and row.mase > 0.0
        assert row.skill is not None
        assert 0.0 <= row.mape <= 200.0
        assert 0.0 <= row.smape <= 200.0
        assert row.band_meta_json


def test_naive_skill_is_zero_by_definition_and_others_are_measured_against_it(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch)
    rows = stored(session)
    naive_mae = rows["naive"].mae
    assert rows["naive"].skill == pytest.approx(0.0)
    assert result["best_model"] != "naive"
    assert result["best_criterion"] == "holdout MAE"
    # a model evaluated on the whole holdout has an exactly reproducible skill
    for name in ("holt_damped", "holt_winters"):
        assert run_for(result, name)["n_eval"] == len(result["split"].test)
        assert rows[name].skill == pytest.approx(
            1.0 - rows[name].mae / naive_mae, abs=1e-9)
    # seasonal_naive only reaches one season ahead, so its skill is computed on
    # the timestamps the naive baseline and it BOTH cover - a smaller sample,
    # which is why it must not be back-derived from the full-holdout MAEs
    sn = run_for(result, "seasonal_naive")
    assert sn["n_eval"] < len(result["split"].test)
    assert rows["seasonal_naive"].skill > 0.0
    assert rows["seasonal_naive"].skill != pytest.approx(
        1.0 - rows["seasonal_naive"].mae / naive_mae, abs=1e-3)


def test_holt_winters_earns_its_place_against_the_season_blind_models(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch)
    hw = run_for(result, "holt_winters")
    naive = run_for(result, "naive")
    damped = run_for(result, "holt_damped")
    assert hw["mae"] < naive["mae"], (
        "the seasonal model must beat a baseline that copies one flat value")
    assert hw["mae"] < damped["mae"], (
        "level+trend alone (damped Holt) cannot see the rhythm either")
    assert hw["skill"] > 0.0
    cfg = json.loads(stored(session)["holt_winters"].model_config_json)
    assert cfg["season_len"] == 3
    assert cfg["tuned_on"] == "train"
    assert {"alpha", "beta", "gamma", "phi", "season_hours"} <= set(cfg)


# ---------------------------------------------------------------- bands
def test_band_metadata_records_the_measured_width_and_coverage(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch)
    r = run_for(result, "naive")
    meta = r["band"]
    assert meta["kind"] == forecast_service.BAND_MODE == "empirical_sqrt_h"
    assert meta["nominal_coverage"] == pytest.approx(0.95)
    assert meta["n_errors"] == r["n_eval"]
    assert meta["half_width"] > 0.0
    assert 0.0 <= meta["measured_coverage"] <= 1.0
    assert r["band_nominal_coverage"] == pytest.approx(0.95)
    widths = [f["upper_bound"] - f["predicted_value"] for f in r["future"]]
    assert widths == sorted(widths), "the band must widen with the horizon"
    assert len(set(widths)) == len(widths)
    assert widths[-1] == pytest.approx(widths[0] * math.sqrt(r["horizon_steps"]))
    for f in r["future"]:
        assert f["lower_bound"] <= f["predicted_value"] <= f["upper_bound"]
        # the nominal target and the measured achievement stay side by side
        assert meta["nominal_coverage"] == pytest.approx(0.95)


def test_band_meta_survives_the_database_round_trip(session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch)
    for r in (x for x in result["runs"] if x["status"] == "ok"):
        meta = json.loads(stored(session)[r["model"]].band_meta_json)
        assert meta["kind"] == r["band"]["kind"]
        assert meta["nominal_coverage"] == pytest.approx(
            r["band"]["nominal_coverage"])
        assert meta["half_width"] == pytest.approx(r["band"]["half_width"])


def test_a_requested_coverage_is_clamped_into_the_defensible_range(
        session, monkeypatch):
    seed(session, SEASONAL)
    for requested, expected in ((0.9999, 0.999), (0.05, 0.5), (0.8, 0.8),
                                (None, 0.95)):
        result = compare(session, monkeypatch, persist=False,
                         band_coverage=requested, models=("naive",))
        assert result["band_nominal_coverage"] == pytest.approx(expected)
    lenient = compare(session, monkeypatch, persist=False, models=("naive",),
                      band_coverage=0.5)
    strict = compare(session, monkeypatch, persist=False, models=("naive",),
                     band_coverage=0.999)
    assert (run_for(lenient, "naive")["band"]["half_width"]
            <= run_for(strict, "naive")["band"]["half_width"])


def test_band_mode_can_keep_the_documented_approximation(session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch, persist=False,
                     band_mode="normal_sqrt_h", models=("naive",))
    meta = run_for(result, "naive")["band"]
    assert meta["kind"] == "normal_sqrt_h"
    assert meta["half_width"] is None
    assert meta["measured_coverage"] is not None, (
        "even untouched bands report the coverage they actually achieved")


def test_best_model_and_ranking_agree_with_the_stored_errors(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch)
    rows = forecast_service.model_ranking(result["runs"], result["ensemble"])
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert [r["mae"] for r in rows] == sorted(r["mae"] for r in rows)
    blend = [r for r in rows if r["model"].endswith(" (blend)")]
    assert len(blend) == 1
    assert blend[0]["persisted"] is False
    assert blend[0]["reason"] == "in-memory blend, not a stored run"
    # best_model is the best REAL stored run: the blend is ranked for comparison
    # but never replaces a model that can be reproduced from its own config
    real = [r for r in rows if r["persisted"]]
    assert min(real, key=lambda r: r["mae"])["model"] == result["best_model"]
    assert all(r["band"] == "empirical_sqrt_h" for r in rows)


# -------------------------------------------------------------- ensemble
def test_ensemble_is_computed_on_the_same_holdout_but_never_persisted(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch)
    ens = result["ensemble"]
    assert ens["status"] == "ok"
    assert ens["persisted"] is False
    assert len(result["runs"]) == len(MODELS), (
        "the ensemble is a comparison result, not a fifth run")
    assert forecast_service.ENSEMBLE_NAME not in stored(session)
    members = ens["members"]
    assert len(members) >= 2
    assert sum(m["weight"] for m in members) == pytest.approx(1.0)
    ordered = sorted(members, key=lambda m: m["mae"])
    weights = [m["weight"] for m in ordered]
    assert weights == sorted(weights, reverse=True), (
        "the more accurate member must carry at least as much weight")
    assert all(m["weight"] > 0.0 for m in members), (
        "the documented ridge keeps every member in the blend")
    assert ens["best_member"] == ordered[0]["model"]
    assert ens["n_eval"] == len(result["split"].test)
    assert ens["test_timestamps"] == list(result["split"].test.index)
    assert ens["note"].startswith("in-memory comparison result")


def test_ensemble_abstains_when_only_one_model_is_compared(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch, models=("naive",), persist=False)
    ens = result["ensemble"]
    assert ens["status"] == "not_applicable"
    assert "at least two" in ens["reason"]
    assert result["best_model"] == "naive"


def test_ensemble_can_be_switched_off(session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch, ensemble=False, persist=False)
    assert result["ensemble"]["status"] == "disabled"


def test_a_seasonal_member_with_a_gap_renormalises_instead_of_guessing(
        session, monkeypatch):
    """seasonal_naive can only reach one season ahead, so most of its holdout
    predictions are NaN - the blend must drop those steps, not treat them as 0.
    """
    seed(session, SEASONAL)
    result = compare(session, monkeypatch)
    sn = run_for(result, "seasonal_naive")
    n_test = len(result["split"].test)
    assert sn["n_eval"] < n_test, "a 3-sample season can only reach 3 steps"
    assert sn["mape"] is not None
    ens = result["ensemble"]
    assert all(not math.isnan(p) for p in ens["test_pred"]), (
        "a member's gap must be renormalised away, never propagated as NaN")


# ------------------------------------------------------- honest refusals
def test_seasonal_models_refuse_out_loud_and_the_refusal_is_stored(
        session, monkeypatch):
    # A 24 h season on a 20-minute grid needs 72 samples per season: the model
    # cannot claim a rhythm from 47 training points, so it says so.
    seed(session, SEASONAL)
    result = compare(session, monkeypatch, season_hours="24")
    for name in ("seasonal_naive", "holt_winters"):
        r = run_for(result, name)
        assert r["status"] == "skipped"
        assert r["reason"] and "mape" not in r
        row = stored(session)[name]
        assert row.status == "insufficient_data"
        assert row.message == r["reason"]
        assert row.mae is None and row.mape is None
        assert row.band_meta_json is None
    assert run_for(result, "naive")["status"] == "ok"
    assert result["best_model"] in ("naive", "holt_damped")


def test_an_unknown_model_is_reported_rather_than_dropped(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch,
                     models=("naive", "not_a_model"))
    assert run_for(result, "not_a_model")["status"] == "unknown_model"
    assert stored(session).keys() == {"naive"}


def test_skill_is_none_when_no_naive_reference_was_compared(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch, models=("holt_damped",))
    assert run_for(result, "holt_damped")["skill"] is None
    assert stored(session)["holt_damped"].skill is None


def test_thin_data_short_circuits_before_any_model_runs(session, monkeypatch):
    seed(session, [50.0, 51.0, 49.0])
    result = compare(session, monkeypatch)
    assert result["status"] == "insufficient_data"
    assert result["runs"] == []
    assert repository.get_forecast_runs(session) == []


# ------------------------------------------------- crossings and warnings
def _fake_run(model, steps, *, interval=60.0, status="ok", kind="empirical"):
    grid = interval or 60.0      # timestamps still exist when the interval is
    return {                     # unknown: only the ETA claim is dropped
        "model": model, "status": status, "interval_seconds": interval,
        "mae": 1.5, "band": {"kind": kind},
        "future": [{"step": s,
                    "target_timestamp": BASE + dt.timedelta(seconds=grid * s),
                    "predicted_value": v, "lower_bound": lo, "upper_bound": hi}
                   for s, v, lo, hi in steps],
    }


def test_threshold_crossing_separates_a_possible_call_from_a_certain_one():
    run = _fake_run("naive", [(1, 90.0, 85.0, 95.0), (2, 105.0, 95.0, 115.0),
                              (3, 130.0, 125.0, 135.0)])
    out = forecast_service.threshold_crossing([run], threshold=100.0)
    assert len(out) == 1
    row = out[0]
    # the point estimate crosses at 2, but only step 3 survives its own band
    assert row["first_possible_step"] == 2
    assert row["first_certain_step"] == 3
    assert row["certainty"] == "certain"
    assert row["step"] == 3
    assert row["eta_seconds"] == pytest.approx(180.0)
    assert "band-clear" in row["note"]
    assert row["band_kind"] == "empirical"


def test_threshold_crossing_downgrades_to_possible_when_the_band_overlaps():
    run = _fake_run("naive", [(1, 90.0, 85.0, 95.0), (2, 101.0, 99.0, 103.0)])
    row = forecast_service.threshold_crossing([run], threshold=100.0)[0]
    assert row["certainty"] == "possible"
    assert row["first_certain_step"] is None
    assert row["step"] == 2 and row["eta_seconds"] == pytest.approx(120.0)
    assert "still overlaps the threshold" in row["note"]


def test_threshold_crossing_respects_the_direction_and_says_nothing_else():
    falling = _fake_run("naive", [(1, 40.0, 35.0, 45.0), (2, 12.0, 8.0, 14.0)])
    below = forecast_service.threshold_crossing([falling], threshold=15.0,
                                               direction="below")[0]
    assert below["certainty"] == "certain" and below["step"] == 2
    assert below["eta_seconds"] == pytest.approx(120.0)
    # the same numbers answer the opposite question at the opposite end
    above = forecast_service.threshold_crossing([falling], threshold=15.0)[0]
    assert above["direction"] == "above" and above["step"] == 1
    clean = _fake_run("naive", [(1, 10.0, 8.0, 12.0)])
    assert forecast_service.threshold_crossing([clean], threshold=100.0) == []


def test_threshold_crossing_ignores_refused_runs_and_unknown_intervals():
    refused = _fake_run("seasonal_naive", [(1, 999.0, 900.0, 1100.0)],
                        status="skipped")
    assert forecast_service.threshold_crossing([refused],
                                               threshold=10.0) == []
    no_interval = _fake_run("naive", [(1, 120.0, 110.0, 130.0)], interval=None)
    row = forecast_service.threshold_crossing([no_interval], threshold=100.0)[0]
    assert row["eta_seconds"] is None, (
        "an unknown interval cannot support a time-to-crossing claim")


def test_warnings_carry_the_certainty_and_the_model_error_next_to_them(
        session, monkeypatch):
    seed(session, SEASONAL)
    result = compare(session, monkeypatch, models=("naive",), persist=False)
    warnings = forecast_service.forecast_warnings(result["runs"],
                                                 threshold=60.0,
                                                 direction="above")
    assert warnings
    for w in warnings:
        assert w["kind"] == "forecast-based warning"
        assert w["certainty"] in ("certain", "possible")
        assert w["model_mae"] is not None
        assert w["eta_seconds"] == pytest.approx(
            w["step"] * result["quality"].observed_interval_seconds)
        assert "not an" in w["note"]
    # a threshold every band clears is reported as certain, never as a hunch
    certain = forecast_service.forecast_warnings(
        [_fake_run("naive", [(1, 500.0, 490.0, 510.0)])], threshold=100.0)
    assert [w["certainty"] for w in certain] == ["certain"]
    assert forecast_service.forecast_warnings(
        [_fake_run("naive", [(1, 105.0, 90.0, 120.0)])], threshold=100.0)[0][
        "certainty"] == "possible"


# --------------------------------------------------- realised band coverage
def test_realized_coverage_is_none_until_time_verifies_a_band(
        session, monkeypatch):
    seed(session, SEASONAL)
    compare(session, monkeypatch, models=("naive",), horizon=4)
    run = stored(session)["naive"]
    res = forecast_service.realized_coverage(session, run.id)
    assert res["n_total"] == 4 and res["n_checked"] == 0
    assert res["n_unverified"] == 4
    assert res["coverage"] is None, (
        "an unverified band is never scored as a hit")
    assert res["mae"] is None and res["bias"] is None
    assert res["band_kind"] == "empirical_sqrt_h"
    assert res["nominal_coverage"] == pytest.approx(0.95)
    assert res["coverage_gap"] is None


def test_realized_coverage_counts_only_the_predictions_time_confirmed(
        session, monkeypatch):
    seed(session, SEASONAL)
    compare(session, monkeypatch, models=("naive",), horizon=4)
    run = stored(session)["naive"]
    preds = repository.get_forecast_predictions(session, run.id)
    half = preds[0].upper_bound - preds[0].predicted_value
    # step 1 lands exactly on its prediction (inside), step 2 misses by 10x the
    # measured half-width (outside); steps 3-4 have not arrived yet.
    repository.add_metric_sample(session, "R1", "rtt",
                                 preds[0].predicted_value, data_source="live",
                                 collected_at=preds[0].target_timestamp)
    repository.add_metric_sample(
        session, "R1", "rtt", preds[1].predicted_value + 10 * half,
        data_source="live", collected_at=preds[1].target_timestamp)
    session.commit()
    assert forecast_service.backfill_actuals(session, run.id) == 2
    res = forecast_service.realized_coverage(session, run.id)
    assert res["n_checked"] == 2 and res["n_inside"] == 1
    assert res["coverage"] == pytest.approx(0.5)
    assert res["n_unverified"] == 2
    assert res["coverage_gap"] == pytest.approx(0.5 - 0.95)
    assert res["mae"] == pytest.approx(5 * half)
    assert res["bias"] == pytest.approx(-5 * half)
    assert res["holdout_measured_coverage"] is not None
