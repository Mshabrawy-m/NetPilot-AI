"""AI Lab reproducible simulator: determinism, ground truth, input bounds.

Covers acceptance criteria 8.1 (same seed + configuration reproduces the same
output) and 8.2 (valid timestamps, metrics, units and event labels).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from analytics import lab_dataset, lab_simulator
from analytics.lab_simulator import (DEFAULT_METRICS, EVENT_PROFILES,
                                     METRIC_PROFILES, SimulationBoundError,
                                     SimulationConfig)

#: Fixed window: determinism must not depend on the wall clock.
START = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)


def config(**overrides) -> SimulationConfig:
    base = {"devices": ("R1", "R2"), "duration_hours": 2.0,
            "interval_seconds": 120, "seed": 4242, "start": START}
    base.update(overrides)
    return SimulationConfig(**base)


# --------------------------------------------------------------- criterion 1
def test_same_seed_and_configuration_reproduce_the_same_dataset():
    a = lab_simulator.simulate(config())
    b = lab_simulator.simulate(config())
    assert a.fingerprint == b.fingerprint
    assert a.source_id == b.source_id
    pd.testing.assert_frame_equal(a.frame, b.frame)
    assert [e["event_id"] for e in a.provenance["events"]] == \
        [e["event_id"] for e in b.provenance["events"]]


def test_a_different_seed_changes_the_traffic_not_the_structure():
    a = lab_simulator.simulate(config(seed=1))
    b = lab_simulator.simulate(config(seed=2))
    assert a.fingerprint != b.fingerprint
    assert a.metrics() == b.metrics()
    assert a.profile["n_rows"] == b.profile["n_rows"]


def test_overrides_and_config_object_describe_the_same_run():
    ds = lab_simulator.simulate(devices=("R1",), duration_hours=1.0,
                               interval_seconds=300, seed=9, start=START)
    other = lab_simulator.simulate(config(devices=("R1",), duration_hours=1.0,
                                         interval_seconds=300, seed=9))
    cfg = SimulationConfig(devices=("R1",), duration_hours=1.0,
                           interval_seconds=300, seed=9, start=START)
    assert ds.source_id == other.source_id == "sim-" + _config_hash(cfg)


def _config_hash(cfg: SimulationConfig) -> str:
    import hashlib
    return hashlib.sha256(repr(cfg.config()).encode()).hexdigest()[:12]


# --------------------------------------------------------------- criterion 2
def test_generated_frame_carries_valid_timestamps_devices_and_intervals():
    cfg = config()
    ds = lab_simulator.simulate(cfg)
    stamps = sorted(set(ds.frame["collected_at"].tolist()))
    assert stamps == sorted(stamps)
    assert len(stamps) == cfg.points
    gaps = {(later - earlier).total_seconds()
            for earlier, later in zip(stamps, stamps[1:])}
    assert gaps == {float(cfg.interval_seconds)}
    assert ds.devices() == ["R1", "R2"]
    assert ds.metrics() == sorted(DEFAULT_METRICS)
    assert ds.profile["n_rows"] == cfg.expected_rows
    # canonical timestamps are naive UTC internally
    assert not any(getattr(ts, "tzinfo", None) for ts in stamps)


def test_every_metric_keeps_its_declared_unit_and_units_are_never_mixed():
    ds = lab_simulator.simulate(config())
    for item in ds.inventory:
        assert item.unit == METRIC_PROFILES[item.ref.metric]["unit"]
        assert len({u for u in ds.group(item.ref)["unit"].dropna()}) == 1


def test_cpu_and_memory_are_flagged_as_simulated_only_metrics():
    ds = lab_simulator.simulate(config(metrics=DEFAULT_METRICS +
                                        ("memory.usage_pct",)))
    assert ds.provenance["simulated_only_metrics"] == ["cpu.usage_pct",
                                                      "memory.usage_pct"]


# ---------------------------------------------------------------- ground truth
def test_injected_events_are_recorded_and_label_their_intervals():
    cfg = config(events={"packet_loss": 2, "latency_spike": 1})
    ds = lab_simulator.simulate(cfg)
    events = ds.provenance["events"]
    # at most the requested count per device; the planner never invents a
    # window when the remaining room is too small, so a shortfall is honest
    assert 0 < len(events) <= (2 + 1) * len(cfg.devices)
    for event in events:
        ref = lab_dataset.SeriesRef(event["device"], event["metric"],
                                    event["interface"])
        labels = ds.series_labels(ref)
        assert labels is not None
        window = labels[(labels.index >= pd.Timestamp(event["start"]))
                        & (labels.index <= pd.Timestamp(event["end"]))]
        assert set(window) == {event["class_label"]}
        assert event["n_points"] == len(window)
        assert event["meaning"] == EVENT_PROFILES[event["type"]]["meaning"]
    # outside the labelled intervals the same series is normal
    ref = lab_dataset.SeriesRef(events[0]["device"], events[0]["metric"],
                                "eth0")
    assert "normal" in set(ds.series_labels(ref))


def test_labels_make_normal_and_abnormal_periods_distinguishable():
    ds = lab_simulator.simulate(config(events={"congestion": 1}))
    assert ds.label_kind == "multiclass"
    event = ds.provenance["events"][0]
    ref = lab_dataset.SeriesRef(event["device"], event["metric"], "eth0")
    classes = lab_simulator.series_class_map(ds, ref)
    assert {"normal", "congestion"} <= set(classes)
    truth = lab_simulator.ground_truth(ds)
    assert truth["binary"][ref.key]["basis"].startswith(
        "multi-class labels, normal class")
    assert len(truth["binary"][ref.key]["anomaly"]) == event["n_points"]
    assert truth["events"] == ds.provenance["events"]


def test_values_really_move_inside_the_labelled_window():
    cfg = config(events={"latency_spike": 1})
    ds = lab_simulator.simulate(cfg)
    event = ds.provenance["events"][0]
    ref = lab_dataset.SeriesRef(event["device"], event["metric"], "eth0")
    series = ds.series_values(ref)
    inside = series[(series.index >= pd.Timestamp(event["start"]))
                    & (series.index <= pd.Timestamp(event["end"]))]
    outside = series[(series.index < pd.Timestamp(event["start"]))
                     | (series.index > pd.Timestamp(event["end"]))]
    assert inside.min() > outside.max()


def test_interface_outage_stops_traffic_and_makes_latency_missing_not_zero():
    cfg = config(metrics=("if.operstate", "if.rx_bps", "ping.avg_rtt_ms",
                          "ping.loss_pct"),
                 events={"interface_outage": 1})
    ds = lab_simulator.simulate(cfg)
    event = next(e for e in ds.provenance["events"]
                 if e["type"] == "interface_outage")
    rows = ds.frame[ds.frame["device"] == event["device"]]
    window = rows[(rows["collected_at"] >= pd.Timestamp(event["start"]))
                  & (rows["collected_at"] <= pd.Timestamp(event["end"]))]

    def values(metric):
        return window.loc[window["metric"] == metric, "value"]

    assert set(values("if.operstate")) == {0.0}
    assert set(values("if.rx_bps")) == {0.0}
    assert set(values("ping.loss_pct")) == {100.0}
    rtt = values("ping.avg_rtt_ms")
    assert rtt.isna().all()                      # unreachable peer: no reading
    assert (rtt.fillna(-1) != 0.0).all()         # never invented as zero
    assert set(window.loc[window["metric"] == "if.operstate", "label"]) == {
        "interface_outage"}


# ------------------------------------------------------------------- bounds
def test_device_count_bound_is_enforced_with_the_setting_named():
    from config import get_settings

    settings = get_settings()
    too_many = tuple(
        f"R{i}" for i in range(1, settings.ai_lab_sim_max_devices + 2))
    with pytest.raises(SimulationBoundError) as exc:
        lab_simulator.simulate(config(devices=too_many))
    assert "NETPILOT_AI_LAB_SIM_MAX_DEVICES" in str(exc.value)


def test_sample_count_bound_is_enforced():
    from config import get_settings

    settings = get_settings()
    hours = settings.ai_lab_sim_max_points * 60 / 3600.0 + 1
    with pytest.raises(SimulationBoundError) as exc:
        lab_simulator.simulate(config(duration_hours=hours,
                                      interval_seconds=60))
    assert "NETPILOT_AI_LAB_SIM_MAX_POINTS" in str(exc.value)


def test_total_row_bound_is_enforced_before_anything_is_generated():
    with pytest.raises(SimulationBoundError) as exc:
        lab_simulator.simulate(config(duration_hours=166.0,
                                      interval_seconds=60,
                                      metrics=DEFAULT_METRICS))
    assert "NETPILOT_ANALYTICS_MAX_POINTS" in str(exc.value)


@pytest.mark.parametrize("overrides", [
    {"duration_hours": 0},
    {"interval_seconds": 0},
    {"devices": ()},
    {"metrics": ("bgp.sessions",)},
    {"events": {"ddos": 1}},
    {"events": {"packet_loss": -1}},
])
def test_invalid_requests_are_refused_rather_than_clamped(overrides):
    with pytest.raises(SimulationBoundError):
        SimulationConfig(**{**{"devices": ("R1",), "duration_hours": 1.0,
                               "interval_seconds": 60, "seed": 1},
                            **overrides}).validate()


def test_config_and_dataset_never_claim_to_be_real_measurements():
    ds = lab_simulator.simulate(config(events={"packet_loss": 1}))
    assert ds.source_type == lab_dataset.SOURCE_SIMULATED
    assert ds.provenance["boundaries"]["not_lab_real"] is True
    assert ds.provenance["labels"].startswith("synthetic ground truth")
    assert "NOT real-network traffic" in lab_dataset.SOURCE_LABELS[
        ds.source_type]
    assert ds.provenance["generator"] == "analytics.lab_simulator"


def test_events_never_overlap_so_each_labelled_point_has_one_class():
    cfg = config(events={"packet_loss": 3, "latency_spike": 3,
                         "congestion": 2})
    ds = lab_simulator.simulate(cfg)
    spans: dict[tuple, list] = {}
    for event in ds.provenance["events"]:
        spans.setdefault((event["device"], event["metric"]), []).append(
            (event["start"], event["end"]))
    for parts in spans.values():
        parts.sort()
        assert all(parts[i][1] < parts[i + 1][0]
                   for i in range(len(parts) - 1)) or len(parts) == 1
    # one class per point: the class map has no ambiguity to hide
    for ref in ds.series_refs():
        labels = ds.series_labels(ref)
        if labels is not None:
            assert not labels.index.duplicated().any()
