"""AI Lab experiment tasks: labels, holdouts, metric edges, safe refusals.

Covers acceptance criteria 8.7 (models scored on the same holdout), 8.8 (metric
correctness incl. edge cases), 8.9 (anomaly detection with and without ground
truth), 8.10 (classification blocked when labels/samples are insufficient),
8.11 (an individual model failure cannot poison a comparison) and the
statistical-analysis honesty rules.

Offline and deterministic: the simulator supplies the labelled fixture, and the
only "external service" involved is absent by design.
"""
from __future__ import annotations

import datetime as dt
import statistics

import pandas as pd
import pytest

from analytics import lab_dataset, lab_simulator
from services import lab_tasks

BASE = dt.datetime(2026, 10, 1)
LABEL_METRIC = "ping.avg_rtt_ms"
FEATURE_METRICS = ("if.rx_bps", "if.tx_bps", LABEL_METRIC, "ping.loss_pct",
                   "cpu.usage_pct")
CLS_FEATURES = ("if.rx_bps", "if.tx_bps", "ping.loss_pct", "cpu.usage_pct")


@pytest.fixture(scope="module")
def sim() -> lab_dataset.LabDataset:
    """One deterministic labelled simulation reused by every test below."""
    return lab_simulator.simulate(
        devices=("R1", "R2"), duration_hours=6.0, interval_seconds=120,
        seed=3, events={"congestion": 2, "packet_loss": 2, "latency_spike": 2},
        metrics=FEATURE_METRICS)


def ref_of(dataset, device, metric, interface="eth0"):
    return lab_dataset.SeriesRef(device, metric, interface)


def build(values, *, metric="series", unit="count", labels=None,
          source_type=lab_dataset.SOURCE_HISTORICAL, source_id="fixture",
          step_seconds=60, start=BASE):
    """Canonicalise one synthetic series into a dataset (fixture helper)."""
    rows = [{"collected_at": start + dt.timedelta(seconds=step_seconds * i),
             "device": "R1", "interface": "eth0", "metric": metric,
             "value": v, "unit": unit,
             "label": (labels[i] if labels else None)}
            for i, v in enumerate(values)]
    return lab_dataset.canonicalize(rows, source_type=source_type,
                                    source_id=source_id)


def history_map(points, series):
    """Actual values per timestamp as the chart points carry them."""
    for item in points:
        if item["kind"] == "history" and item["series"] == series:
            return {p["t"]: p["v"] for p in item["points"]}
    raise AssertionError(f"no history points returned for {series}")


def row_for(result, series, model):
    return next(r for r in result.table
                if r.get("series") == series and r.get("model") == model)


# ==========================================================================
# 8.7 forecasting: one holdout, several models, past vs future kept apart
# ==========================================================================
def test_every_model_is_scored_on_the_identical_holdout_timestamps(sim):
    refs = [ref_of(sim, "R1", m) for m in ("if.rx_bps", LABEL_METRIC)]
    res = lab_tasks.run_forecast_comparison(sim, refs, horizon=6,
                                            models=("naive", "holt_damped"))
    assert res.status == "ok"
    assert res.metrics["identical_holdout"] is True
    for series in (str(refs[0]), str(refs[1])):
        # only an independently fitted model carries its own split counts; the
        # blend row is a comparison result and is excluded on purpose
        scored = [r for r in res.table if r.get("series") == series
                  and r.get("status") == "ok" and r.get("mae") is not None
                  and r.get("n_test") is not None]
        assert scored, "no model was scored for " + series
        assert len({r["n_test"] for r in scored}) == 1
        blend = [r for r in res.table if r.get("series") == series
                 and "not_persisted" in r]
        assert blend and all("n_test" not in r for r in blend)
        boundary = next(p for p in res.points if p["kind"] == "split_boundary"
                        and p["series"] == series)
        assert boundary["train_end"] < boundary["test_start"]
        # every model's holdout is the same timestamp list, in the same order
        stamps = [tuple(p["t"] for p in item["points"]) for item in res.points
                  if item["kind"] == "holdout" and item["series"] == series]
        assert stamps and len(set(stamps)) == 1
        split = res.artifacts["per_series"][series]["split"]
        assert split["test"] == scored[0]["n_test"]
        assert split["train"] + split["val"] + split["test"] <= \
            sim.series_frame(refs[0]).shape[0]


def test_holdout_is_backtesting_and_the_future_path_is_separate(sim):
    ref = ref_of(sim, "R1", LABEL_METRIC)
    series = str(ref)
    res = lab_tasks.run_forecast_comparison(sim, [ref], horizon=5,
                                            models=("naive",))
    actual = history_map(res.points, series)
    holdout = next(p for p in res.points if p["kind"] == "holdout"
                   and p["series"] == series)
    future = next(p for p in res.points if p["kind"] == "future"
                  and p["series"] == series)
    # the holdout timestamps are recorded history, the future ones are not
    assert all(p["t"] in actual for p in holdout["points"])
    assert all(p["t"] not in actual for p in future["points"])
    last_seen = max(actual)
    assert min(p["t"] for p in future["points"]) > last_seen
    assert any("must not be read as one line" in w for w in res.warnings)
    assert res.metrics["band_nominal_coverage"] == pytest.approx(0.95)
    assert res.metrics["band_mode"]


def test_forecast_metrics_are_recomputable_from_the_returned_points(sim):
    refs = [ref_of(sim, "R1", "if.rx_bps")]
    res = lab_tasks.run_forecast_comparison(sim, refs, horizon=6,
                                            models=("naive", "holt_damped"))
    series = str(refs[0])
    actual = history_map(res.points, series)
    for item in (p for p in res.points if p["kind"] == "holdout"
                 and p["series"] == series):
        row = row_for(res, series, item["model"])
        pairs = [(actual[p["t"]], p["v"]) for p in item["points"]
                 if p["v"] is not None]
        assert pairs
        errors = [abs(a - f) for a, f in pairs]
        mae = sum(errors) / len(errors)
        rmse = (sum(e * e for e in errors) / len(errors)) ** 0.5
        assert row["mae"] == pytest.approx(mae, abs=2e-3)
        assert row["rmse"] == pytest.approx(rmse, abs=2e-3)
        assert row["rmse"] >= row["mae"]
        assert row["n_eval"] == len(pairs)
    # the naive baseline defines skill 0; another model is scored against it
    naive = row_for(res, series, "naive")
    assert naive["skill"] == 0.0
    other = row_for(res, series, "holt_damped")
    assert other["skill"] is None or isinstance(other["skill"], float)


def test_constant_series_reports_zero_error_without_inventing_r2(sim):
    flat = build([5.0] * 80, metric="flat")
    res = lab_tasks.run_forecast_comparison(flat, flat.series_refs(), horizon=4,
                                            models=("naive",))
    row = row_for(res, str(flat.series_refs()[0]), "naive")
    assert row["mae"] == 0.0 and row["rmse"] == 0.0
    assert row["r2"] is None          # variance is zero: no R² exists
    assert row["mase"] is None        # in-sample MAE is zero: MASE undefined
    assert row["band"]["half_width"] == 0.0
    assert row["band"]["measured_coverage"] == 1.0
    assert any("MASE/sMAPE are reported whenever" in w for w in res.warnings)


def test_a_series_of_zeros_reports_no_percentage_or_scaled_error():
    """MAPE/sMAPE/MASE/R2 are undefined on all-zero actuals, never 0 %."""
    series = build([0.0] * 80, metric="zeros")
    res = lab_tasks.run_forecast_comparison(series, series.series_refs(),
                                            horizon=4, models=("naive",))
    assert res.status == "ok"
    row = row_for(res, str(series.series_refs()[0]), "naive")
    assert row["n_eval"] > 0 and row["mae"] == 0.0 and row["rmse"] == 0.0
    assert row["mape"] is None          # every actual is zero: nothing survives
    assert row["smape"] is None         # the |a|+|p| denominator is zero too
    assert row["mase"] is None and row["r2"] is None
    assert row["skill"] == 0.0          # naive against itself, on real pairs


def test_mape_skips_zero_actuals_instead_of_diluting_or_nulling_them():
    values = [0.0, 10.0] * 24           # 48 points: passes the forecast gate
    series = build(values, metric="onoff")
    res = lab_tasks.run_forecast_comparison(series, series.series_refs(),
                                            horizon=4, models=("naive",))
    assert res.status == "ok"
    name = str(series.series_refs()[0])
    row = row_for(res, name, "naive")
    actual = history_map(res.points, name)
    holdout = next(p for p in res.points if p["kind"] == "holdout"
                   and p["series"] == name)
    pairs = [(actual[p["t"]], p["v"]) for p in holdout["points"]
             if p["v"] is not None]
    nonzero = [(a, f) for a, f in pairs if abs(a) > 1e-9]
    assert pairs and nonzero and len(nonzero) < len(pairs)
    expected = sum(abs(a - f) / abs(a) for a, f in nonzero) / len(nonzero) * 100
    assert row["mape"] == pytest.approx(expected, abs=2e-2)
    # the metric is a number here, so it must not be quietly nulled, and the
    # warning has to tell the reader that zero actuals were left out
    assert row["mape"] is not None
    assert any("MAPE skips every point whose actual value is" in w
               for w in res.warnings)


def test_short_series_is_refused_with_the_threshold_named():
    series = build([float(i) for i in range(20)], metric="short")
    res = lab_tasks.run_forecast_comparison(series, series.series_refs(),
                                            horizon=6)
    assert res.status == "insufficient_data"
    assert res.metrics["reason"] == "insufficient_data"
    assert "insufficient data" in res.message
    # the threshold that was missed is stated, not just "not enough data"
    assert "Precondition: min_points_forecast = 30" in res.warnings
    assert res.table == [] and res.points == []


def test_unknown_model_is_refused_before_anything_is_fitted(sim):
    res = lab_tasks.run_forecast_comparison(sim, [ref_of(sim, "R1", "if.rx_bps")],
                                            models=("crystal_ball",))
    assert res.status == "refused"
    assert res.metrics == {"reason": "unknown_model"}
    assert "crystal_ball" in res.message and "naive" in res.message
    assert res.table == []


def test_one_broken_model_cannot_poison_the_comparison(sim, monkeypatch):
    """Criterion 8.11: a raising model becomes an error row; the rest survive."""
    from analytics import forecasting

    def explode(*args, **kwargs):
        raise RuntimeError("simulated estimator crash")

    refs = [ref_of(sim, "R1", "if.rx_bps")]
    monkeypatch.setitem(forecasting.FORECAST_MODELS, "holt_damped", explode)
    res = lab_tasks.run_forecast_comparison(
        sim, refs, horizon=6, models=("naive", "holt_damped"), ensemble=False)
    assert res.status == "ok"
    broken = row_for(res, str(refs[0]), "holt_damped")
    assert broken["status"] == "error"
    assert "simulated estimator crash" in broken["reason"]
    healthy = row_for(res, str(refs[0]), "naive")
    assert healthy["status"] == "ok" and healthy["mae"] is not None
    assert res.artifacts["per_series"][str(refs[0])]["best_model"] == "naive"
    assert res.metrics["scored_models"] == 1


def test_ensemble_is_reported_as_a_blend_not_an_independent_fit(sim):
    refs = [ref_of(sim, "R1", "if.rx_bps")]
    res = lab_tasks.run_forecast_comparison(sim, refs, horizon=6)
    blend = next(r for r in res.table if str(r["model"]).startswith("ensemble"))
    assert blend["not_persisted"] == (
        "the blend is a comparison result over the other runs, not an "
        "independently fitted model")
    ens = res.artifacts["per_series"][str(refs[0])]["ensemble"]
    assert ens["method"].startswith("inverse holdout-MAE weights")
    assert sum(member["weight"] for member in ens["members"]) == pytest.approx(1.0)


# ==========================================================================
# 8.9 anomaly comparison: supervised only when the source really has labels
# ==========================================================================
def test_labelled_source_yields_supervised_detector_scores(sim):
    refs = [ref_of(sim, "R1", m) for m in ("if.rx_bps", LABEL_METRIC,
                                           "cpu.usage_pct")]
    res = lab_tasks.run_anomaly_comparison(
        sim, refs, detectors=("rolling_mad", "rolling_zscore",
                              "isolation_forest"))
    assert res.status == "ok"
    assert res.metrics["supervised"] is True
    assert res.metrics["labels_available"] is True
    assert res.metrics["detectors_compared"] == 3
    assert res.metrics["series_evaluated"] == 3
    for row in res.table:
        if row["detector"] == "(agreement)":
            continue
        assert row["status"] == "ok"
        assert {"tp", "fp", "fn", "precision", "recall", "f1"} <= set(row)
        assert row["label_basis"]
    assert res.metrics["best_by_f1"] in {r["detector"] for r in res.table}
    assert res.metrics["total_flags"] == sum(
        r["n_flagged"] for r in res.table
        if r["detector"] != "(agreement)" and r["status"] == "ok")
    assert any("SYNTHETIC ground truth" in w for w in res.warnings)
    # charts are built from computed points only, and every flagged timestamp
    # is one of the series' own timestamps
    timestamps = {pd.Timestamp(ts).isoformat() for ts in
                  sim.series_frame(refs[0])["collected_at"]}
    for point in res.points:
        if point["kind"] == "flags":
            assert all(ts in timestamps for ts in point["points"])


def test_unlabelled_source_reports_evidence_but_never_accuracy(sim):
    plain = build([10.0 + (i % 7) + (60.0 if i == 41 else 0.0)
                   for i in range(60)], metric="rtt", unit="ms")
    res = lab_tasks.run_anomaly_comparison(plain, plain.series_refs())
    assert res.status == "ok"
    assert res.metrics["supervised"] is False
    assert res.metrics["labels_available"] is False
    assert res.metrics["best_by_f1"] is None
    assert "no usable anomaly/normal ground truth" in \
        res.metrics["accuracy_claims"]
    for row in res.table:
        if row["detector"] == "(agreement)":
            continue
        for key in ("tp", "fp", "fn", "precision", "recall", "f1", "pr_auc"):
            assert row[key] is None
        assert row["unsupervised_only"]
        assert isinstance(row["n_flagged"], int)
    assert any("unsupervised evidence only" in w for w in res.warnings)
    assert "truth" not in {p["kind"] for p in res.points}


def test_detector_agreement_is_a_diagnostic_not_a_competitor(sim):
    refs = [ref_of(sim, "R1", "if.rx_bps"), ref_of(sim, "R1", LABEL_METRIC)]
    res = lab_tasks.run_anomaly_comparison(sim, refs)
    agree = [r for r in res.table if r["detector"] == "(agreement)"]
    assert len(agree) == 2                       # one per series
    assert {r["status"] for r in agree} == {"diagnostic"}
    assert res.metrics["detectors_compared"] == 2  # agreement is not counted
    assert agree[0]["pairwise_jaccard"]


def test_multivariate_pass_is_opt_in_and_abstains_without_two_metrics(sim):
    refs = [ref_of(sim, "R1", LABEL_METRIC)]
    off = lab_tasks.run_anomaly_comparison(sim, refs)
    assert not any(r["detector"] == "mv_isolation_forest" for r in off.table)
    on = lab_tasks.run_anomaly_comparison(
        sim, [ref_of(sim, "R1", m) for m in ("if.rx_bps", LABEL_METRIC)],
        include_multivariate=True)
    mv = [r for r in on.table if r["detector"] == "mv_isolation_forest"]
    assert len(mv) == 1
    assert mv[0]["status"] in ("ok", "insufficient_features")
    assert sorted(mv[0]["aligned_metrics"]) == ["if.rx_bps",
                                                "ping.avg_rtt_ms"]
    assert any("retrospective" in w for w in on.warnings)
    assert on.config["multivariate"] is True


def test_unknown_detector_is_refused_with_the_supported_list(sim):
    res = lab_tasks.run_anomaly_comparison(sim, [ref_of(sim, "R1", "if.rx_bps")],
                                           detectors=("tarot",))
    assert res.status == "refused"
    assert res.metrics == {"reason": "unknown_detector"}
    assert "isolation_forest" in res.message


def test_threshold_sweep_curve_accompanies_the_pr_auc(sim):
    ref = ref_of(sim, "R1", LABEL_METRIC)
    res = lab_tasks.run_anomaly_comparison(sim, [ref],
                                           detectors=("rolling_mad",))
    row = next(r for r in res.table if r["detector"] == "rolling_mad")
    assert row["curve"], "sweep curve is missing"
    for point in row["curve"]:
        assert point["setting"] == "mad_threshold"
        assert 0.0 <= point["f1"] <= 1.0
    assert "sweep" in row["pr_auc_method"]
    assert isinstance(res.artifacts["sweep_method"], str)
    assert "not a continuous-score PR-AUC" in res.artifacts["sweep_method"]
    assert res.config["sweep_curve"] is True


# ==========================================================================
# 8.10 fault classification: only ever run on real labels
# ==========================================================================
def test_classification_uses_the_injected_labels_and_reports_both_splits(sim):
    res = lab_tasks.run_fault_classification(
        sim, device="R1", interface="eth0", feature_metrics=CLS_FEATURES,
        label_metric=LABEL_METRIC, split_mode="stratified")
    assert res.status == "ok"
    assert res.metrics["n_features"] == len(CLS_FEATURES)
    assert res.metrics["n_train"] + res.metrics["n_test"] == \
        res.metrics["n_rows"]
    assert len(res.metrics["classes"]) >= 2
    assert res.metrics["best_model"] in {r["model"] for r in res.table}
    assert res.metrics["imbalance_ratio"] >= 1.0
    for row in res.table:
        if row["status"] != "ok":
            continue
        assert row["accuracy"] <= 1.0 and row["balanced_accuracy"] <= 1.0
        # the majority baseline of the TEST split, reported next to accuracy
        # so an imbalanced 86 % accuracy cannot be read as success
        assert row["majority_baseline"] == pytest.approx(
            max(c["support"] for c in row["per_class"]) / row["n_test"])
        assert {c["class"] for c in row["per_class"]} == set(
            res.metrics["classes"])
        # the imbalance-aware metrics, not just raw accuracy
        assert 0.0 <= row["macro_f1"] <= 1.0
    matrix = res.artifacts["artifacts"]["confusion_matrices"][
        res.metrics["best_model"]]
    assert matrix["orientation"].startswith("rows = true class")
    total = sum(sum(row) for row in matrix["matrix"])
    assert total == res.metrics["n_test"]
    assert {p["kind"] for p in res.points} == {"confusion"}
    assert sum(p["count"] for p in res.points
               if p["model"] == res.metrics["best_model"]) == \
        res.metrics["n_test"]
    assert res.config["preprocessing"]["imputation"] == "train-median"
    assert res.config["label_provenance"].startswith("supervised scores use "
                                                     "SYNTHETIC ground truth")


def test_chronological_split_is_the_default_and_states_its_protocol(sim):
    res = lab_tasks.run_fault_classification(
        sim, device="R1", interface="eth0", feature_metrics=CLS_FEATURES,
        label_metric=LABEL_METRIC)
    assert res.config["split_mode"] == "chronological"
    assert "no future information is available to the fit" in \
        res.config["split_note"]


def test_stratified_split_is_allowed_but_flagged_as_optimistic(sim):
    chrono = lab_tasks.run_fault_classification(
        sim, device="R1", interface="eth0", feature_metrics=CLS_FEATURES,
        label_metric=LABEL_METRIC)
    strat = lab_tasks.run_fault_classification(
        sim, device="R1", interface="eth0", feature_metrics=CLS_FEATURES,
        label_metric=LABEL_METRIC, split_mode="stratified")
    assert strat.config["split_note"].startswith("randomised, class-balanced")
    assert any("optimistic for time-ordered telemetry" in w
               for w in strat.warnings)
    assert not any("optimistic for time-ordered" in w for w in chrono.warnings)


def test_classification_refuses_a_source_without_labels():
    plain = lab_dataset.canonicalize(
        [{"collected_at": BASE + dt.timedelta(minutes=i), "device": "R1",
          "interface": "eth0", "metric": m, "value": float(i % 5),
          "unit": "ms", "label": None} for i in range(60) for m in
         ("rtt", "load")],
        source_type=lab_dataset.SOURCE_HISTORICAL, source_id="plain")
    res = lab_tasks.run_fault_classification(plain, device="R1",
                                             interface="eth0",
                                             feature_metrics=("load",),
                                             label_metric="rtt")
    # "no labels" is a distinct status from "not enough rows", and the reason
    # is carried in metrics so the UI can branch on it
    assert res.status == "no_labels"
    assert res.metrics == {"reason": "no_labels"}
    assert "no accuracy is reported" in res.message
    # the refusal points at the legitimate ways to get labels instead of
    # offering a fallback that would fabricate them
    assert "Choose the simulated source" in res.message
    assert res.table == [] and res.points == []


@pytest.mark.parametrize("kwargs,reason", [
    ({"device": "R9", "interface": "eth0", "feature_metrics": CLS_FEATURES,
      "label_metric": LABEL_METRIC}, "unknown_series"),
    ({"device": "R1", "interface": "eth0",
      # the label metric is also in the feature list: the model would be
      # handed the answer as an input
      "feature_metrics": FEATURE_METRICS, "label_metric": LABEL_METRIC},
     "label_in_features"),
])
def test_classification_refuses_impossible_setups(sim, kwargs, reason):
    res = lab_tasks.run_fault_classification(sim, **kwargs)
    assert res.status == "refused"
    assert res.metrics == {"reason": reason}
    assert res.table == []


def test_classification_refuses_one_class_and_thin_classes():
    single = lab_dataset.canonicalize(
        [{"collected_at": BASE + dt.timedelta(minutes=i), "device": "R1",
          "interface": "eth0", "metric": m, "value": float(i % 4),
          "unit": "ms", "label": "congestion"}
         for i in range(40) for m in ("rtt", "load")],
        source_type=lab_dataset.SOURCE_SIMULATED, source_id="single")
    res = lab_tasks.run_fault_classification(single, device="R1",
                                             interface="eth0",
                                             feature_metrics=("load",),
                                             label_metric="rtt")
    assert res.status == "insufficient_data"
    assert res.metrics == {"reason": "single_class"}
    assert "at least two distinct fault classes" in res.message
    assert "Precondition: classes_found = ['congestion']" in res.warnings

    thin = lab_dataset.canonicalize(
        [{"collected_at": BASE + dt.timedelta(minutes=i), "device": "R1",
          "interface": "eth0", "metric": m, "value": float(i % 5),
          "unit": "ms", "label": ("fault" if i == 39 else "normal")}
         for i in range(60) for m in ("rtt", "load")],
        source_type=lab_dataset.SOURCE_SIMULATED, source_id="thin")
    res = lab_tasks.run_fault_classification(thin, device="R1",
                                             interface="eth0",
                                             feature_metrics=("load",),
                                             label_metric="rtt")
    assert res.status == "insufficient_data"
    assert res.metrics["reason"] == "class_too_thin"
    # the minimum is stated in the message and as a structured precondition,
    # so the operator knows what to add rather than that it failed
    assert "fewer than 4 examples" in res.message
    assert "Precondition: min_rows_per_class = 4" in res.warnings
    assert res.table == [] and res.points == []


def test_unlabelled_rows_are_dropped_and_disclosed_not_given_a_class():
    """A missing label costs one row and a note; it never becomes a guessed one."""
    rows = []
    for i in range(60):
        for m in ("rtt", "load"):
            rows.append({"collected_at": BASE + dt.timedelta(minutes=i),
                         "device": "R1", "interface": "eth0", "metric": m,
                         "value": float(i % 5), "unit": "ms",
                         "label": (None if i == 30 else
                                   ("fault" if i % 9 == 0 else "normal"))})
    dataset = lab_dataset.canonicalize(
        rows, source_type=lab_dataset.SOURCE_SIMULATED, source_id="partial")
    res = lab_tasks.run_fault_classification(dataset, device="R1",
                                             interface="eth0",
                                             feature_metrics=("load",),
                                             label_metric="rtt")
    assert res.status == "ok"
    assert res.metrics["n_rows"] == 59        # the unlabelled row is gone
    assert any("had no label at that timestamp" in w for w in res.warnings)
    # the classes counted here are the ones the source supplied; the dropped
    # row is absent from every per-class support total
    row = next(r for r in res.table if r["status"] == "ok")
    assert sum(c["support"] for c in row["per_class"]) == row["n_test"]
    assert res.metrics["n_rows"] == sum(res.metrics["class_counts"].values())
    # every scored row is a row the source actually labelled
    assert res.metrics["n_train"] + res.metrics["n_test"] == \
        res.metrics["n_rows"]


def test_classification_model_failure_becomes_an_error_row(sim, monkeypatch):
    from sklearn import linear_model

    def explode(*args, **kwargs):
        raise RuntimeError("simulated sklearn failure")

    monkeypatch.setattr(linear_model, "LogisticRegression", explode)
    res = lab_tasks.run_fault_classification(
        sim, device="R1", interface="eth0", feature_metrics=CLS_FEATURES,
        label_metric=LABEL_METRIC, split_mode="stratified")
    broken = next(r for r in res.table if r["model"] == "logistic_regression")
    assert broken["status"] == "error"
    assert "simulated sklearn failure" in broken["reason"]
    survivor = next(r for r in res.table if r["model"] == "random_forest")
    assert survivor["status"] == "ok"
    assert res.metrics["best_model"] == "random_forest"


def test_lag_features_are_recorded_as_effective_features(sim):
    res = lab_tasks.run_fault_classification(
        sim, device="R1", interface="eth0",
        feature_metrics=("if.rx_bps", "ping.loss_pct"),
        label_metric=LABEL_METRIC, split_mode="stratified", add_lags=2)
    assert res.status == "ok"
    assert "if.rx_bps__lag1" in res.effective_features
    assert "ping.loss_pct__lag2" in res.effective_features
    assert res.metrics["n_features"] == 6
    assert res.metrics["n_rows"] == 178      # 2 rows lost per lag step


# ==========================================================================
# statistical analysis: description and association, never causation
# ==========================================================================
def test_statistics_describe_the_series_exactly_as_stored():
    series = build([float(i) for i in range(1, 41)], metric="level")
    res = lab_tasks.run_statistics(series, series.series_refs(),
                                   correlations=False,
                                   group_by_labels=False)
    row = next(r for r in res.table if r.get("mean") is not None)
    assert row["n"] == 40 and row["mean"] == 20.5
    assert row["median"] == 20.5 and row["min"] == 1.0 and row["max"] == 40.0
    assert row["std"] in (round(statistics.stdev(range(1, 41)), 4),
                          round(statistics.pstdev(range(1, 41)), 4))
    assert row["n_null_values"] == 0 and row["n_duplicates"] == 0
    assert row["interval_seconds"] == 60.0
    assert row["trend_direction"] == "rising"
    assert row["acf"][0] == pytest.approx(row["acf_lag1"])
    assert {p["kind"] for p in res.points} == {"distribution"}


def test_statistics_correlate_and_test_groups_but_disclaim_causation(sim):
    refs = [ref_of(sim, "R1", m) for m in ("if.rx_bps", LABEL_METRIC,
                                          "ping.loss_pct")]
    res = lab_tasks.run_statistics(sim, refs)
    assert res.status == "ok"
    assert res.metrics["correlations_computed"] is True
    assert res.metrics["no_causal_claim"] is True
    assert res.metrics["series_described"] == 3
    assert res.metrics["group_tests"] >= 1
    causes = [w for w in res.warnings if "causation" in w]
    assert causes and "establishing a cause needs an intervention" in \
        causes[0]
    assert any("does not prove that one metric leads or causes" in w
               for w in res.warnings)
    described = [r for r in res.table if r.get("acf") is not None]
    pairs = [r for r in res.table if "pearson_r" in r]
    assert described and pairs
    assert res.metrics["series_described"] == len(described)
    for pair in pairs:
        assert -1.0 <= pair["pearson_r"] <= 1.0
        assert pair["n_aligned"] > 0 and pair["method"]
        # a lagged peak is reported as association strength, never as a cause
        assert "causal" not in str(pair["xcorr_peak"]).lower()
    groups = [r for r in res.table if r.get("test")]
    assert groups and "Mann-Whitney" in groups[0]["test"]


def test_statistics_on_an_unlabelled_series_skips_the_group_test():
    plain = build([float(i % 6) for i in range(40)], metric="rtt", unit="ms")
    res = lab_tasks.run_statistics(plain, plain.series_refs())
    assert res.metrics["group_tests"] == 0
    assert res.metrics["no_causal_claim"] is True


def test_tasks_refuse_rather_than_invent_on_an_empty_selection(sim):
    for call in (lambda: lab_tasks.run_anomaly_comparison(sim, []),
                 lambda: lab_tasks.run_forecast_comparison(sim, []),
                 lambda: lab_tasks.run_statistics(sim, [])):
        res = call()
        assert res.status == "insufficient_data"
        assert res.metrics["reason"] == "insufficient_data"
        assert res.message.startswith("no series were selected")
        # the dataset-wide gate is not quoted as if it were the problem
        assert "meet the" not in res.message
        assert res.table == [] and res.points == []
