"""Chronological evaluation utilities (Phase 2 Scope E protocol).

Guarantees enforced here:

* splits are by TIME, never by random row shuffling;
* every training timestamp precedes every validation/test timestamp;
* preprocessing/parameter tuning may only reference train (and validation)
  segments - services must pass ``train``/``val`` slices to fit calls and
  the ``test`` slice only to scoring;
* metrics require a non-empty test segment, otherwise they are reported as
  unavailable (None) instead of a confident number.
"""
from __future__ import annotations

import dataclasses
import math

import pandas as pd


class LeakageError(AssertionError):
    """Raised when a protocol check detects out-of-order information use."""


def _finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


@dataclasses.dataclass
class ChronoSplit:
    train: pd.Series
    val: pd.Series
    test: pd.Series
    train_end: pd.Timestamp | None
    val_end: pd.Timestamp | None
    test_end: pd.Timestamp | None
    min_test_points: int = 5
    note: str = ""

    @property
    def has_test(self) -> bool:
        return len(self.test) >= self.min_test_points

    @property
    def has_val(self) -> bool:
        return len(self.val) >= 2


def chronological_split(
    series: pd.Series,
    *,
    test_frac: float = 0.2,
    val_frac: float = 0.15,
    min_train: int = 12,
    min_test: int = 5,
) -> ChronoSplit:
    """Split a chronologically ordered series into train|val|test blocks.

    The caller must provide values already sorted by time (the frames from
    ``analytics.time_series`` are). Raises nothing - short series simply
    yield empty segments and ``has_test`` False, which services translate
    into an "insufficient data" status.
    """
    s = pd.to_numeric(series, errors="coerce")
    s = s[s.map(_finite_number)]
    n = len(s)
    n_test = int(round(n * test_frac))
    n_val = int(round(n * val_frac))
    if n_test < min_test:
        n_test = min(min_test, max(0, n - min_train))
    if n - n_test - n_val < min_train:
        n_val = max(0, n - n_test - min_train)
    if n - n_test - n_val < min_train:  # not enough for any sensible split
        train = s
        val = s.iloc[0:0]
        test = s.iloc[0:0]
    else:
        train = s.iloc[: n - n_test - n_val]
        val = s.iloc[n - n_test - n_val: n - n_test]
        test = s.iloc[n - n_test:]
    split = ChronoSplit(
        train=train, val=val, test=test,
        train_end=train.index.max() if len(train) else None,
        val_end=val.index.max() if len(val) else None,
        test_end=test.index.max() if len(test) else None,
        min_test_points=min_test,
    )
    validate_split_order(split)
    return split


def validate_split_order(split: ChronoSplit) -> None:
    """Hard protocol check: train < val < test in timestamp order."""
    if split.train_end is not None and split.val is not None and len(split.val):
        if not split.train_end < split.val.index.min():
            raise LeakageError("validation data is not strictly after training data")
    if split.val_end is not None and len(split.test):
        if not split.val_end < split.test.index.min():
            raise LeakageError("test data is not strictly after validation data")
    elif split.train_end is not None and len(split.test) and not len(split.val):
        if not split.train_end < split.test.index.min():
            raise LeakageError("test data is not strictly after training data")


# ------------------------------------------------------------------ metrics
def mae(actual, predicted) -> float | None:
    a, p = _pairs(actual, predicted)
    if a is None or len(a) == 0:
        return None
    return float(sum(abs(x - y) for x, y in zip(a, p)) / len(a))


def mape(actual, predicted) -> float | None:
    """Mean absolute **percentage** error in percent, on a defensible subset.

    Actuals at (or very near) zero make the ratio explode, so those points are
    skipped rather than clipped. Returns None when nothing survives the guard -
    an unavailable metric is never reported as 0% error. A metric with an
    unbounded range (e.g. byte counters) can still be misleading; ``smape`` and
    ``mase`` are offered alongside it for exactly that reason.
    """
    a, p = _pairs(actual, predicted)
    if a is None:
        return None
    terms = [abs(x - y) / abs(x) for x, y in zip(a, p) if abs(x) > 1e-9]
    if not terms:
        return None
    return sum(terms) / len(terms) * 100.0


def smape(actual, predicted) -> float | None:
    """Symmetric MAPE (0-200%), bounded so a near-zero *prediction* cannot
    inflate it the way MAPE does. Points with an exactly-zero denominator
    (actual and prediction both 0) are skipped; None when nothing is scoreable.
    """
    a, p = _pairs(actual, predicted)
    if a is None:
        return None
    terms = []
    for x, y in zip(a, p):
        denom = abs(x) + abs(y)
        if denom > 1e-12:
            terms.append(2.0 * abs(x - y) / denom)
    if not terms:
        return None
    return sum(terms) / len(terms) * 100.0


def mase(actual, predicted, train) -> float | None:
    """Mean absolute scaled error (the forecast-competition standard).

    Scale = mean absolute **one-step difference of the training series**, i.e.
    what the naive model would have achieved in-sample. <1 beats the naive
    baseline on that history, >1 is worse than naive. Scale-free and unit-free,
    so it is comparable across metrics with very different magnitudes. None when
    the training history carries no variability (a flat series gives no
    defensible scale) or when there is nothing to score.
    """
    a, p = _pairs(actual, predicted)
    if a is None:
        return None
    # Difference before filtering: deleting a missing/non-finite point first
    # would turn its neighbours into a false one-step transition.
    t = pd.to_numeric(pd.Series(train), errors="coerce")
    differences = t.diff().abs()
    differences = differences[differences.map(_finite_number)]
    if differences.empty:
        return None
    scale = float(differences.mean())
    if scale <= 1e-12:
        return None            # flat training history: no honest scale exists
    numerator = sum(abs(x - y) for x, y in zip(a, p)) / len(a)
    return numerator / scale


def skill_vs_naive(actual, predicted, naive_predicted) -> float | None:
    """1 - MAE(model)/MAE(naive) on the *same* timestamps.

    Positive means the model beats the mandatory naive baseline on the holdout,
    0 means identical, negative means worse. None when the naive reference is
    missing or has zero error (a perfect naive baseline leaves no room to score
    skill, and dividing by it would be meaningless).
    """
    base = mae(actual, naive_predicted) if naive_predicted is not None else None
    model = mae(actual, predicted)
    if base is None or model is None or abs(base) <= 1e-12:
        return None
    return 1.0 - model / base


def rmse(actual, predicted) -> float | None:
    a, p = _pairs(actual, predicted)
    if a is None or len(a) == 0:
        return None
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, p)) / len(a))


def r2(actual, predicted) -> float | None:
    """R^2 - reported only when sample size and target variance make it
    meaningful (>= 3 points and non-constant actuals)."""
    a, p = _pairs(actual, predicted)
    if a is None or len(a) < 3:
        return None
    mean_a = sum(a) / len(a)
    ss_tot = sum((x - mean_a) ** 2 for x in a)
    ss_res = sum((x - y) ** 2 for x, y in zip(a, p))
    if ss_tot <= 1e-15:
        return None            # constant target: R^2 undefined
    return 1.0 - ss_res / ss_tot


def _pairs(actual, predicted):
    # Keep the original positional pairing. Dropping missing values from each
    # side independently can pair a prediction with the wrong observation.
    a = pd.to_numeric(pd.Series(actual), errors="coerce")
    p = pd.to_numeric(pd.Series(predicted), errors="coerce")
    if len(a) != len(p):
        return None, None
    pairs = [(float(x), float(y)) for x, y in zip(a, p)
             if _finite_number(x) and _finite_number(y)]
    if not pairs:
        return None, None
    return [x for x, _ in pairs], [y for _, y in pairs]


def common_finite_indices(actual, predictions) -> list[int]:
    """Indices scoreable by every candidate, preserving a shared test period."""
    values = pd.to_numeric(pd.Series(actual), errors="coerce").tolist()
    vectors = [pd.to_numeric(pd.Series(pred), errors="coerce").tolist()
               for pred in predictions]
    if not vectors:
        return []
    return [i for i, observed in enumerate(values)
            if _finite_number(observed)
            and all(i < len(pred) and _finite_number(pred[i])
                    for pred in vectors)]


@dataclasses.dataclass
class DetectionScore:
    """Supervised scoring of anomaly flags against LABELLED ground truth.

    Only meaningful on labelled (in practice: synthetic, documented)
    fixtures. Point matching rule: a true-anomaly timestamp is a hit when
    some flagged point has the same timestamp. Nothing here may be
    presented as real-network accuracy (no live labels exist).
    """

    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float | None
    recall: float | None
    f1: float | None
    false_alarm_rate: float | None


def score_detection(flagged_ts: set, true_anomaly_ts: set,
                    total_normal: int = 0) -> DetectionScore:
    """Point-matching rule: flagged timestamp == labelled anomaly timestamp.

    ``total_normal`` is the number of labelled normal observations in the
    scored universe (needed for a meaningful false-alarm rate); FAR is
    None when it is not supplied.
    """
    tp = len(flagged_ts & true_anomaly_ts)
    fp = len(flagged_ts - true_anomaly_ts)
    fn = len(true_anomaly_ts - flagged_ts)
    # A false positive is a *normal* observation that was flagged, so it is
    # already part of ``total_normal``. Subtracting the missed anomalies (fn)
    # would count them as normal and inflate the true-negative count.
    tn = max(0, total_normal - fp)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    far = fp / (fp + tn) if (fp + tn) else None
    return DetectionScore(tp, fp, fn, tn, precision, recall, f1, far)
