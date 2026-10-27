"""AI Lab unified telemetry representation: validation, lineage, split safety.

Covers acceptance criteria 8.3 (validation cases), 8.4 (source identity),
8.5 (chronological splits without future leakage) and 8.6 (preprocessing
fitted on training data only).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from analytics import lab_dataset
from analytics.evaluation import LeakageError
from tests.ai_lab_fixtures import BASE, dataset, frame_of, long_rows


# ---------------------------------------------------------------- timestamps
def test_parse_timestamps_handles_iso_and_garbage():
    parsed = lab_dataset.parse_timestamps(
        ["2026-10-01T00:00:00Z", "2026-10-01T00:01:00Z", "not a date", None])
    assert parsed.iloc[0] == pd.Timestamp("2026-10-01T00:00:00", tz="UTC")
    assert parsed.iloc[1] == pd.Timestamp("2026-10-01T00:01:00", tz="UTC")
    assert pd.isna(parsed.iloc[2]) and pd.isna(parsed.iloc[3])


def test_naive_timestamp_column_is_read_as_utc():
    parsed = lab_dataset.parse_timestamps(["2026-10-01 00:01:00",
                                           "2026-10-01 00:02:00"])
    assert parsed.iloc[0] == pd.Timestamp("2026-10-01T00:01:00", tz="UTC")


def test_utc_offsets_are_converted_and_an_unmixable_row_is_rejected_not_repaired():
    """A uniform offset column becomes UTC; a row pandas cannot place is NaT.

    NaT is what the ingest path counts and reports as rejected, so a failed
    parse never turns into an invented timestamp. Mixing aware and naive shapes
    in one column is exactly that case.
    """
    uniform = lab_dataset.parse_timestamps(["2026-10-01 00:01:00+02:00",
                                            "2026-10-01 00:02:00+02:00"])
    assert uniform.iloc[0] == pd.Timestamp("2026-09-30T22:01:00", tz="UTC")
    assert uniform.iloc[1] == pd.Timestamp("2026-09-30T22:02:00", tz="UTC")
    mixed = lab_dataset.parse_timestamps(["2026-10-01T00:01:00Z",
                                          "2026-10-01 00:02:00+02:00"])
    assert pd.isna(mixed.iloc[1])


@pytest.mark.parametrize("scale", [1, 1000, 1_000_000, 1_000_000_000])
def test_epoch_columns_are_read_in_the_unit_implied_by_their_magnitude(scale):
    epoch = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()
    values = [epoch * scale, (epoch + 60) * scale]
    series = lab_dataset.parse_timestamps(
        pd.Series([int(v) for v in values]))
    assert series.iloc[0] == pd.Timestamp("2026-10-01T00:00:00", tz="UTC")
    assert series.iloc[1] - series.iloc[0] == pd.Timedelta(seconds=60)


def test_datetime_column_is_not_rescaled_into_an_epoch():
    """A datetime64 column must survive unchanged.

    ``pd.to_numeric`` would turn it into microseconds and the epoch branch
    would then shift every row by tens of thousands of years.
    """
    series = pd.Series(pd.date_range("2026-10-01", periods=4, freq="min",
                                     tz="UTC"))
    parsed = lab_dataset.parse_timestamps(series)
    assert parsed.iloc[0] == series.iloc[0]
    assert parsed.iloc[-1] == series.iloc[-1]


def test_timedelta_column_is_refused_instead_of_becoming_a_date():
    parsed = lab_dataset.parse_timestamps(
        pd.Series(pd.to_timedelta(["1 min", "2 min"])))
    assert bool(parsed.isna().all())


# ------------------------------------------------------------- canonical form
def test_source_identity_survives_the_whole_pipeline():
    ds = dataset(frame_of(long_rows()), source_type=lab_dataset.SOURCE_UPLOADED,
                 source_id="lab_upload.csv")
    assert ds.source_type == "uploaded"
    assert ds.source_id == "lab_upload.csv"
    assert ds.provenance["source_type"] == "uploaded"
    assert ds.provenance["source_id"] == "lab_upload.csv"
    assert ds.provenance["label"] == lab_dataset.SOURCE_LABELS["uploaded"]
    # and it is carried into the per-series frame handed to the models
    frame = ds.series_frame(ds.series_refs()[0])
    assert set(frame["data_source"]) == {"uploaded"}
    # which is deliberately outside the operational {"demo","live"} vocabulary
    assert "demo" not in set(frame["data_source"])


def test_rows_are_chronological_and_duplicate_timestamps_are_counted():
    rows = long_rows(n=12)
    rows.reverse()                                    # shuffled input
    rows.append(dict(rows[-1]))                       # exact duplicate
    ds = dataset(frame_of(rows))
    ref = ds.series_refs()[0]
    stamps = ds.series_frame(ref)["collected_at"]
    assert stamps.is_monotonic_increasing
    assert ds.profile["n_rows"] == 12                 # duplicate collapsed
    assert any("duplicate timestamp" in w.lower() for w in ds.warnings)
    assert [step.step for step in ds.lineage].count("dedupe") == 1


def test_unparseable_timestamps_are_rejected_not_repaired():
    rows = long_rows(n=12)
    rows[3]["collected_at"] = "yesterday-ish"
    ds = dataset(frame_of(rows))
    assert ds.profile["n_rows"] == 11
    assert any("unparseable timestamp" in w for w in ds.warnings)


def test_non_numeric_value_becomes_missing_not_zero():
    rows = long_rows(n=12, value=lambda pos: (None if pos == 5 else
                                              ("high" if pos == 6 else
                                               10.0 + pos)))
    ds = dataset(frame_of(rows))
    values = ds.series_values(ds.series_refs()[0])
    assert 0.0 not in list(values)                    # never fabricated
    assert ds.profile["n_rows"] == 12
    assert ds.profile["n_values"] == 10
    assert ds.profile["n_missing_values"] == 2
    assert any("kept as missing" in w for w in ds.warnings)


def test_mixed_units_are_quarantined_instead_of_averaged():
    rows = long_rows(n=12, metric="ping.avg_rtt_ms", unit="ms")
    rows += long_rows(n=12, metric="if.rx_bps", unit="bps")
    # same device/metric, different unit: a byte counter and a packet counter
    # are not one series, so nothing may average them into a single curve
    rows += long_rows(n=12, metric="if.rx_bps", unit="pps",
                      start=BASE + dt.timedelta(hours=1))
    ds = dataset(frame_of(rows))
    assert any("mixed units" in w for w in ds.warnings)
    kept = {(item.ref.device, item.ref.interface, item.ref.metric)
            for item in ds.inventory}
    assert ("R1", "eth0", "if.rx_bps") not in kept
    assert ("R1", "eth0", "ping.avg_rtt_ms") in kept
    assert "unit-check" in [step.step for step in ds.lineage]


def test_oversized_source_is_refused_rather_than_silently_truncated():
    with pytest.raises(lab_dataset.LabDataError) as exc:
        lab_dataset.canonicalize(frame_of(long_rows(n=48)),
                                 source_type=lab_dataset.SOURCE_UPLOADED,
                                 source_id="big.csv", max_rows=20)
    assert "bound" in str(exc.value).lower()


@pytest.mark.parametrize("frame,expect", [
    (pd.DataFrame([{"collected_at": BASE, "device": "R1", "value": 1.0}]),
     "metric"),
    (pd.DataFrame([{"collected_at": BASE, "metric": "rtt", "value": 1.0}]),
     "device"),
])
def test_missing_required_columns_are_named_in_the_error(frame, expect):
    with pytest.raises(lab_dataset.LabDataError) as exc:
        lab_dataset.canonicalize(frame,
                                 source_type=lab_dataset.SOURCE_UPLOADED,
                                 source_id="bad.csv")
    assert expect in str(exc.value)


def test_empty_and_unknown_source_types_are_refused():
    with pytest.raises(lab_dataset.LabDataError):
        lab_dataset.canonicalize(pd.DataFrame(columns=lab_dataset.LONG_COLUMNS),
                                 source_type=lab_dataset.SOURCE_UPLOADED,
                                 source_id="empty.csv")
    with pytest.raises(lab_dataset.LabDataError):
        lab_dataset.canonicalize(frame_of(long_rows(n=6)),
                                 source_type="guess", source_id="x")
    with pytest.raises(lab_dataset.LabDataError):
        lab_dataset.canonicalize(frame_of(long_rows(n=6)),
                                 source_type=lab_dataset.SOURCE_LIVE,
                                 source_id="")


def test_metric_alias_is_applied_and_recorded_in_lineage():
    ds = dataset(frame_of(long_rows(n=12, metric="rtt")),
                 metric_aliases={"rtt": "ping.avg_rtt_ms"})
    assert ds.metrics() == ["ping.avg_rtt_ms"]
    step = [s for s in ds.lineage if s.step == "metric-alias"]
    assert step and "1 alias rule" in step[0].detail


# ------------------------------------------------------------------- labels
def test_binary_spellings_become_a_flag_and_class_names_are_preserved():
    binary = dataset(frame_of(long_rows(
        n=12, label=lambda pos: "anomaly" if pos >= 10 else "normal")))
    assert binary.label_kind == "binary"
    assert list(binary.frame["label"].dropna().unique()) == [0, 1]

    multi = dataset(frame_of(long_rows(
        n=12, label=lambda pos: ["normal", "congestion", "packet_loss"][
            pos % 3])))
    assert multi.label_kind == "multiclass"
    assert {"congestion", "packet_loss"} <= set(
        multi.frame["label"].dropna().unique())


def test_binary_truth_needs_an_explicit_normal_class():
    ref = lab_dataset.SeriesRef("R1", "ping.avg_rtt_ms", "eth0")

    binary = dataset(frame_of(long_rows(
        n=12, label=lambda pos: 1 if pos >= 10 else 0)))
    truth = lab_dataset.binary_truth(binary, ref)
    assert truth["normal"] == 10 and len(truth["anomaly"]) == 2
    assert truth["basis"] == "binary label column"

    ambiguous = dataset(frame_of(long_rows(
        n=12, label=lambda pos: ["congestion", "packet_loss"][pos % 2])))
    assert lab_dataset.binary_truth(ambiguous, ref) is None


def test_time_windowed_truth_only_counts_that_window():
    binary = dataset(frame_of(long_rows(
        n=24, label=lambda pos: 1 if pos >= 20 else 0)))
    ref = binary.series_refs()[0]
    truth = lab_dataset.binary_truth(binary, ref,
                                     from_ts=BASE + dt.timedelta(minutes=1),
                                     until_ts=BASE + dt.timedelta(minutes=5))
    assert truth["n_scored"] == 5                    # minutes 1..5 inclusive
    assert truth["normal"] == 5 and not truth["anomaly"]
    assert "windowed" in truth["basis"]


# ------------------------------------------------------- splits and leakage
def test_chronological_split_is_contiguous_and_ordered():
    values = pd.Series([float(v) for v in range(60)],
                       index=pd.date_range("2026-10-01", periods=60, freq="min"))
    train, test = lab_dataset.split_values(values, test_frac=0.25)
    assert len(train) + len(test) == len(values)
    assert list(train.index) == sorted(train.index)
    assert train.index.max() < test.index.min()
    lab_dataset.assert_chronological(train, test)
    assert train.iloc[-1] == 44.0 and test.iloc[0] == 45.0


def test_random_shuffle_split_is_rejected_by_the_leakage_gate():
    index = pd.date_range("2026-10-01", periods=60, freq="min")
    values = pd.Series([float(v) for v in range(60)], index=index)
    train = values.iloc[:40]
    test = values.iloc[35:]                          # overlaps the train tail
    with pytest.raises(LeakageError):
        lab_dataset.assert_chronological(train, test)


def test_split_refuses_too_little_history_instead_of_fitting_anything():
    values = pd.Series([1.0, 2.0, 3.0],
                       index=pd.date_range("2026-10-01", periods=3, freq="min"))
    with pytest.raises(lab_dataset.LabDataError):
        lab_dataset.split_values(values, min_train=10)


# --------------------------------------------------- train-only fitting
def test_preprocessor_learns_fill_and_scale_from_train_rows_only():
    index = pd.date_range("2026-10-01", periods=12, freq="min")
    frame = pd.DataFrame({
        "cpu": [10.0, 12.0, 11.0, 13.0, 12.0, 11.0,
                # test rows are far outside the train range on purpose
                900.0, 950.0, 910.0, 940.0, 905.0, 930.0],
    }, index=index)
    train_mask = frame.index < index[6]
    pre = lab_dataset.TrainPreprocessor.fit(frame, fit_rows=train_mask)

    assert pre.n_fit_rows == 6
    assert pre.fit_range == (str(index[0]), str(index[5]))
    assert abs(pre.medians["cpu"] - pd.Series([10, 12, 11, 13, 12, 11]).median()) \
        < 1e-9
    transformed = pre.transform(frame)
    # the test rows are still centred on the TRAIN median, so a leak-free
    # distance between train and test is preserved
    assert transformed["cpu"].iloc[0] == pytest.approx(
        (10.0 - pre.medians["cpu"]) / pre.scales["cpu"])
    assert transformed["cpu"].iloc[-1] > 0           # direction, not invention

    # A missing value is filled with the TRAIN median, never the global median
    hole = frame.copy()
    hole.loc[index[9], "cpu"] = float("nan")
    filled = pre.transform(hole)["cpu"].loc[index[9]]
    assert filled == pytest.approx(0.0)
    assert filled != pytest.approx(
        (930.0 - pre.medians["cpu"]) / pre.scales["cpu"])


def test_preprocessor_records_a_constant_column_without_scaling_it():
    frame = pd.DataFrame({"state": [1.0] * 8, "rtt": [1, 2, 3, 4, 5, 6, 7, 8]},
                         index=pd.date_range("2026-10-01", periods=8,
                                             freq="min"))
    pre = lab_dataset.TrainPreprocessor.fit(frame)
    assert pre.scales["state"] is None
    assert pre.scales["rtt"]
    out = pre.transform(frame)
    assert list(out["state"]) == [1.0] * 8           # untouched, not zeroed
    assert pre.config()["columns"]["state"]["scale"] is None
    assert pre.config()["imputation"] == "train-median"


def test_preprocessor_refuses_an_empty_learning_set():
    with pytest.raises(lab_dataset.LabDataError):
        lab_dataset.TrainPreprocessor.fit(pd.DataFrame(index=[1, 2]))


# ------------------------------------------------------------ task gates
def test_classification_gate_blocks_an_unlabelled_source_in_words():
    from tests.ai_lab_fixtures import unlabelled_dataset

    ds = unlabelled_dataset()
    allowed, why = lab_dataset.task_gate(ds, lab_dataset.TASK_CLASSIFICATION)
    assert allowed is False
    assert "no ground-truth labels" in why and "will not be faked" in why
    ok_anomaly, _ = lab_dataset.task_gate(ds, lab_dataset.TASK_ANOMALY)
    assert ok_anomaly is True


def test_short_series_are_reported_as_insufficient_with_the_threshold():
    ds = dataset(frame_of(long_rows(n=6)))
    allowed, why = lab_dataset.task_gate(ds, lab_dataset.TASK_FORECAST)
    assert allowed is False
    assert "insufficient data" in why and "forecasting" in why
    assert ds.inventory[0].reasons                  # stated, not swallowed
    with pytest.raises(lab_dataset.LabDataError):
        lab_dataset.task_gate(ds, "quantum_analysis")


def test_unknown_series_reference_is_reported_not_assumed():
    ds = dataset(frame_of(long_rows(n=12)))
    ok, why = lab_dataset.task_gate(
        ds, lab_dataset.TASK_ANOMALY,
        ref=lab_dataset.SeriesRef("R9", "nope", "eth9"))
    assert ok is False and "not part of this dataset" in why


# ----------------------------------------------------------- multivariate
def test_feature_frame_keeps_only_timestamps_every_metric_reports():
    rows = long_rows(n=20, metric="ping.avg_rtt_ms")
    rows += long_rows(n=20, metric="cpu.usage_pct",
                      start=BASE + dt.timedelta(minutes=5))[:15]
    ds = dataset(frame_of(rows))
    features, stamps, used = ds.feature_frame(
        "R1", ("ping.avg_rtt_ms", "cpu.usage_pct"), interface="eth0")
    assert used == ["ping.avg_rtt_ms", "cpu.usage_pct"]
    assert len(features) == len(stamps) == 15        # overlap only
    assert not features.isna().any().any()


def test_feature_frame_reports_which_metrics_it_could_not_use():
    ds = dataset(frame_of(long_rows(n=20)))
    features, stamps, used = ds.feature_frame(
        "R1", ("ping.avg_rtt_ms", "if.not_in_this_source"),
        interface="eth0")
    assert used == ["ping.avg_rtt_ms"]
    assert len(features) == len(stamps) == 20
    # a metric name that does not exist is never silently invented
    assert "if.not_in_this_source" not in features.columns
    nothing, _, used2 = ds.feature_frame("R9", ("ping.avg_rtt_ms",),
                                        interface="eth0")
    assert used2 == [] and nothing.empty


# ------------------------------------------------------------- fingerprint
def test_fingerprint_is_stable_and_content_sensitive():
    rows = long_rows(n=24)
    a = dataset(frame_of(rows), source_id="same")
    b = dataset(frame_of(list(rows)), source_id="same")
    assert a.fingerprint == b.fingerprint
    mutated = [dict(r) for r in rows]
    mutated[7]["value"] = float(mutated[7]["value"]) + 0.5
    c = dataset(frame_of(mutated), source_id="same")
    assert c.fingerprint != a.fingerprint
    assert len(a.fingerprint) == 32


# ---------------------------------------------------------------- quality
def test_quality_report_exposes_duplicates_and_staleness_per_series():
    ds = dataset(frame_of(long_rows(n=48)))         # last point +47 min
    quality = ds.quality(ds.series_refs()[0])
    assert quality.n_rows == 48 and quality.is_stale is False
    assert quality.stale_seconds == pytest.approx(13 * 60.0)
    stale = lab_dataset.canonicalize(
        frame_of(long_rows(n=48)), source_type=lab_dataset.SOURCE_HISTORICAL,
        source_id="old", now=BASE + dt.timedelta(days=2))
    assert stale.quality(stale.series_refs()[0]).is_stale is True
    assert stale.profile["n_stale_series"] == 1
