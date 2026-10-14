"""Tests for v2 Slice 4a multivariate anomaly detection (analytics.ml_anomaly).

Deterministic (seeded). Verifies the detector catches a *joint* deviation,
honours the complete-case rule, abstains on short history, and never claims
an observed raw value for a multivariate flag.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analytics import ml_anomaly


def _frame(n=40, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-10-13", periods=n, freq="min")
    data = {
        "ping.avg_rtt_ms": rng.normal(20, 1.0, n),
        "ping.loss_pct": rng.normal(0.5, 0.2, n),
        "if.rx_bytes": rng.normal(1000, 40, n),
    }
    return pd.DataFrame(data, index=idx), pd.Series(idx)


def test_flags_joint_outlier_missed_by_univariate():
    feats, ts = _frame()
    # A correlated spike: each metric only ~2-3 sigma alone, but all together.
    feats.loc[feats.index[30]] = [23.5, 1.1, 1150]
    feats.loc[feats.index[31]] = [24.0, 1.2, 1170]
    points = ml_anomaly.detect_multivariate_isolation_forest(
        feats, ts, device_name="R1", data_source="demo", min_samples=20)
    flagged = {p.timestamp for p in points}
    # At least one of the injected joint outliers is caught.
    assert flagged & {ts.iloc[30], ts.iloc[31]}
    # Every flag is a multivariate record: observed is never a raw measurement.
    for p in points:
        assert p.detector == "mv_isolation_forest"
        assert p.metric.startswith("multivariate:")
        assert p.observed is None
        assert p.deviation is not None


def test_abstains_on_short_history():
    feats, ts = _frame(n=10)
    assert ml_anomaly.detect_multivariate_isolation_forest(
        feats, ts, device_name="R1", min_samples=20) == []


def test_nan_rows_are_not_judged():
    feats, ts = _frame()
    feats.loc[feats.index[15], "if.rx_bytes"] = np.nan
    points = ml_anomaly.detect_multivariate_isolation_forest(
        feats, ts, device_name="R1", min_samples=20)
    assert ts.iloc[15] not in {p.timestamp for p in points}


def test_rejects_empty_and_mismatched_input():
    feats, ts = _frame()
    with pytest.raises(ValueError):
        ml_anomaly.detect_multivariate_isolation_forest(
            pd.DataFrame(), ts, device_name="R1")
    with pytest.raises(ValueError):
        ml_anomaly.detect_multivariate_isolation_forest(
            feats, ts.iloc[:5], device_name="R1")


def test_seeded_and_reproducible():
    feats, ts = _frame()
    feats.loc[feats.index[25]] = [30.0, 1.5, 1300]
    a = ml_anomaly.detect_multivariate_isolation_forest(
        feats, ts, device_name="R1")
    b = ml_anomaly.detect_multivariate_isolation_forest(
        feats, ts, device_name="R1")
    assert [p.timestamp for p in a] == [p.timestamp for p in b]
