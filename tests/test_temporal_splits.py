"""Phase 2 Scope E protocol tests: chronological splits + leakage guarantees.

Synthetic fixtures only; they verify the documented protocol, not
real-world model performance.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest

from analytics import evaluation


def time_series(n=60, step_s=60, start=dt.datetime(2026, 1, 1)):
    idx = pd.to_datetime([start + dt.timedelta(seconds=i * step_s)
                          for i in range(n)])
    return pd.Series([float(i) for i in range(n)], index=idx)


def test_split_is_chronological_never_shuffled():
    s = time_series(60)
    split = evaluation.chronological_split(s, min_train=12, min_test=5)
    assert len(split.train) + len(split.val) + len(split.test) == 60
    assert split.train.index.max() < split.val.index.min()
    assert split.val.index.max() < split.test.index.min()
    # train is literally the FIRST block (values 0..k)
    assert split.train.iloc[0] == 0.0
    assert split.test.iloc[-1] == 59.0


def test_split_rejects_leaky_construction():
    s = time_series(40)
    split = evaluation.chronological_split(s)
    # manually corrupt: val timestamps before train end -> validator raises
    split.val = split.train.head(2)
    with pytest.raises(evaluation.LeakageError):
        evaluation.validate_split_order(split)


def test_short_series_yields_no_test_segment():
    s = time_series(10)
    split = evaluation.chronological_split(s, min_train=12, min_test=5)
    assert not split.has_test
    assert len(split.test) < 5


def test_metrics_values_exact():
    actual = [10.0, 20.0, 30.0, 40.0]
    pred = [12.0, 18.0, 35.0, 40.0]
    assert evaluation.mae(actual, pred) == pytest.approx((2 + 2 + 5 + 0) / 4)
    assert evaluation.rmse(actual, pred) == pytest.approx(
        math.sqrt((4 + 4 + 25 + 0) / 4))
    r2 = evaluation.r2(actual, pred)
    assert r2 is not None and r2 < 1.0


def test_metrics_none_on_empty_or_mismatched_segments():
    assert evaluation.mae([], []) is None
    assert evaluation.rmse([1.0, 2.0], [1.0]) is None
    # R^2 refused for constant target (undefined) and tiny samples
    assert evaluation.r2([5.0, 5.0, 5.0], [5.0, 5.1, 4.9]) is None
    assert evaluation.r2([1.0, 2.0], [1.0, 3.0]) is None  # n < 3


def test_detection_score_point_matching_synthetic():
    # SYNTHETIC labelled fixture: verifies the matching rule, NOT accuracy.
    flagged = {"t1", "t2", "t9"}
    truth = {"t1", "t3"}
    score = evaluation.score_detection(flagged, truth, total_normal=97)
    assert score.true_positives == 1
    assert score.false_positives == 2
    assert score.false_negatives == 1
    assert score.precision == pytest.approx(1 / 3)
    assert score.recall == pytest.approx(1 / 2)
    assert score.f1 == pytest.approx(2 * (1 / 3) * (1 / 2) / (1 / 3 + 1 / 2))
    assert score.false_alarm_rate == pytest.approx(2 / 98)


def test_detection_score_empty_universe():
    score = evaluation.score_detection(set(), set())
    assert score.precision is None and score.recall is None
    assert score.f1 is None
