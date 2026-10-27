"""Reproducible telemetry simulator for the AI Lab (Phase 5).

Generates multi-device network measurements together with **ground-truth**
event labels, because supervised evaluation (precision / recall / F1 / PR-AUC,
fault classification) is only defensible when the true answer is known. In a
real lab nobody knows the ground truth, which is exactly why every supervised
number produced from this source is tagged
``labels=synthetic-ground-truth`` in the run metadata.

Honesty contract:

* the output is clearly a simulation: ``source_type == "simulated"`` and the
  provenance records the seed, the injected events and their intervals;
* volume is bounded by configuration - an oversized request is refused instead
  of silently truncated;
* the same seed and the same parameters produce a byte-identical dataset (and
  therefore the same fingerprint), so an experiment can be re-run and audited;
* a down device yields NULL latency, not 0 ms - missing data stays missing,
  mirroring ``services/demo_data.py`` and the live collector semantics;
* ``cpu.usage_pct`` / ``memory.usage_pct`` exist **only** in simulated data:
  the live collector samples ping/interface counters/operstate/routes and never
  CPU or memory, so a run that used them must not be presented as lab-real.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import math
import random

import pandas as pd

from analytics import lab_dataset
from analytics.lab_dataset import (SOURCE_SIMULATED, LabDataError, SeriesRef,
                                   LabDataset, canonicalize)
from config import get_settings

#: Event name -> (affected metric, transform id, class label). The class label
#: doubles as the multiclass ground truth used by the fault-classification task.
EVENT_PROFILES: dict[str, dict] = {
    "congestion": {
        "metric": "if.rx_bps", "kind": "multiplicative_ramp", "factor": 2.6,
        "class_label": "congestion",
        "meaning": "sustained traffic surge on the receive path",
    },
    "latency_spike": {
        "metric": "ping.avg_rtt_ms", "kind": "step_up", "factor": 6.0,
        "class_label": "latency_spike",
        "meaning": "round-trip time jumps well above the baseline",
    },
    "packet_loss": {
        "metric": "ping.loss_pct", "kind": "step_pct", "factor": 38.0,
        "class_label": "packet_loss",
        "meaning": "elevated loss percentage during the window",
    },
    "cpu_overload": {
        "metric": "cpu.usage_pct", "kind": "logistic_ramp", "factor": 1.9,
        "class_label": "cpu_overload",
        "meaning": "device CPU saturation ramp (simulated metric only)",
    },
    "interface_outage": {
        "metric": "if.operstate", "kind": "link_down", "factor": 0.0,
        "class_label": "interface_outage",
        "meaning": "interface goes down: traffic 0, latency unavailable",
    },
}

#: metric -> (unit, base level, relative noise, diurnal amplitude)
METRIC_PROFILES: dict[str, dict] = {
    "if.rx_bps": {"unit": "bps", "base": 4.0e7, "noise": 0.12,
                  "season": 0.35, "floor": 0.0},
    "if.tx_bps": {"unit": "bps", "base": 2.6e7, "noise": 0.12,
                  "season": 0.30, "floor": 0.0},
    "ping.avg_rtt_ms": {"unit": "ms", "base": 6.0, "noise": 0.18,
                        "season": 0.10, "floor": 0.05},
    "ping.loss_pct": {"unit": "%", "base": 0.4, "noise": 0.9,
                      "season": 0.0, "floor": 0.0, "ceiling": 100.0},
    "cpu.usage_pct": {"unit": "%", "base": 26.0, "noise": 0.22,
                      "season": 0.18, "floor": 1.0, "ceiling": 100.0},
    "memory.usage_pct": {"unit": "%", "base": 48.0, "noise": 0.05,
                         "seed_drift": 0.4, "season": 0.04, "floor": 1.0,
                         "ceiling": 100.0},
    "if.operstate": {"unit": "link", "base": 1.0, "noise": 0.0,
                     "season": 0.0, "floor": 0.0, "ceiling": 1.0},
}

DEFAULT_METRICS = ("if.rx_bps", "if.tx_bps", "ping.avg_rtt_ms",
                   "ping.loss_pct", "cpu.usage_pct", "if.operstate")


class SimulationBoundError(LabDataError):
    """Raised when a simulation request exceeds the configured bounds."""


@dataclasses.dataclass
class SimulationConfig:
    """Bounded, serialisable description of one simulated dataset."""

    devices: tuple[str, ...] = ("R1", "R2", "R3")
    duration_hours: float = 6.0
    interval_seconds: int = 60
    seed: int = 20261027
    metrics: tuple[str, ...] = DEFAULT_METRICS
    events: dict[str, int] = dataclasses.field(
        default_factory=lambda: {"latency_spike": 2, "packet_loss": 2,
                                 "congestion": 1, "cpu_overload": 1,
                                 "interface_outage": 1})
    interface: str = "eth0"
    start: dt.datetime | None = None

    def __post_init__(self) -> None:
        # The canonical frame stores naive UTC, so an aware start is converted
        # once here. The generated samples, the provenance event windows and
        # the config hash then all speak the same convention as every
        # timestamp a task compares against (no tz-aware/naive mismatch).
        if self.start is not None and self.start.tzinfo is not None:
            self.start = self.start.astimezone(dt.timezone.utc).replace(
                tzinfo=None)

    @property
    def points(self) -> int:
        if self.interval_seconds <= 0:
            raise SimulationBoundError("interval_seconds must be positive")
        return int(max(1, math.floor(
            self.duration_hours * 3600.0 / self.interval_seconds)))

    @property
    def expected_rows(self) -> int:
        return self.points * len(self.devices) * len(self.metrics)

    def config(self) -> dict:
        return {
            "devices": list(self.devices), "duration_hours":
                self.duration_hours, "interval_seconds": self.interval_seconds,
            "seed": self.seed, "metrics": list(self.metrics),
            "events": dict(self.events), "interface": self.interface,
            "start": self.start.isoformat() if self.start else None,
            "points": self.points, "expected_rows": self.expected_rows,
        }

    def validate(self) -> None:
        """Fail loudly on anything outside the configured input bounds."""
        settings = get_settings()
        if self.duration_hours <= 0:
            raise SimulationBoundError("duration must be > 0 hours")
        if self.interval_seconds < 1:
            raise SimulationBoundError("sampling interval must be >= 1 s")
        if not self.devices:
            raise SimulationBoundError("at least one device is required")
        if len(self.devices) > settings.ai_lab_sim_max_devices:
            raise SimulationBoundError(
                f"{len(self.devices)} devices exceeds the configured bound of "
                f"{settings.ai_lab_sim_max_devices} "
                "(NETPILOT_AI_LAB_SIM_MAX_DEVICES).")
        if self.points > settings.ai_lab_sim_max_points:
            raise SimulationBoundError(
                f"{self.points} samples per device exceeds the configured "
                f"bound of {settings.ai_lab_sim_max_points} "
                "(NETPILOT_AI_LAB_SIM_MAX_POINTS). Reduce the duration or "
                "widen the sampling interval.")
        if self.expected_rows > settings.analytics_max_points:
            raise SimulationBoundError(
                f"the request would generate {self.expected_rows} rows, above "
                f"the analytics bound of {settings.analytics_max_points} "
                "(NETPILOT_ANALYTICS_MAX_POINTS).")
        unknown = [m for m in self.metrics if m not in METRIC_PROFILES]
        if unknown:
            raise SimulationBoundError("unknown simulated metric(s): "
                                       + ", ".join(unknown))
        bad = [name for name in self.events if name not in EVENT_PROFILES]
        if bad:
            raise SimulationBoundError("unknown event type(s): "
                                        + ", ".join(bad))
        negative = [name for name, count in self.events.items()
                    if count < 0]
        if negative:
            raise SimulationBoundError("event counts cannot be negative: "
                                       + ", ".join(negative))


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def simulate(cfg: SimulationConfig | None = None, **overrides) -> LabDataset:
    """Build a labelled, reproducible :class:`LabDataset` from the simulator."""
    if cfg is None:
        cfg = SimulationConfig(**overrides)
    elif overrides:
        raise LabDataError("pass either a SimulationConfig or keyword "
                           "overrides, not both")
    cfg.validate()

    settings = get_settings()
    start = cfg.start or _default_start(cfg)
    timestamps = pd.DatetimeIndex(
        [start + dt.timedelta(seconds=cfg.interval_seconds * i)
         for i in range(cfg.points)])

    schedule, events = _plan_events(cfg, timestamps)
    # 1) baseline + primary event effect per (device, metric)
    series_map: dict[tuple[str, str], list] = {}
    label_map: dict[tuple[str, str], list] = {}
    for device in cfg.devices:
        phases = _device_phases(cfg.seed, device)
        for metric in cfg.metrics:
            profile = METRIC_PROFILES[metric]
            rng = random.Random(f"{cfg.seed}|{device}|{metric}")
            series = _baseline(profile, rng, cfg.points, phases,
                               hourly=cfg.interval_seconds / 3600.0)
            labels = ["normal"] * cfg.points
            for event in schedule.get((device, metric), []):
                series, labels = _apply_event(
                    event, series, labels, profile, rng)
            series_map[(device, metric)] = series
            label_map[(device, metric)] = labels
    # 2) documented side effects of an interface outage on the same device.
    #    Traffic stops, latency becomes genuinely unavailable (NULL, never 0)
    #    and loss goes to 100% - the same semantics the live collector uses.
    for event in events:
        if event.type != "interface_outage":
            continue
        _apply_outage_side_effects(event, series_map, label_map)

    rows: list[dict] = []
    for device in cfg.devices:
        for metric in cfg.metrics:
            profile = METRIC_PROFILES[metric]
            series = series_map[(device, metric)]
            labels = label_map[(device, metric)]
            for pos, ts in enumerate(timestamps):
                rows.append({
                    "collected_at": ts.to_pydatetime(),
                    "device": device, "interface": cfg.interface,
                    "metric": metric, "value": series[pos],
                    "unit": profile["unit"], "label": labels[pos],
                })

    frame = pd.DataFrame(rows)
    provenance = {
        "generator": "analytics.lab_simulator",
        "config": cfg.config(),
        "seed": cfg.seed,
        "labels": "synthetic ground truth (injected event intervals)",
        "events": [
            {"event_id": e.event_id, "type": e.type, "class_label":
             e.class_label, "device": e.device, "metric": e.metric,
             "interface": cfg.interface, "start": e.start.isoformat(),
             "end": e.end.isoformat(), "n_points": e.n_points,
             "meaning": EVENT_PROFILES[e.type]["meaning"]}
            for e in events],
        "simulated_only_metrics": sorted(
            m for m in cfg.metrics if m in ("cpu.usage_pct",
                                            "memory.usage_pct")),
        "boundaries": {
            "not_lab_real": True,
            "cpu_memory_collected_live": False,
            "max_points": settings.ai_lab_sim_max_points,
            "max_devices": settings.ai_lab_sim_max_devices,
        },
    }
    source_id = "sim-" + hashlib.sha256(
        repr(cfg.config()).encode()).hexdigest()[:12]
    dataset = canonicalize(
        frame, source_type=SOURCE_SIMULATED, source_id=source_id,
        provenance=provenance, now=dt.datetime.now(
            dt.timezone.utc).replace(tzinfo=None),
        max_rows=settings.analytics_max_points)
    return dataset


def _default_start(cfg: SimulationConfig) -> dt.datetime:
    """Anchor the window so the newest sample is 'now' (naive UTC)."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, second=0,
                                                   microsecond=0)
    span = cfg.interval_seconds * (cfg.points - 1)
    return now - dt.timedelta(seconds=span)


def _device_phases(seed: int, device: str) -> dict[str, float]:
    rng = random.Random(f"{seed}|phase|{device}")
    return {"hour": rng.uniform(0.0, math.pi), "slow": rng.uniform(0.0, math.pi)}


def _baseline(profile: dict, rng: random.Random, n: int, phases: dict,
              *, hourly: float) -> list[float | None]:
    """Smooth diurnal shape + bounded noise, no events yet."""
    values: list[float | None] = []
    level = profile["base"]
    drift = profile.get("seed_drift", 0.0)
    walk = 0.0
    for i in range(n):
        hour_angle = 2 * math.pi * (i * hourly) + phases["hour"]
        shape = 1.0 + profile["season"] * math.sin(hour_angle)
        if drift:
            walk += rng.uniform(-drift, drift) * level * 0.01
        noisy = level * shape + walk + rng.gauss(0.0,
                                                 profile["noise"] * level * 0.5)
        floor = profile.get("floor")
        ceiling = profile.get("ceiling")
        if floor is not None:
            noisy = max(floor, noisy)
        if ceiling is not None:
            noisy = min(ceiling, noisy)
        values.append(round(float(noisy), 4))
    return values


@dataclasses.dataclass
class EventWindow:
    event_id: str
    type: str
    class_label: str
    device: str
    metric: str
    start: dt.datetime
    end: dt.datetime
    n_points: int
    start_pos: int
    end_pos: int
    peak_index: int


def _plan_events(cfg: SimulationConfig, timestamps: pd.DatetimeIndex
                 ) -> tuple[dict[tuple[str, str], list[EventWindow]],
                            list[EventWindow]]:
    """Spread the requested events deterministically over the window.

    Events never overlap, so a labelled point belongs to exactly one fault
    class - an overlapping label would make a confusion matrix meaningless.
    """
    rng = random.Random(f"{cfg.seed}|schedule")
    n = cfg.points
    used: list[tuple[int, int]] = []
    schedule: dict[tuple[str, str], list[EventWindow]] = {}
    all_events: list[EventWindow] = []
    for event_type in sorted(cfg.events):
        profile = EVENT_PROFILES[event_type]
        if profile["metric"] not in cfg.metrics:
            continue
        for device in cfg.devices:
            for _ in range(cfg.events[event_type]):
                length = max(3, min(n // 8, 12))
                start = None
                for _attempt in range(60):
                    candidate = rng.randint(1, max(1, n - length - 1))
                    span = (candidate, candidate + length)
                    if any(span[0] < b and a < span[1] for a, b in used):
                        continue
                    start = candidate
                    used.append(span)
                    break
                if start is None:
                    continue          # window fully occupied: honest no-op
                end_pos = min(n - 1, start + length - 1)
                window = EventWindow(
                    event_id=f"{event_type}-{device}-{start}",
                    type=event_type, class_label=profile["class_label"],
                    device=device, metric=profile["metric"],
                    start=timestamps[start].to_pydatetime(),
                    end=timestamps[end_pos].to_pydatetime(),
                    n_points=end_pos - start + 1,
                    start_pos=start, end_pos=end_pos,
                    peak_index=start + length // 2)
                schedule.setdefault((device, profile["metric"]), []).append(
                    window)
                all_events.append(window)
    return schedule, all_events


def _apply_event(window: EventWindow, series: list[float | None],
                 labels: list[str], profile: dict,
                 rng: random.Random) -> tuple[list[float | None], list[str]]:
    """Distort the affected series inside the labelled interval."""
    kind = EVENT_PROFILES[window.type]["kind"]
    factor = EVENT_PROFILES[window.type]["factor"]
    start, stop = window.start_pos, window.end_pos + 1
    width = max(1, stop - start)
    for offset, pos in enumerate(range(start, min(stop, len(series)))):
        progress = (offset + 1) / width
        base = series[pos]
        if base is None:
            base = profile["base"]
        if kind == "step_up":
            value = base * factor + rng.uniform(0.0, base * 0.4)
        elif kind == "multiplicative_ramp":
            value = base * (1.0 + (factor - 1.0) * progress)
        elif kind == "logistic_ramp":
            value = min(profile.get("ceiling", 100.0),
                        base * (1.0 + factor * progress))
        elif kind == "step_pct":
            value = min(profile.get("ceiling", 100.0), factor)
        elif kind == "link_down":
            value = 0.0
        else:  # pragma: no cover - guarded by EVENT_PROFILES
            value = base
        floor = profile.get("floor")
        if floor is not None:
            value = max(floor, value)
        series[pos] = round(float(value), 4)
        labels[pos] = window.class_label
    return series, labels


def _apply_outage_side_effects(window: EventWindow,
                               series_map: dict[tuple[str, str], list],
                               label_map: dict[tuple[str, str], list]) -> None:
    """Propagate an interface outage to the other series of the same device."""
    effects = {"if.rx_bps": 0.0, "if.tx_bps": 0.0, "ping.loss_pct": 100.0}
    for pos in range(window.start_pos, window.end_pos + 1):
        for metric, value in effects.items():
            key = (window.device, metric)
            if key not in series_map or pos >= len(series_map[key]):
                continue
            series_map[key][pos] = value
            label_map[key][pos] = window.class_label
        key = (window.device, "ping.avg_rtt_ms")
        if key in series_map and pos < len(series_map[key]):
            # Unreachable peer: the reading does not exist. NULL, not 0.
            series_map[key][pos] = None
            label_map[key][pos] = window.class_label


def ground_truth(dataset: LabDataset) -> dict:
    """Extract the recorded event truth for supervised scoring.

    Delegates the anomaly/normal derivation to
    ``analytics.lab_dataset.binary_truth`` (one rule for every source type) and
    adds the per-point class map used by the fault-classification task, so the
    truth always matches what was actually generated and stored.
    """
    binary = {item.ref.key: lab_dataset.binary_truth(dataset, item.ref)
              for item in dataset.inventory}
    classes: dict[tuple, str] = {}
    for item in dataset.inventory:
        rows = dataset.group(item.ref)
        rows = rows[rows["value"].notna() & rows["label"].notna()]
        for _, row in rows.iterrows():
            classes[(item.ref.device, row["collected_at"])] = row["label"]
    return {"binary": binary, "classes": classes,
            "events": list(dataset.provenance.get("events", []))}


def series_class_map(dataset: LabDataset, ref: SeriesRef) -> pd.Series | None:
    """Label series for one metric, or None when the source is unlabelled."""
    labels = dataset.series_labels(ref)
    if labels is None:
        return None
    return labels.map(lambda v: v if isinstance(v, str) else
                      ("fault" if v == 1 else "normal"))
