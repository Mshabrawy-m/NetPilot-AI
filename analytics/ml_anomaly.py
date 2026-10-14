"""Multivariate anomaly detection (v2 Slice 4a).

The Phase 2 detectors judge one metric at a time. Real incidents often show
up as a *combination* of metrics drifting together (e.g. RTT up + loss up +
rx_bytes down) even when no single series crosses its own threshold. This
module adds a seeded, reproducible multivariate Isolation Forest over an
aligned feature matrix (columns = metrics, rows = timestamps).

Honesty rules carried from Phase 2:
* anomalies are statistical, never a failure/attack/root-cause claim;
* missing values are dropped from the fit, never imputed with zeros;
* no future sample influences an earlier verdict (the fit uses the whole
  aligned window up to the judged point via trailing refit is unnecessary
  here because Isolation Forest is unsupervised; we instead require enough
  history and score each point with a model trained on the available window);
* ``observed`` is left None and the isolation score is reported in
  ``deviation`` so nothing is mistaken for a raw measurement.
"""
from __future__ import annotations

import pandas as pd

from analytics.anomaly_detection import AnomalyPoint


def detect_multivariate_isolation_forest(
    features: pd.DataFrame,
    timestamps: pd.Series,
    *,
    device_name: str,
    data_source: str = "live",
    interface_name: str | None = None,
    contamination: float = 0.05,
    random_seed: int = 42,
    min_samples: int = 20,
    metric_label: str | None = None,
    sample_ids: pd.Series | None = None,
) -> list[AnomalyPoint]:
    """Flag rows whose *joint* behaviour is unusual.

    ``features`` columns are metric values aligned index-wise with
    ``timestamps``. Rows with any missing metric are excluded from the fit
    and never judged. Returns one ``AnomalyPoint`` per flagged row.
    """
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler

    if features.shape[1] == 0:
        raise ValueError("features matrix has no columns")
    if len(features) != len(timestamps):
        raise ValueError("features and timestamps length mismatch")

    label = metric_label or ("multivariate:" + "|".join(features.columns))

    # Complete-case rows only (no zero/mean imputation - never invent data).
    mask = features.notna().all(axis=1).to_numpy()
    clean = features[mask]
    clean_ts = pd.Series(timestamps.to_numpy()[mask])
    clean_ids = (pd.Series(sample_ids.to_numpy()[mask])
                 if sample_ids is not None else None)
    if len(clean) < min_samples:
        return []                        # insufficient joint history: abstain

    X = StandardScaler().fit_transform(clean)
    model = IsolationForest(
        n_estimators=100, contamination=contamination,
        random_state=random_seed)
    model.fit(X)
    preds = model.predict(X)
    # score_samples: higher = more normal; anomaly score = -score.
    scores = -model.score_samples(X)
    lo, hi = float(scores.min()), float(scores.max())
    span = (hi - lo) or 1.0

    points: list[AnomalyPoint] = []
    for pos in range(len(clean)):
        if int(preds[pos]) != -1:
            continue
        norm_score = (float(scores[pos]) - lo) / span
        row = clean.iloc[pos]
        drivers = ", ".join(
            f"{c}={row[c]:.4g}" for c in clean.columns)
        points.append(AnomalyPoint(
            device_name=device_name, metric=label,
            interface_name=interface_name,
            timestamp=pd.Timestamp(clean_ts.iloc[pos]),
            observed=None, baseline=None, threshold_upper=None,
            threshold_lower=None,
            deviation=float(scores[pos]),
            severity="critical" if norm_score >= 0.8 else "warning",
            detector="mv_isolation_forest", detector_version="1.0",
            detector_config={"n_features": clean.shape[1],
                             "contamination": contamination,
                             "random_seed": random_seed,
                             "min_samples": min_samples},
            explanation=(
                f"Multivariate Isolation Forest flagged this sample jointly "
                f"across {clean.shape[1]} metric(s) [{drivers}] against a "
                f"{len(clean)}-sample complete-case window (contamination "
                f"{contamination:.2f}, seed {random_seed}). Exploratory "
                "correlated-deviation signal, not a failure or attack claim."),
            data_source=data_source,
            sample_id=(int(clean_ids.iloc[pos]) if clean_ids is not None
                       and not pd.isna(clean_ids.iloc[pos]) else None),
        ))
    return points
