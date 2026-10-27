"""Enhanced detector tests (EWMA, CUSUM, seasonal-residual) + registry routing.

Every fixture here is SYNTHETIC and documented: these tests verify detector
*behaviour* (does the maths do what its docstring claims, does it abstain when
it cannot) - they are NOT evidence of detection performance on a real network,
because no labelled ground truth exists for one.
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest

from analytics.anomaly_detection import (
    DEFAULT_DETECTORS,
    DETECTORS,
    INTERVAL_REQUIRED,
    SENSITIVITY_DEFAULTS,
    detect_frame,
    detect_seasonal_residual,
)

BASE = dt.datetime(2026, 1, 1, 0, 0, 0)
STEP_S = 60

# Deterministic, zero-mean jitter (population std ~= 0.33) so every expected
# flag index is reproducible without relying on a random seed.
JITTER = [0.3, -0.4, 0.2, -0.3, 0.5, -0.2, 0.4, -0.5, 0.1, -0.1]


def make_frame(values, step_s=STEP_S):
    n = len(values)
    return pd.DataFrame({
        "sample_id": list(range(1, n + 1)),
        "collected_at": pd.to_datetime(
            [BASE + dt.timedelta(seconds=i * step_s) for i in range(n)]),
        "value": [None if v is None else float(v) for v in values],
        "unit": "ms", "data_source": "live", "imputed": [False] * n,
    })


def steady(n, level=10.0):
    return [level + JITTER[i % len(JITTER)] for i in range(n)]


def shifted(n, level=10.0, step=0.5):
    return [level + step + JITTER[i % len(JITTER)] for i in range(n)]


def index_of(ts) -> int:
    return int((ts - BASE).total_seconds() // STEP_S)


GENTLE = steady(20)
SPIKE_AT = 17
SPIKE_SERIES = GENTLE[:SPIKE_AT] + [500.0] + GENTLE[SPIKE_AT + 1:]


def run_detector(name, frame, **kwargs):
    return DETECTORS[name](frame["value"], frame["collected_at"],
                           device_name="R1", metric="ping.avg_rtt_ms",
                           **kwargs)


# ------------------------------------------------------------------- registry
def test_registry_exposes_all_six_and_defaults_stay_the_documented_pair():
    assert set(DETECTORS) == {"rolling_mad", "rolling_zscore", "ewma", "cusum",
                              "seasonal_residual", "isolation_forest"}
    # The always-on pair must not be silently widened by adding new detectors.
    assert DEFAULT_DETECTORS == ("rolling_mad", "rolling_zscore")
    assert INTERVAL_REQUIRED == ("seasonal_residual",)
    assert set(SENSITIVITY_DEFAULTS) == {"ewma", "cusum", "seasonal_residual"}


def test_detect_frame_defaults_to_the_two_baseline_detectors():
    frame = make_frame(SPIKE_SERIES)
    points = detect_frame(frame, device_name="R1", metric="m")
    assert points, "the fixture spike should be caught by the baselines"
    assert {p.detector for p in points} <= set(DEFAULT_DETECTORS)


def test_detect_frame_rejects_unknown_detector_instead_of_ignoring_it():
    frame = make_frame(SPIKE_SERIES)
    with pytest.raises(ValueError, match="unknown detector"):
        detect_frame(frame, device_name="R1", metric="m",
                     detectors=("rolling_mad", "not_a_detector"))


def test_detect_frame_empty_frame_returns_nothing():
    assert detect_frame(pd.DataFrame(), device_name="R1", metric="m") == []


def test_detect_frame_aggregates_and_sorts_the_selected_detectors():
    frame = make_frame(SPIKE_SERIES)
    points = detect_frame(
        frame, device_name="R1", metric="m",
        detectors=("rolling_mad", "ewma", "cusum"),
        window=8, ewma_threshold=3.0, cusum_drift=0.5, cusum_limit=4.0)
    assert points
    keys = [(p.timestamp, p.detector) for p in points]
    assert keys == sorted(keys)
    assert {"rolling_mad", "ewma"} <= {p.detector for p in points}


def test_detectors_label_provenance_and_scope_they_were_given():
    frame = make_frame(SPIKE_SERIES)
    ewma = run_detector("ewma", frame, window=8, threshold=3.0,
                        data_source="demo", interface_name="eth1")
    cusum = run_detector("cusum", frame, window=8, drift=0.5, limit=4.0,
                         data_source="demo", interface_name="eth1")
    for points in (ewma, cusum):
        assert points
        for p in points:
            assert p.data_source == "demo"
            assert p.interface_name == "eth1"


# ----------------------------------------------------------------------- EWMA
def test_ewma_flags_spike_against_its_exponentially_weighted_past():
    frame = make_frame(SPIKE_SERIES)
    points = run_detector("ewma", frame, window=8, threshold=3.0)
    flagged = [p for p in points if index_of(p.timestamp) == SPIKE_AT]
    assert flagged, "the synthetic spike must be flagged"
    p = flagged[0]
    assert p.detector == "ewma"
    assert p.observed == 500.0
    assert p.baseline < 20.0                  # the past, never the spike itself
    assert p.deviation > 3.0
    assert "EWMA residual scale" in p.explanation


def test_ewma_baseline_is_the_recursive_strictly_past_average():
    """Recomputed by hand: b_1 = v_0, b_t = b_(t-1) + alpha*(v_(t-1) - b_(t-1)).

    A value may never pull its own baseline, so the expected reference at the
    flagged index uses values[0..SPIKE_AT-1] only.
    """
    lam = 0.3
    values = [float(v) for v in SPIKE_SERIES]
    baseline = values[0]
    for i in range(1, SPIKE_AT):
        baseline = baseline + lam * (values[i] - baseline)
    frame = make_frame(SPIKE_SERIES)
    p = next(q for q in run_detector("ewma", frame, window=8, threshold=3.0)
             if index_of(q.timestamp) == SPIKE_AT)
    assert p.baseline == pytest.approx(baseline, rel=1e-9)
    assert p.baseline != values[SPIKE_AT]


def test_ewma_abstains_on_a_flat_series_instead_of_flagging_noise():
    frame = make_frame([10.0] * 30)
    assert run_detector("ewma", frame, window=8, threshold=1.5) == []


def test_ewma_is_monotone_in_its_sensitivity_knob():
    frame = make_frame(SPIKE_SERIES)
    lenient = run_detector("ewma", frame, window=8, threshold=1.5)
    strict = run_detector("ewma", frame, window=8, threshold=6.0)
    assert len(strict) <= len(lenient)
    assert {p.timestamp for p in strict} <= {p.timestamp for p in lenient}


def test_ewma_records_an_auditable_half_life_not_a_magic_number():
    frame = make_frame(SPIKE_SERIES)
    p = run_detector("ewma", frame, window=8, threshold=3.0, lambda_=0.4)[0]
    cfg = p.detector_config
    assert cfg["lambda"] == pytest.approx(0.4)
    assert cfg["half_life_samples"] == pytest.approx(
        math.log(0.5) / math.log(1.0 - 0.4))
    assert cfg["window"] == 8 and cfg["threshold"] == 3.0
    assert "half-life" in p.explanation


def test_ewma_clamps_an_absurd_smoothing_factor():
    frame = make_frame(SPIKE_SERIES)
    p = run_detector("ewma", frame, window=8, threshold=3.0, lambda_=5.0)[0]
    assert 0.02 <= p.detector_config["lambda"] <= 0.95


def test_ewma_never_judges_a_missing_sample():
    values = list(SPIKE_SERIES)
    values[5] = None
    frame = make_frame(values)
    points = run_detector("ewma", frame, window=8, threshold=1.0)
    assert points
    assert 5 not in {index_of(p.timestamp) for p in points}


# ---------------------------------------------------------------------- CUSUM
SHIFT_START = 30
SHIFT = 0.5                # ~2 sigma of the past window: modest per point
CUSUM_SERIES = steady(SHIFT_START) + shifted(10, step=SHIFT)


def cusum(values, **kwargs):
    frame = make_frame(values)
    kwargs.setdefault("window", 20)
    kwargs.setdefault("drift", 0.5)
    kwargs.setdefault("limit", 4.0)
    return frame, run_detector("cusum", frame, **kwargs)


def test_cusum_catches_a_sustained_shift_a_zscore_test_is_blind_to():
    frame, points = cusum(CUSUM_SERIES)
    assert points, "a sustained ~2 sigma shift must accumulate"
    z = run_detector("rolling_zscore", frame, window=20, threshold=3.0)
    assert z == [], ("every shifted point alone is inside 3 sigma - that is "
                     "exactly the blindness CUSUM exists to remove")
    assert min(index_of(p.timestamp) for p in points) >= SHIFT_START


def test_cusum_reports_one_flag_per_episode_not_one_per_sample():
    _, points = cusum(steady(30) + shifted(30, step=SHIFT))
    assert 1 <= len(points) < 15, (
        "the statistic resets after a flag, so an episode is not counted once "
        "per sample")


def test_cusum_is_deterministic():
    _, a = cusum(CUSUM_SERIES)
    _, b = cusum(CUSUM_SERIES)
    assert [index_of(p.timestamp) for p in a] == [index_of(p.timestamp)
                                                  for p in b]
    assert [p.deviation for p in a] == [p.deviation for p in b]


def test_cusum_run_cannot_be_stitched_across_a_gap():
    """Two shifted samples are not enough on their own, and a missing sample
    discards the evidence accumulated before it instead of hopping over it."""
    _, continuous = cusum(steady(30) + shifted(5, step=SHIFT))
    _, after_gap = cusum(steady(30) + shifted(2, step=SHIFT) + [None]
                         + shifted(2, step=SHIFT))
    assert continuous, "a 5-sample run is enough to cross the boundary"
    assert after_gap == [], ("2 + (gap) + 2 samples must not be read as a "
                             "4-sample run")


def test_cusum_abstains_on_a_flat_window():
    frame = make_frame([10.0] * 40)
    assert run_detector("cusum", frame, window=20, drift=0.5, limit=4.0) == []


def test_cusum_carries_the_boundary_it_crossed_and_its_direction():
    _, points = cusum(steady(30) + shifted(12, step=SHIFT))
    assert points
    p = points[0]
    assert p.detector == "cusum"
    assert p.deviation >= 4.0                      # the decision boundary
    assert p.threshold_upper is not None and p.threshold_lower is None
    assert p.threshold_upper > p.baseline
    assert "CUSUM statistic" in p.explanation
    assert "statistic reset after this flag" in p.explanation
    assert p.detector_config["drift"] == 0.5
    assert p.detector_config["limit"] == 4.0
    assert p.detector_config["side"] == "two-sided"


def test_cusum_below_side_flags_with_a_lower_threshold_only():
    _, points = cusum(steady(30) + shifted(12, step=-SHIFT))
    assert points
    p = points[0]
    assert p.threshold_lower is not None and p.threshold_upper is None
    assert p.baseline > p.observed
    assert "below" in p.explanation


def test_cusum_severity_escalates_only_at_twice_the_boundary():
    saw_critical = saw_warning = False
    # step=0.5 creeps over the boundary (warning); step=3.0 smashes straight
    # through twice the boundary in a single sample (critical).
    for drift, limit, step in ((0.5, 4.0, 0.5), (0.5, 4.0, 0.8),
                               (0.5, 4.0, 3.0)):
        _, points = cusum(steady(30) + shifted(30, step=step),
                          drift=drift, limit=limit)
        assert points
        for p in points:
            expected = "critical" if p.deviation >= 2.0 * limit else "warning"
            assert p.severity == expected
            saw_critical |= expected == "critical"
            saw_warning |= expected == "warning"
    assert saw_warning and saw_critical, ("the sweep must exercise both "
                                          "severity branches")


def test_a_farther_boundary_can_stop_producing_a_verdict_at_all():
    _, lenient = cusum(CUSUM_SERIES, limit=3.0)
    _, strict = cusum(CUSUM_SERIES, limit=9.0)
    assert lenient
    assert strict == [], ("with this much noise the run never reaches 9 "
                          "sigma - an honest absence, not a clean result")


# ------------------------------------------------------------ seasonal residual
SEASON = 3
SEASON_HOURS = SEASON * STEP_S / 3600.0
SEASONAL = [[10.0, 20.0, 30.0][p] + 0.5 * c for c in range(8) for p in range(3)]
OUTLIER_AT = 7 * SEASON            # cycle 7, phase 0 -> index 21
SEASONAL_WITH_OUTLIER = list(SEASONAL)
SEASONAL_WITH_OUTLIER[OUTLIER_AT] = 100.0


def seasonal(values, **kwargs):
    frame = make_frame(values)
    kwargs.setdefault("interval_seconds", STEP_S)
    kwargs.setdefault("season_hours", SEASON_HOURS)
    return frame, run_detector("seasonal_residual", frame, **kwargs)


def test_seasonal_residual_flags_what_is_unusual_for_that_phase():
    frame, points = seasonal(SEASONAL_WITH_OUTLIER, threshold=3.5)
    assert [index_of(p.timestamp) for p in points] == [OUTLIER_AT]
    p = points[0]
    # baseline = median of the 7 strictly past phase-0 samples (10.0 .. 13.0)
    assert p.baseline == pytest.approx(11.5)
    assert p.detector_config["season_len_samples"] == SEASON
    assert p.detector_config["phase"] == 0
    assert f"phase 0/{SEASON}" in p.explanation
    assert "assumed regular grid" in p.explanation


def test_seasonal_residual_sees_a_normal_ramp_as_normal():
    _, points = seasonal(SEASONAL, threshold=3.5)
    assert points == []


def test_seasonal_residual_refuses_without_a_known_interval():
    _, points = seasonal(SEASONAL_WITH_OUTLIER, interval_seconds=None)
    assert points == []
    _, zero = seasonal(SEASONAL_WITH_OUTLIER, interval_seconds=0)
    assert zero == []


def test_seasonal_residual_refuses_a_season_shorter_than_two_samples():
    _, points = seasonal(SEASONAL_WITH_OUTLIER, season_hours=0.005)
    assert points == []


def test_seasonal_residual_abstains_until_it_has_same_phase_history():
    _, points = seasonal(SEASONAL[:9])
    assert points == [], "3 cycles cannot establish a phase baseline"
    _, late = seasonal(SEASONAL, min_hist=4)
    assert late == []


def test_seasonal_baseline_uses_only_strictly_past_same_phase_samples():
    """The first judged phase-0 sample (index 12) must see exactly 10, 10.5,
    11 and 11.5 - the 60 in its own row may not lift its baseline."""
    probe = list(SEASONAL)
    probe[12] = 60.0
    frame, points = seasonal(probe, min_hist=4)
    assert [index_of(p.timestamp) for p in points] == [12]
    assert points[0].baseline == pytest.approx(10.75)


def test_seasonal_detector_is_opt_in_and_abstains_inside_detect_frame():
    frame = make_frame(SEASONAL_WITH_OUTLIER)
    without_interval = detect_frame(
        frame, device_name="R1", metric="m",
        detectors=("rolling_mad", "seasonal_residual"), window=8)
    assert "seasonal_residual" not in {p.detector for p in without_interval}
    with_interval = detect_frame(
        frame, device_name="R1", metric="m", detectors=("seasonal_residual",),
        interval_seconds=STEP_S, season_hours=SEASON_HOURS)
    assert {p.detector for p in with_interval} == {"seasonal_residual"}


def test_detect_frame_routes_the_seasonal_threshold():
    frame = make_frame(SEASONAL_WITH_OUTLIER)
    kwargs = dict(device_name="R1", metric="m",
                  detectors=("seasonal_residual",),
                  interval_seconds=STEP_S, season_hours=SEASON_HOURS)
    assert detect_frame(frame, seasonal_threshold=2.0, **kwargs)
    assert detect_frame(frame, seasonal_threshold=1000.0, **kwargs) == []


def test_missing_samples_never_enter_the_seasonal_history():
    values = list(SEASONAL_WITH_OUTLIER)
    values[3] = None                       # one phase-0 sample goes missing
    frame, points = seasonal(values)
    assert 3 not in {index_of(p.timestamp) for p in points}
    assert [index_of(p.timestamp) for p in points] == [OUTLIER_AT]
    # the six surviving phase-0 predecessors have a median of 11.75
    assert points[0].baseline == pytest.approx(11.75)


def test_seasonal_residual_returns_no_verdict_for_a_short_series():
    """A season longer than the series cannot be measured: abstain, empty."""
    frame = make_frame([10.0, 20.0, 30.0, 11.0])
    points = detect_seasonal_residual(
        frame["value"], frame["collected_at"], device_name="R1", metric="m",
        interval_seconds=STEP_S, season_hours=24.0)
    assert points == []
