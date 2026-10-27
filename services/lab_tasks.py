"""The four AI Lab experiment tasks over one validated dataset (Phase 5).

Every task takes a :class:`analytics.lab_dataset.LabDataset` plus parameters and
returns a :class:`TaskResult`: metrics, a comparison table, bounded chart-ready
points, artifacts (confusion matrices, bands, curves), the *effective* feature
list and the warnings/notes that qualify the numbers.

Reuse-first: nothing here re-implements a detector, a forecast model, a metric
formula or a quality gate. The tasks orchestrate

* ``analytics.anomaly_detection.detect_frame`` (six detectors) and
  ``analytics.ml_anomaly.detect_multivariate_isolation_forest``;
* ``analytics.forecasting.FORECAST_MODELS`` + ``MODEL_KWARGS`` routing,
  ``apply_holdout_bands`` and ``services.forecast_service.build_ensemble``;
* ``analytics.evaluation`` (chronological split, MAE/RMSE/R2/MAPE/sMAPE/MASE,
  skill-vs-naive, ``score_detection``) and ``analytics.data_quality`` gates;
* ``analytics.eda`` (summary/histogram/correlation) plus ``scipy`` tests;
* ``analytics.lab_classification`` (label-gated supervised classification).

Refusal is a result, never a gap: a task without enough data, or without
labels when labels are required, returns ``status`` plus the concrete reason
and the precondition that would satisfy it. No metric is invented to fill a
cell, and a score computed on synthetic ground truth is always tagged as such.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pandas as pd

from analytics import (anomaly_detection, data_quality, eda, evaluation,
                       forecasting, lab_classification, lab_dataset,
                       ml_anomaly, time_series)
from config import get_settings

#: Chart payloads are capped: a run must stay exportable and renderable.
#: When a series is longer, it is down-sampled for the PLOT only - metrics are
#: always computed on every point, and the note says so.
MAX_CHART_POINTS = 1200

#: Threshold sweep factors used for the supervised detector comparison curve.
#: PR-AUC here is a step-wise average precision over these detector settings,
#: not a continuous-score PR-AUC; the method string is carried with the value.
SWEEP_FACTORS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0)

#: Which knob of ``detect_frame`` each detector's sensitivity is expressed by.
SWEEP_PARAM = {
    "rolling_mad": "mad_threshold",
    "rolling_zscore": "zscore_threshold",
    "ewma": "ewma_threshold",
    "cusum": "cusum_limit",
    "seasonal_residual": "seasonal_threshold",
}

#: Detectors ranked as the reference baseline for comparison wording.
STATISTICAL_BASELINE = "rolling_mad"

#: Half-width of the lag search used for the cross-correlation peak.
MAX_XCORR_LAG = 12

#: Detectors whose cost grows with every judged point (a model refit per
#: sample). In the lab they are evaluated on a bounded, disclosed tail of the
#: series rather than on all of it.
PER_POINT_DETECTORS = frozenset({"isolation_forest"})

#: Seed used by the multivariate joint detector (fixed => reproducible runs).
MULTIVARIATE_SEED = 42

CAUSATION_NOTE = (
    "Correlation, lagged correlation and group differences describe "
    "association only. Nothing here establishes causation: establishing a "
    "cause needs an intervention (a controlled change in the lab) or an "
    "explicit causal model, and this application deliberately performs "
    "neither from an analytics screen.")


def _refusal_status(reason: str) -> str:
    """Map a refusal reason code onto the status the UI has to show.

    One table for every task, so "no usable labels" and "not enough rows" stay
    distinguishable wherever the refusal comes from (a gate, the label pipeline
    or the classifier itself) instead of depending on which code path ran.
    """
    if reason in ("insufficient_data", "insufficient_features",
                  "insufficient_train_rows", "class_too_thin", "row_mismatch",
                  "single_class"):
        return "insufficient_data"
    if reason in ("no_labels", "partial_labels"):
        return "no_labels"
    return "refused"


@dataclasses.dataclass
class TaskResult:
    """Uniform result envelope for all four tasks."""

    task: str
    status: str                     # ok | refused | insufficient_data | error
    message: str
    metrics: dict = dataclasses.field(default_factory=dict)
    table: list[dict] = dataclasses.field(default_factory=list)
    points: list[dict] = dataclasses.field(default_factory=list)
    artifacts: dict = dataclasses.field(default_factory=dict)
    warnings: list[str] = dataclasses.field(default_factory=list)
    config: dict = dataclasses.field(default_factory=dict)
    effective_features: list[str] = dataclasses.field(default_factory=list)

    @classmethod
    def refused(cls, task: str, reason: str, detail: str = "", *,
                needed: dict | None = None, config: dict | None = None
                ) -> "TaskResult":
        return cls(task=task, status=_refusal_status(reason),
                   message=detail or f"{reason}: nothing was computed.",
                   metrics={"reason": reason},
                   warnings=([f"Precondition: {k} = {v}"
                              for k, v in (needed or {}).items()]),
                   config=dict(config or {}))

    def as_dict(self, *, include_points: bool = True) -> dict:
        payload = {
            "task": self.task, "status": self.status, "message": self.message,
            "metrics": self.metrics, "table": self.table,
            "artifacts": self.artifacts, "warnings": self.warnings,
            "config": self.config,
            "effective_features": self.effective_features,
        }
        if include_points:
            payload["points"] = self.points
        return payload


def _downsample(values: list, limit: int = MAX_CHART_POINTS
                ) -> tuple[list, bool]:
    n = len(values)
    if n <= limit:
        return values, False
    step = math.ceil(n / limit)
    return values[::step], True


def _label_note(dataset: lab_dataset.LabDataset) -> str:
    if dataset.label_kind == "none":
        return "no ground-truth labels: unsupervised evidence only"
    if dataset.source_type == lab_dataset.SOURCE_SIMULATED:
        return ("supervised scores use SYNTHETIC ground truth from the "
                "simulator (injected events); they are not real-network accuracy")
    return (f"supervised scores use {dataset.label_kind} labels supplied by "
            f"the {dataset.source_type} source; their correctness is asserted "
            "by the source, not verified here")


# ==========================================================================
# Task A - anomaly detection comparison
# ==========================================================================
def run_anomaly_comparison(
    dataset: lab_dataset.LabDataset,
    refs: list[lab_dataset.SeriesRef],
    *,
    detectors: tuple[str, ...] | None = None,
    include_multivariate: bool = False,
    multivariate_metrics: tuple[str, ...] | None = None,
    window: int | None = None,
    mad_threshold: float | None = None,
    zscore_threshold: float | None = None,
    ewma_threshold: float | None = None,
    cusum_drift: float | None = None,
    cusum_limit: float | None = None,
    seasonal_threshold: float | None = None,
    sweep_curve: bool = True,
) -> TaskResult:
    """Compare detectors on the selected series, supervised when labels exist."""
    settings = get_settings()
    task = lab_dataset.TASK_ANOMALY
    ok, why = lab_dataset.task_gate(dataset, task)
    if not ok or not refs:
        # an empty selection is not a data-quality failure, and quoting the
        # dataset-wide gate ("10/10 series meet ...") would say the opposite
        return TaskResult.refused(
            task, "insufficient_data",
            why if refs else "no series were selected, so nothing was computed; "
            "pick at least one series from the inventory.",
            needed={"min_points_detection": settings.min_points_detection})
    chosen = tuple(detectors or anomaly_detection.DEFAULT_DETECTORS)
    for name in chosen:
        if name not in anomaly_detection.DETECTORS:
            return TaskResult.refused(
                task, "unknown_detector",
                f"detector {name!r} is not in {tuple(anomaly_detection.DETECTORS)}")
    params = {
        "window": window or settings.detector_window,
        "mad_threshold": (settings.mad_threshold if mad_threshold is None
                          else mad_threshold),
        "zscore_threshold": (settings.zscore_threshold if zscore_threshold
                             is None else zscore_threshold),
        "ewma_threshold": (settings.ewma_threshold if ewma_threshold is None
                           else ewma_threshold),
        "cusum_drift": (settings.cusum_drift_k if cusum_drift is None
                        else cusum_drift),
        "cusum_limit": (settings.cusum_threshold_h if cusum_limit is None
                        else cusum_limit),
        "seasonal_threshold": (settings.seasonal_threshold
                               if seasonal_threshold is None
                               else seasonal_threshold),
    }

    rows: list[dict] = []
    points: list[dict] = []
    notes: list[str] = [_label_note(dataset)]
    totals: dict[str, dict] = {}
    labelled_series_count = 0

    for ref in refs:
        series_ok, series_why = lab_dataset.task_gate(dataset, task, ref=ref)
        if not series_ok:
            rows.append({"series": str(ref), "detector": "(all)",
                         "status": "insufficient_data", "reason": series_why})
            continue
        frame = dataset.series_frame(ref)
        interval = time_series.median_interval_seconds(frame)
        truth = lab_dataset.binary_truth(dataset, ref)
        if truth is not None:
            labelled_series_count += 1
        flagged_by_detector: dict[str, set] = {}

        for name in chosen:
            eval_frame, eval_truth, slice_note = _evaluation_slice(
                dataset, ref, frame, truth, name)
            flagged, meta = _detect_one(
                dataset, ref, eval_frame, name, params, interval, eval_truth,
                sweep=sweep_curve and eval_truth is not None)
            if slice_note:
                meta["evaluation_slice"] = slice_note
                notes.append(f"{ref} · {name}: {slice_note}")
            flagged_by_detector[name] = flagged
            rows.append({"series": str(ref), "detector": name, **meta})
            bucket = totals.setdefault(name, {
                "tp": 0, "fp": 0, "fn": 0, "normal": 0, "flagged": 0})
            bucket["flagged"] += int(meta.get("n_flagged", 0))
            for key in ("tp", "fp", "fn"):
                if isinstance(meta.get(key), int):
                    bucket[key] += meta[key]
            if meta.get("normal_total"):
                bucket["normal"] += int(meta["normal_total"])

        if len(flagged_by_detector) > 1:
            rows.append({"series": str(ref), "detector": "(agreement)",
                         "status": "diagnostic",
                         "pairwise_jaccard": _pairwise_jaccard(
                             flagged_by_detector),
                         "explanation": (
                             "How much the detectors agree on this series. "
                             "Agreement is evidence of robustness, not of "
                             "correctness - without labels no detector can be "
                             "called accurate.")})

        values = dataset.series_values(ref)
        shown, downsampled = _downsample(
            [{"t": ts.isoformat(), "v": float(v)}
             for ts, v in zip(values.index, values.to_numpy())])
        points.append({"kind": "series", "series": str(ref),
                       "points": shown,
                       "note": ("chart down-sampled for display; metrics use "
                                f"all {len(values)} points"
                                if downsampled else "all points plotted")})
        for ts in sorted((truth or {}).get("anomaly", ())):
            points.append({"kind": "truth", "series": str(ref),
                           "t": pd.Timestamp(ts).isoformat()})
        for name, flagged in flagged_by_detector.items():
            points.append({"kind": "flags", "series": str(ref),
                           "detector": name,
                           "points": [pd.Timestamp(ts).isoformat()
                                      for ts in sorted(flagged)]})

    mv_rows, mv_points, mv_notes = _multivariate_pass(
        dataset, refs, chosen, include_multivariate, multivariate_metrics,
        params)
    rows.extend(mv_rows)
    points.extend(mv_points)
    notes.extend(mv_notes)

    per_point = [n for n in chosen if n in PER_POINT_DETECTORS]
    if per_point:
        cap = get_settings().ai_lab_ml_max_points
        fits = len(refs) * len(per_point) * cap
        notes.append(
            f"cost note: {len(per_point)} per-point detector(s) over {len(refs)} "
            f"series means up to {fits} model fits at the {cap}-sample "
            "evaluation cap (NETPILOT_AI_LAB_ML_MAX_POINTS). Fewer series or a "
            "lower cap makes a run faster; the cap never raises a score.")

    labelled = [r for r in rows if r.get("precision") is not None]
    metrics = {
        "series_evaluated": len({r["series"] for r in rows
                                 if r.get("status") == "ok"
                                 and r["series"] in {str(x) for x in refs}}),
        "detectors_compared": len(chosen) + (1 if include_multivariate else 0),
        "labels_available": labelled_series_count > 0,
        "supervised": bool(labelled),
        "best_by_f1": _best_row(labelled, "f1"),
        "best_by_recall_at_precision": _best_recall_at_precision(labelled),
        "total_flags": sum(int(r.get("n_flagged", 0)) for r in rows
                           if r.get("status") == "ok"),
    }
    if not labelled:
        metrics["accuracy_claims"] = (
            "none - this source has no usable anomaly/normal ground truth, so "
            "precision/recall/F1/PR-AUC are reported as null instead of "
            "invented")
    return TaskResult(
        task=task, status="ok",
        message=(f"compared {len(chosen)} detector(s) across "
                 f"{len(refs)} series ({'supervised' if labelled else 'unsupervised'} "
                 "evaluation)"),
        metrics=metrics, table=rows, points=points,
        artifacts={"per_detector": totals,
                   "sweep_method": ("step-wise average precision over "
                                    f"{len(SWEEP_FACTORS)} detector threshold "
                                    "settings, not a continuous-score PR-AUC")},
        warnings=notes,
        config={"detectors": list(chosen), "series": [str(r) for r in refs],
                "sweep_curve": sweep_curve,
                "multivariate": include_multivariate,
                "multivariate_seed": (MULTIVARIATE_SEED
                                      if include_multivariate else None),
                "per_point_detector_cap": get_settings().ai_lab_ml_max_points,
                **{k: v for k, v in params.items()}},
        effective_features=sorted({r.metric for r in refs}))


def _evaluation_slice(dataset, ref, frame, truth, name):
    """Bound a per-point detector to the most recent N samples, honestly.

    Returns the frame to evaluate, the ground truth restricted to exactly that
    window, and the sentence that says so. Scoring a tail against whole-series
    truth would count anomalies the model never saw as false negatives, so both
    sides are narrowed together and the basis string carries the window.
    """
    cap = get_settings().ai_lab_ml_max_points
    if name not in PER_POINT_DETECTORS or len(frame) <= cap:
        return frame, truth, None
    subset = frame.iloc[-cap:].reset_index(drop=True)
    start = pd.Timestamp(subset["collected_at"].iloc[0])
    window_truth = (lab_dataset.binary_truth(dataset, ref, from_ts=start)
                    if truth is not None else None)
    note = (f"this detector refits a model for every judged point, so cost "
            f"scales with series length: only the most recent {cap} of "
            f"{len(frame)} samples were evaluated "
            "(NETPILOT_AI_LAB_ML_MAX_POINTS). Its counts and any supervised "
            f"score describe {start.isoformat()} onward, not the whole series.")
    return subset, window_truth, note


def _detect_one(dataset, ref, frame, name, params, interval, truth, *,
                sweep: bool) -> tuple[set, dict]:
    settings = get_settings()
    base_kwargs = {
        "device_name": ref.device, "metric": ref.metric,
        "interface_name": ref.interface, "window": params["window"],
        "interval_seconds": interval, "season_hours": settings.season_hours,
    }
    if name in SWEEP_PARAM:
        base_kwargs[SWEEP_PARAM[name]] = params[SWEEP_PARAM[name]]
    points = anomaly_detection.detect_frame(
        frame, detectors=(name,), **_kwargs_for(name, params, base_kwargs))
    flagged = {pd.Timestamp(p.timestamp) for p in points}
    deviations = [p.deviation for p in points if p.deviation is not None]
    meta: dict = {
        "status": "ok", "n_points": int(len(frame)),
        "n_flagged": len(flagged),
        "flag_rate": round(len(flagged) / max(1, len(frame)), 4),
        "median_deviation": (round(float(pd.Series(deviations).median()), 4)
                             if deviations else None),
        "interval_seconds": interval,
    }
    if name == "seasonal_residual" and interval is None:
        meta["abstained"] = ("no sampling interval, so the seasonal model "
                             "refused to judge (an abstention, not a clean "
                             "bill of health)")
    if truth is not None:
        score = evaluation.score_detection(flagged, truth["anomaly"],
                                           total_normal=truth["normal"])
        meta.update({
            "tp": score.true_positives, "fp": score.false_positives,
            "fn": score.false_negatives, "precision": _round(score.precision),
            "recall": _round(score.recall), "f1": _round(score.f1),
            "false_alarm_rate": _round(score.false_alarm_rate),
            "normal_total": truth["normal"],
            "label_basis": truth["basis"],
        })
        if sweep and SWEEP_PARAM.get(name):
            curve = _sweep_curve(dataset, ref, frame, name, params, interval,
                                 truth)
            meta["pr_auc"] = curve["average_precision"]
            meta["pr_auc_method"] = curve["method"]
            meta["curve"] = curve["curve"]
    else:
        meta.update({"tp": None, "fp": None, "fn": None, "precision": None,
                     "recall": None, "f1": None, "pr_auc": None,
                     "unsupervised_only": (
                         "no ground truth for this series: only flag counts and "
                         "agreement can be reported")})
    return flagged, meta


def _kwargs_for(name: str, params: dict, base: dict) -> dict:
    """Pass each detector exactly the knob it declares (mirrors detect_frame)."""
    kwargs = dict(base)
    if name == "rolling_mad":
        kwargs["mad_threshold"] = params["mad_threshold"]
    elif name == "rolling_zscore":
        kwargs["zscore_threshold"] = params["zscore_threshold"]
    elif name == "ewma":
        kwargs["ewma_threshold"] = params["ewma_threshold"]
    elif name == "cusum":
        kwargs["cusum_drift"] = params["cusum_drift"]
        kwargs["cusum_limit"] = params["cusum_limit"]
    elif name == "seasonal_residual":
        kwargs["seasonal_threshold"] = params["seasonal_threshold"]
    return kwargs


def _sweep_curve(dataset, ref, frame, name, params, interval, truth) -> dict:
    base = dict(params)
    curve: list[dict] = []
    for factor in SWEEP_FACTORS:
        knob = SWEEP_PARAM[name]
        tuned = dict(base)
        tuned[knob] = max(1e-6, float(base[knob]) * factor)
        points = anomaly_detection.detect_frame(
            frame, detectors=(name,),
            **_kwargs_for(name, tuned, {
                "device_name": ref.device, "metric": ref.metric,
                "interface_name": ref.interface, "window": base["window"],
                "interval_seconds": interval,
                "season_hours": get_settings().season_hours}))
        flagged = {pd.Timestamp(p.timestamp) for p in points}
        score = evaluation.score_detection(flagged, truth["anomaly"],
                                           total_normal=truth["normal"])
        curve.append({"setting": knob, "factor": factor,
                      "threshold": round(float(tuned[knob]), 6),
                      "n_flagged": len(flagged),
                      "precision": _round(score.precision, 6),
                      "recall": _round(score.recall, 6),
                      "f1": _round(score.f1, 6)})
    return {"average_precision": _average_precision(curve),
            "method": (f"step-wise average precision over a "
                       f"{len(SWEEP_FACTORS)}-point {SWEEP_PARAM[name]} sweep; "
                       "not a continuous-score PR-AUC"),
            "curve": curve}


def _average_precision(curve: list[dict]) -> float | None:
    pts = [(c["recall"], c["precision"]) for c in curve
           if c["recall"] is not None and c["precision"] is not None]
    if len(pts) < 2:
        return None
    pts.sort(key=lambda p: p[0])
    # Precision envelope: the highest precision achievable at >= this recall.
    envelope = [0.0] * len(pts)
    running = 0.0
    for i in range(len(pts) - 1, -1, -1):
        running = max(running, pts[i][1])
        envelope[i] = running
    total, prev_recall = 0.0, 0.0
    for (recall, _precision), precision in zip(pts, envelope):
        total += (recall - prev_recall) * precision
        prev_recall = recall
    return round(total, 4)


def _pairwise_jaccard(flags: dict[str, set]) -> dict[str, float]:
    names = sorted(flags)
    out: dict[str, float] = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            union = flags[a] | flags[b]
            out[f"{a}~{b}"] = (round(len(flags[a] & flags[b]) / len(union), 4)
                               if union else None)
    return out


def _multivariate_pass(dataset, refs, chosen, include, metrics, params):
    if not include:
        return [], [], []
    rows: list[dict] = []
    points: list[dict] = []
    notes: list[str] = []
    settings = get_settings()
    for device in sorted({r.device for r in refs}):
        interface = next((r.interface for r in refs if r.device == device), None)
        wanted = tuple(metrics) if metrics else tuple(
            sorted({r.metric for r in refs if r.device == device}))
        features, timestamps, used = dataset.feature_frame(
            device, wanted, interface=interface)
        if features.empty or len(used) < 2:
            rows.append({"series": f"{device} (multivariate)",
                         "detector": "mv_isolation_forest",
                         "status": "insufficient_features",
                         "reason": (f"only {len(used)} metric(s) could be "
                                    "time-aligned; the joint detector needs "
                                    "at least two and abstains otherwise")})
            continue
        # Joint truth = union of the labelled anomaly windows across this
        # device's selected series, with the normal count pooled the same way.
        truth_ts: set = set()
        normal_total = 0
        labelled_series: list[str] = []
        for ref in refs:
            if ref.device != device:
                continue
            truth = lab_dataset.binary_truth(dataset, ref)
            if truth:
                truth_ts |= truth["anomaly"]
                normal_total += int(truth["normal"])
                labelled_series.append(ref.metric)
        found = ml_anomaly.detect_multivariate_isolation_forest(
            features, timestamps, device_name=device,
            data_source=dataset.source_type, interface_name=interface,
            contamination=0.05, random_seed=MULTIVARIATE_SEED,
            min_samples=settings.min_points_detection,
            metric_label="multivariate:" + "|".join(used))
        flagged = {pd.Timestamp(p.timestamp) for p in found}
        row = {"series": f"{device} (multivariate)",
               "detector": "mv_isolation_forest", "status": "ok",
               "n_points": int(len(features)), "n_flagged": len(flagged),
               "flag_rate": round(len(flagged) / max(1, len(features)), 4),
               "aligned_metrics": used}
        if truth_ts:
            score = evaluation.score_detection(flagged, truth_ts,
                                               total_normal=normal_total)
            row.update({"tp": score.true_positives,
                        "fp": score.false_positives,
                        "fn": score.false_negatives,
                        "precision": _round(score.precision),
                        "recall": _round(score.recall),
                        "f1": _round(score.f1), "pr_auc": None,
                        "label_basis": (f"union over {labelled_series}"),
                        "scored_over_rows": int(len(features))})
            notes.append(
                f"{device}: supervised scores pool the labelled anomaly windows "
                f"of {labelled_series}; a point normal for one metric but "
                "flagged as anomalous by another is counted as an anomaly for "
                "the joint view, so this is a deliberately conservative basis.")
        else:
            row.update({"tp": None, "fp": None, "fn": None, "precision": None,
                        "recall": None, "f1": None, "pr_auc": None,
                        "unsupervised_only": (
                            "no joint anomaly/normal truth for this device")})
        rows.append(row)
        points.append({"kind": "flags", "series": f"{device} (multivariate)",
                       "detector": "mv_isolation_forest",
                       "points": [ts.isoformat() for ts in sorted(flagged)]})
        notes.append(
            f"{device}: multivariate Isolation Forest is retrospective over a "
            f"{len(features)}-row complete-case window over {used} - it uses "
            "the whole supplied window for the fit, so treat it as post-hoc "
            "evidence, not an online detector.")
    return rows, points, notes


# ==========================================================================
# Task B - forecasting comparison on one identical holdout
# ==========================================================================
def run_forecast_comparison(
    dataset: lab_dataset.LabDataset,
    refs: list[lab_dataset.SeriesRef],
    *,
    horizon: int | None = None,
    models: tuple[str, ...] | None = None,
    test_frac: float = 0.2,
    band_coverage: float | None = None,
    ensemble: bool = True,
) -> TaskResult:
    """Backtest several forecast models per series on the same holdout.

    Each model is fitted on train only and evaluated on identical test
    timestamps, so the table compares like with like. The future path is a
    separate refit on all in-time data and is kept clearly apart from the
    holdout in ``points`` (``kind`` = ``holdout`` vs ``future``).
    """
    from services import forecast_service

    settings = get_settings()
    task = lab_dataset.TASK_FORECAST
    ok, why = lab_dataset.task_gate(dataset, task)
    if not ok or not refs:
        return TaskResult.refused(
            task, "insufficient_data",
            why if refs else "no series were selected, so nothing was computed; "
            "pick at least one series from the inventory.",
            needed={"min_points_forecast": settings.min_points_forecast})
    chosen = tuple(models or forecast_service.DEFAULT_MODELS)
    for name in chosen:
        if name not in forecasting.FORECAST_MODELS:
            return TaskResult.refused(
                task, "unknown_model",
                f"model {name!r} is not in {tuple(forecasting.FORECAST_MODELS)}")
    horizon = max(1, horizon or settings.forecast_horizon_default)
    coverage = (settings.band_nominal_coverage if band_coverage is None
                else min(0.999, max(0.5, float(band_coverage))))

    rows: list[dict] = []
    points: list[dict] = []
    notes: list[str] = []
    per_series: dict[str, dict] = {}
    # holdout stamps actually scored per series, so "identical_holdout" below is
    # a verified statement about the runs, not a claim printed by the code
    holdout_stamps: dict[str, set[tuple[str, ...]]] = {}

    for ref in refs:
        series_ok, series_why = lab_dataset.task_gate(dataset, task, ref=ref)
        if not series_ok:
            rows.append({"series": str(ref), "model": "(all)",
                         "status": "insufficient_data",
                         "reason": series_why})
            continue
        values = dataset.series_values(ref)
        interval = time_series.median_interval_seconds(
            dataset.series_frame(ref))
        split = evaluation.chronological_split(
            values, test_frac=test_frac, val_frac=max(0.0, test_frac / 2),
            min_train=max(12, settings.min_points_forecast // 2), min_test=5)
        if not split.has_test:
            rows.append({"series": str(ref), "model": "(all)",
                         "status": "insufficient_data",
                         "reason": (f"holdout needs >= 5 points after a train "
                                    f"block of >= {len(split.train)}; series "
                                    f"has {len(values)} usable points")})
            continue
        ok_runs: list[dict] = []
        test_actuals = [float(a) for a in split.test.to_numpy(dtype=float)]
        origin = values.index.max()
        for name in chosen:
            fn = forecasting.FORECAST_MODELS[name]
            supplied = {"interval_seconds": interval,
                        "season_hours": settings.season_hours,
                        "val": split.val}
            kwargs = {k: supplied[k] for k in forecasting.MODEL_KWARGS.get(
                name, ()) if supplied.get(k) is not None}
            try:
                eval_res = fn(split.train, horizon, future_timestamps=None,
                              test=split.test, **kwargs)
            except Exception as exc:
                rows.append({"series": str(ref), "model": name,
                             "status": "error",
                             "reason": f"{type(exc).__name__}: {str(exc)[:160]}"})
                continue
            if not eval_res.ok:
                rows.append({"series": str(ref), "model": name,
                             "status": "skipped", "reason": eval_res.reason,
                             "model_version": eval_res.model_version,
                             "config": eval_res.config})
                continue
            pairs = [(a, p) for a, p in zip(test_actuals, eval_res.test_pred)
                     if not _isnan(p)]
            act = [a for a, _ in pairs]
            prd = [p for _, p in pairs]
            abs_errors = [abs(a - p) for a, p in pairs]
            fut_kwargs = dict(kwargs)
            fut_kwargs.pop("val", None)
            steps = ([origin + pd.Timedelta(seconds=interval * (h + 1))
                      for h in range(horizon)] if interval else None)
            try:
                future_res = fn(values, horizon, future_timestamps=steps,
                                **fut_kwargs)
            except Exception as exc:
                future_res = None
                notes.append(f"{ref} · {name}: future refit failed "
                             f"({type(exc).__name__}); holdout scores are "
                             "unaffected and still reported")
            raw_future = list(future_res.future) if future_res else []
            future, band_meta = forecasting.apply_holdout_bands(
                raw_future, abs_errors, coverage=coverage,
                mode=forecast_service.BAND_MODE)
            row = {
                "series": str(ref), "model": name, "status": "ok",
                "model_version": eval_res.model_version,
                "config": eval_res.config,
                "n_train": len(split.train), "n_val": len(split.val),
                "n_test": len(split.test), "n_eval": len(prd),
                "mae": _round(evaluation.mae(act, prd)),
                "rmse": _round(evaluation.rmse(act, prd)),
                "r2": _round(evaluation.r2(act, prd)),
                "mape": _round(evaluation.mape(act, prd)),
                "smape": _round(evaluation.smape(act, prd)),
                "mase": _round(evaluation.mase(act, prd, split.train)),
                "skill": None,
                "test_pred": eval_res.test_pred,
                "test_timestamps": list(split.test.index),
                "future": future, "band": band_meta,
            }
            rows.append({k: v for k, v in row.items()
                         if k not in ("test_pred", "test_timestamps",
                                      "future")})
            ok_runs.append(row)

        naive = next((r for r in ok_runs if r["model"] == "naive"), None)
        holdout_stamps[str(ref)] = {
            tuple(ts.isoformat() for ts in r["test_timestamps"])
            for r in ok_runs}
        if len(holdout_stamps[str(ref)]) > 1:
            notes.append(f"{ref}: the compared models were scored on different "
                         "holdout timestamps, so the table rows are not directly "
                         "comparable")
        for r in ok_runs:
            if r is naive:
                r["skill"] = 0.0
            elif naive is None:
                r["skill"] = None
            else:
                aligned = [(a, p, n) for a, p, n in zip(
                    test_actuals, r["test_pred"], naive["test_pred"])
                    if not _isnan(p) and not _isnan(n)]
                r["skill"] = _round(evaluation.skill_vs_naive(
                    [x[0] for x in aligned], [x[1] for x in aligned],
                    [x[2] for x in aligned]))
            rows[[i for i, x in enumerate(rows)
                  if x.get("series") == str(ref)
                  and x.get("model") == r["model"]][0]]["skill"] = r["skill"]
        if naive is None:
            notes.append(f"{ref}: naive baseline was not in the model list, so "
                         "skill-vs-naive cannot be computed and is reported as "
                         "null")

        scored = [r for r in ok_runs if r.get("mae") is not None]
        best = (min(scored, key=lambda r: (r["mae"], r["model"]))
                if scored else None)
        ens = forecast_service.build_ensemble(
            ok_runs, test_actuals, train=split.train, coverage=coverage,
            band_mode=forecast_service.BAND_MODE) if ensemble else {
                "model": forecast_service.ENSEMBLE_NAME, "status": "disabled",
                "reason": "ensemble blending not requested"}
        if ens.get("mae") is not None:
            rows.append({"series": str(ref), "model": ens["model"],
                         "status": "ok", "mae": _round(ens["mae"]),
                         "n_eval": ens.get("n_eval"),
                         "blend_note": ens.get("status", "computed"),
                         "not_persisted": (
                             "the blend is a comparison result over the other "
                             "runs, not an independently fitted model")})
        per_series[str(ref)] = {"best_model": (best or {}).get("model"),
                                "best_mae": (best or {}).get("mae"),
                                "ensemble": ens,
                                "split": {"train": len(split.train),
                                          "val": len(split.val),
                                          "test": len(split.test),
                                          "train_end": _iso(split.train_end),
                                          "test_end": _iso(split.test_end)}}

        actual = [(ts.isoformat(), float(v)) for ts, v in
                  zip(values.index, values.to_numpy())]
        shown, downsampled = _downsample([{"t": t, "v": v} for t, v in actual])
        points.append({"kind": "history", "series": str(ref),
                       "points": shown,
                       "note": ("chart down-sampled; metrics use every point"
                                if downsampled else "all points plotted")})
        points.append({"kind": "split_boundary", "series": str(ref),
                       "train_end": _iso(split.train_end),
                       "test_start": _iso(split.test.index.min())})
        for r in ok_runs:
            points.append({"kind": "holdout", "series": str(r["series"]),
                           "model": r["model"],
                           "points": [{"t": ts.isoformat(), "v": _round(p, 6)}
                                      for ts, p in zip(r["test_timestamps"],
                                                       r["test_pred"])]})
            points.append({"kind": "future", "series": str(r["series"]),
                           "model": r["model"],
                           "points": [{"t": _iso(f["target_timestamp"]),
                                       "v": _round(f["predicted_value"], 6),
                                       "lower": _round(f.get("lower_bound"), 6),
                                       "upper": _round(f.get("upper_bound"), 6)}
                                      for f in r["future"]]})
        notes.append(
            f"{ref}: holdout scores are backtests on data that already "
            "exists; the future path is an extrapolation refit on all in-time "
            "data and is not a scoreable prediction yet. They are plotted "
            "separately and must not be read as one line.")

    ok_rows = [r for r in rows if r.get("status") == "ok" and r.get("mae")
               is not None]
    return TaskResult(
        task=task, status="ok",
        message=(f"forecast comparison over {len(refs)} series with "
                 f"{len(chosen)} model(s), horizon {horizon}"),
        metrics={
            "models": list(chosen), "horizon": horizon,
            "band_nominal_coverage": coverage,
            "band_mode": forecast_service.BAND_MODE,
            "best_by_series": {k: v["best_model"] for k, v in
                               per_series.items()},
            "identical_holdout": bool(holdout_stamps) and all(
                len(stamps) <= 1 for stamps in holdout_stamps.values()),
            "scored_models": len(ok_rows),
        },
        table=rows, points=points, artifacts={"per_series": per_series},
        warnings=notes + [
            "MASE/sMAPE are reported whenever they are computable; MAPE "
            "skips every point whose actual value is (near) zero, because a "
            "percentage against zero is not a number. It is null only when no "
            "point survives that guard, so read it next to n_eval rather than "
            "assuming it covers the whole holdout."],
        config={"models": list(chosen), "horizon": horizon,
                "test_frac": test_frac, "band_coverage": coverage,
                "ensemble": ensemble, "series": [str(r) for r in refs]},
        effective_features=sorted({r.metric for r in refs}))


# ==========================================================================
# Task C - fault classification (label-gated)
# ==========================================================================
def run_fault_classification(
    dataset: lab_dataset.LabDataset,
    *,
    device: str,
    interface: str | None,
    feature_metrics: tuple[str, ...],
    label_metric: str,
    models: tuple[str, ...] = ("logistic_regression", "random_forest"),
    test_frac: float = 0.25,
    split_mode: str = lab_classification.SPLIT_CHRONOLOGICAL,
    add_lags: int = 0,
    random_seed: int = 42,
) -> TaskResult:
    task = lab_dataset.TASK_CLASSIFICATION
    if dataset.label_kind == "none":
        return TaskResult.refused(
            task, "no_labels",
            "this source carries no fault labels, so classification is not "
            "run and no accuracy is reported. Choose the simulated source (its "
            "injected events provide ground truth) or a file with a label "
            "column.",
            needed={"label column": "a class per observation"})
    label_ref = lab_dataset.SeriesRef(device, label_metric, interface)
    if not any(item.ref.key == label_ref.key for item in dataset.inventory):
        return TaskResult.refused(
            task, "unknown_series",
            f"{label_ref} is not part of this dataset")
    if label_metric in set(feature_metrics):
        # The label would be handed to the model as an input column.
        return TaskResult.refused(
            task, "label_in_features",
            f"{label_metric} is both the label and a feature: the model would "
            "be shown the answer. Remove it from the feature list.",
            needed={"feature_metrics": "metrics other than the label metric"})
    features, timestamps, used = dataset.feature_frame(
        device, feature_metrics, interface=interface)
    if features.empty:
        return TaskResult.refused(
            task, "insufficient_features",
            f"no time-aligned matrix for {', '.join(feature_metrics)} on "
            f"{device} ({interface or 'device-level'}); the metrics do not "
            "share enough timestamps",
            needed={"metrics": list(feature_metrics)})
    labels = dataset.series_labels(label_ref)
    if labels is None:
        return TaskResult.refused(
            task, "no_labels", f"{label_ref} has no label values")
    # Align labels to feature timestamps; unmatched feature rows are dropped
    # (counted) rather than given an invented class.
    index = pd.DatetimeIndex(timestamps.to_numpy())
    aligned = labels.reindex(index)
    keep = aligned.notna()
    dropped = int((~keep).sum())
    features = features[keep.to_numpy()].reset_index(drop=True)
    aligned = aligned[keep].reset_index(drop=True)
    stamps = pd.Series(index[keep.to_numpy()])
    lag_note = None
    if add_lags:
        # Lags remove leading rows from the matrix, so the matching labels and
        # timestamps are dropped by the same mask - never re-paired by position.
        features, lag_mask, lag_note = _add_lag_features(
            features, int(add_lags))
        aligned = aligned[lag_mask.to_numpy()].reset_index(drop=True)
        stamps = stamps[lag_mask.to_numpy()].reset_index(drop=True)
    result = lab_classification.evaluate_classification(
        features, stamps, aligned, models=models, test_frac=test_frac,
        split_mode=split_mode, random_seed=random_seed,
        label_provenance=_label_note(dataset))
    notes = list(result.get("warnings") or [])
    if dropped:
        notes.append(f"{dropped} feature row(s) had no label at that "
                     "timestamp and were excluded from the fit")
    if lag_note:
        notes.append(lag_note)
    status = result.get("status", "refused")
    refused_reason = result.get("reason")
    points = []
    for row in result.get("points", []):
        points.append({"kind": "confusion", "model": row["model"],
                       "true": row["true"], "predicted": row["predicted"],
                       "count": row["count"]})
    return TaskResult(
        task=task,
        # a refusal keeps the classifier's own reason code, mapped through the
        # same table as every other refusal so the UI can show "no usable
        # labels" separately from "not enough rows"
        status=("ok" if status == "ok" else _refusal_status(refused_reason)),
        message=(f"classification over "
                 f"{result.get('metrics', {}).get('n_rows', 0)} labelled rows, "
                 f"{len(result.get('metrics', {}).get('classes', []))} class(es)"
                 if status == "ok" else result.get("detail", "")),
        metrics=result.get("metrics", {"reason": result.get("reason")}),
        table=result.get("table", []), points=points,
        artifacts={k: v for k, v in result.items()
                   if k in ("artifacts", "config")},
        # the classifier says what a refusal would need; carried through as
        # preconditions so a refusal is actionable and not just a dead end
        warnings=notes + ([] if status == "ok" else
                          [f"Precondition: {k} = {v}" for k, v in
                           dict(result.get("needed") or {}).items()]),
        config={**dict(result.get("config", {})),
                # identity of what was classified, so the record is readable
                # without the UI in front of it
                "device": device, "interface": interface,
                "label_metric": label_metric,
                "feature_metrics_requested": list(feature_metrics),
                "add_lags": int(add_lags or 0)},
        effective_features=list(features.columns))


def _add_lag_features(features: pd.DataFrame, lags: int
                      ) -> tuple[pd.DataFrame, pd.Series, str]:
    """Append strictly-past lag columns (lag k = value k rows earlier).

    Also returns the boolean row mask that survived, so the caller can drop the
    same leading rows from the labels and timestamps instead of silently
    re-pairing a shorter matrix with a longer label vector.
    """
    out = features.copy()
    for lag in range(1, max(0, int(lags)) + 1):
        for column in features.columns:
            out[f"{column}__lag{lag}"] = features[column].shift(lag)
    usable = out.notna().all(axis=1)
    dropped = int((~usable).sum())
    out = out[usable].reset_index(drop=True)
    return (out, usable.reset_index(drop=True),
            f"added {max(0, int(lags))} lag(s) per metric; the first "
            f"{dropped} row(s) had no past value and were dropped along with "
            "their labels (lags only ever look backwards)")


# ==========================================================================
# Task D - statistical analysis
# ==========================================================================
def run_statistics(
    dataset: lab_dataset.LabDataset,
    refs: list[lab_dataset.SeriesRef],
    *,
    lag_max: int = 12,
    correlations: bool = True,
    group_by_labels: bool = True,
) -> TaskResult:
    """Descriptive statistics, association and difference tests - no causal claims."""
    settings = get_settings()
    task = lab_dataset.TASK_STATISTICS
    ok, why = lab_dataset.task_gate(dataset, task)
    if not ok or not refs:
        return TaskResult.refused(
            task, "insufficient_data",
            why if refs else "no series were selected, so nothing was computed; "
            "pick at least one series from the inventory.",
            needed={"min_points_analysis": settings.min_points_analysis})
    rows: list[dict] = []
    points: list[dict] = []
    notes: list[str] = [CAUSATION_NOTE]
    per_device: dict[str, dict[str, pd.Series]] = {}

    for ref in refs:
        values = dataset.series_values(ref)
        if len(values) < settings.min_points_analysis:
            rows.append({"series": str(ref), "statistic": "(all)",
                         "status": "insufficient_data",
                         "reason": f"{len(values)} usable values, needs "
                                   f"{settings.min_points_analysis}"})
            continue
        frame = dataset.series_frame(ref)
        stats = eda.summary_stats(frame)
        if not stats.get("available"):
            rows.append({"series": str(ref), "statistic": "(all)",
                         "status": "insufficient_data",
                         "reason": stats.get("reason",
                                             "no non-null values to describe")})
            continue
        quality = dataset.quality(ref)
        trend = _trend(values)
        acf = _autocorrelation(values, min(int(lag_max), max(1, len(values) // 4)))
        rows.append({
            "series": str(ref), "status": "ok",
            "n": int(stats["n"]),
            "mean": _round(stats["mean"]), "std": _round(stats["std"]),
            "min": _round(stats["min"]), "max": _round(stats["max"]),
            "median": _round(stats["median"]), "p95": _round(stats["p95"]),
            "skew": _round(float(values.skew())),
            "kurtosis": _round(float(values.kurtosis())),
            "unit": stats.get("unit") or _unit_of(dataset, ref),
            # honest data picture: the numbers below describe what survived
            # validation, and these fields say how much did not
            "n_rows": int(quality.n_rows),
            "n_null_values": int(quality.n_null_values),
            "n_duplicates": int(quality.n_duplicates),
            "gap_count": int(quality.gap_count),
            "interval_seconds": _round(quality.observed_interval_seconds, 2),
            "trend_slope_per_step": trend["slope"],
            "trend_r2": trend["r2"],
            "trend_direction": trend["direction"],
            "acf_lag1": acf[0] if acf else None,
            "acf": acf,
        })
        for reason in quality.reasons_for(data_quality.ANALYSIS):
            notes.append(f"{ref}: descriptive row is qualified by {reason}")
        points.append({"kind": "distribution", "series": str(ref),
                       "points": [{"v": round(float(v), 6)}
                                  for v in _downsample(
                                      values.to_numpy().tolist())[0]]})
        per_device.setdefault(ref.device, {})[
            f"{ref.metric}{'@' + ref.interface if ref.interface else ''}"] = \
            values

    if correlations:
        for device, series_map in per_device.items():
            if len(series_map) < 2:
                continue
            for res in eda.pairwise_correlation(series_map):
                a, b = res["pair"]
                if res["available"]:
                    rows.append({"series": f"{device}: {a} ↔ {b}",
                                 "status": "ok",
                                 "pearson_r": res["r"], "n_aligned": res["n"],
                                 "method": res["method"],
                                 "xcorr_peak": _cross_correlation_peak(
                                     series_map[a], series_map[b])})
                else:
                    rows.append({"series": f"{device}: {a} ↔ {b}",
                                 "status": "insufficient_data",
                                 "reason": res["reason"]})
        notes.append(
            "Pearson/Spearman coefficients are computed on time-aligned "
            "observations only (no interpolation), and a lagged peak is "
            "reported as association strength - it does not prove that one "
            "metric leads or causes the other.")

    if group_by_labels and dataset.label_kind != "none":
        for ref in refs:
            group = _label_group_test(dataset, ref)
            if group is None:
                continue
            rows.append({"series": str(ref), "status": "ok",
                         "test": "labelled-group difference (Mann-Whitney U)",
                         **group})
            if group.get("note"):
                notes.append(f"{ref}: {group['note']}")

    scored = [r for r in rows if r.get("status") == "ok"]
    described = [r for r in scored if r.get("acf") is not None]
    return TaskResult(
        task=task, status="ok",
        message=(f"statistical analysis over {len(described)} series "
                 f"({len(scored)} result rows including association and "
                 "difference tests)"),
        metrics={
            "series_described": len(described),
            "correlations_computed": any("pearson_r" in r for r in rows),
            "group_tests": sum(1 for r in rows if r.get("test")),
            "no_causal_claim": True},
        table=rows, points=points,
        warnings=notes,
        config={"lag_max": lag_max, "correlations": correlations,
                "group_by_labels": group_by_labels,
                "series": [str(r) for r in refs]},
        effective_features=sorted({r.metric for r in refs}))


def _unit_of(dataset, ref) -> str | None:
    for item in dataset.inventory:
        if item.ref.key == ref.key:
            return item.unit
    return None


def _trend(values: pd.Series) -> dict:
    y = values.to_numpy(dtype=float)
    x = np_arange(len(y))
    if len(y) < 3:
        return {"slope": None, "r2": None, "direction": "not enough points"}
    slope, intercept = polyfit(x, y, 1)
    predicted = slope * x + intercept
    ss_res = float(((y - predicted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = None if ss_tot <= 1e-15 else round(1 - ss_res / ss_tot, 4)
    scale = float(abs(y.mean())) or 1.0
    direction = ("flat" if r2 is None or abs(slope) < 0.001 * scale
                 else ("rising" if slope > 0 else "falling"))
    return {"slope": round(float(slope), 6), "r2": r2,
            "direction": direction}


def _autocorrelation(values: pd.Series, max_lag: int) -> list[float | None]:
    y = values.to_numpy(dtype=float)
    if len(y) < 4:
        return []
    y = y - y.mean()
    denom = float((y * y).sum())
    out: list[float | None] = []
    for lag in range(1, max(1, int(max_lag)) + 1):
        if lag >= len(y) or denom <= 1e-15:
            out.append(None)
            continue
        out.append(round(float((y[:-lag] * y[lag:]).sum() / denom), 4))
    return out


def _cross_correlation_peak(a: pd.Series, b: pd.Series) -> dict:
    """Best lagged Pearson association within +/- ``MAX_XCORR_LAG`` steps."""
    common = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    if len(common) < 8:
        return {"peak": None, "reason": "fewer than 8 aligned points"}
    best = {"peak": None, "lag": None}
    for lag in range(-MAX_XCORR_LAG, MAX_XCORR_LAG + 1):
        shifted = common["a"].shift(-lag)
        pair = pd.concat([shifted, common["b"]], axis=1).dropna()
        if len(pair) < 8:
            continue
        r = float(pair.corr().iloc[0, 1])
        if math.isnan(r):
            continue
        if best["peak"] is None or abs(r) > abs(best["peak"]):
            best = {"peak": round(r, 4), "lag": lag,
                    "n_aligned": int(len(pair))}
    best["note"] = ("largest |r| over a lag search; a non-zero lag shows "
                    "timing structure, not causation")
    return best


def _label_group_test(dataset, ref) -> dict | None:
    truth = lab_dataset.binary_truth(dataset, ref)
    if truth is None or not truth["anomaly"]:
        return None
    values = dataset.series_values(ref)
    anomaly_ts = {pd.Timestamp(t) for t in truth["anomaly"]}
    mask = pd.Series([ts in anomaly_ts for ts in values.index],
                     index=values.index)
    faulty = values[mask]
    normal = values[~mask]
    if len(faulty) < 3 or len(normal) < 3:
        return {"test_note": ("not compared: fewer than 3 observations in one "
                              "group (a tiny group is not evidence)")}
    from scipy import stats as sps
    try:
        statistic, p_value = sps.mannwhitneyu(faulty.to_numpy(),
                                              normal.to_numpy(),
                                              alternative="two-sided")
    except Exception as exc:  # pragma: no cover - scipy raises on degenerate input
        return {"test_note": f"test refused: {type(exc).__name__}"}
    pooled = math.sqrt((faulty.var(ddof=1) + normal.var(ddof=1)) / 2.0) \
        if len(faulty) > 1 and len(normal) > 1 else 0.0
    return {
        "n_anomaly": int(len(faulty)), "n_normal": int(len(normal)),
        "median_anomaly": round(float(faulty.median()), 6),
        "median_normal": round(float(normal.median()), 6),
        "mannwhitney_u": round(float(statistic), 4),
        "p_value": (float(p_value) if p_value >= 1e-12 else 0.0),
        "effect_size_ranks": (round(float((faulty.mean()
                                           - normal.mean()) / pooled), 4)
                              if pooled > 1e-12 else None),
        "label_basis": truth["basis"],
        "note": ("distribution difference between labelled and unlabelled "
                 "observations. The label set comes from "
                 f"{truth['basis']}; a difference is association, not a "
                 "cause."),
    }


def _best_row(rows: list[dict], key: str) -> str | None:
    scored = [r for r in rows if isinstance(r.get(key), (int, float))]
    if not scored:
        return None
    return str(min(scored, key=lambda r: (-r[key], r["detector"]))["detector"])


def _best_recall_at_precision(rows: list[dict], floor: float = 0.5) -> dict:
    eligible = [r for r in rows if isinstance(r.get("recall"), (int, float))
                and isinstance(r.get("precision"), (int, float))
                and r["precision"] >= floor]
    if not eligible:
        return {"status": "none_at_precision_floor",
                "precision_floor": floor}
    best = max(eligible, key=lambda r: (r["recall"], r["f1"] or 0))
    return {"detector": best["detector"], "series": best["series"],
            "recall": best["recall"], "precision": best["precision"],
            "precision_floor": floor}


def _isnan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _round(value, nd: int = 4) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else \
        round(number, nd)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# numpy access kept local so the module imports even if numpy is unexpected
# ---------------------------------------------------------------------------
def np_arange(n: int):
    import numpy as np
    return np.arange(n, dtype=float)


def polyfit(x, y, deg: int):
    import numpy as np
    coef = np.polyfit(x, y, deg)
    return float(coef[0]), float(coef[1])
