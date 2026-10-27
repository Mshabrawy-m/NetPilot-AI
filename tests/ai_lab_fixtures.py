"""Deterministic, offline fixtures shared by the AI Lab test modules.

Everything here is hand-built and clock-independent: the AI Lab acceptance
tests must not need internet access, a live device, Prometheus or a wall-clock
that changes between runs.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from analytics import lab_dataset

#: Fixed anchor for every fixture. Tests pass it as ``now`` so staleness is
#: decided against the fixture window, not against the machine clock.
BASE = dt.datetime(2026, 10, 1, 0, 0, 0)


def long_rows(*, n: int = 48, step_seconds: int = 60, device: str = "R1",
              metric: str = "ping.avg_rtt_ms", interface: str | None = "eth0",
              unit: str | None = "ms", label=None, value=None,
              start: dt.datetime | None = None) -> list[dict]:
    """One canonical long-format series.

    ``value`` and ``label`` may be callables of the point position, which keeps
    malformed-row fixtures readable (return None to inject a missing value).
    """
    origin = start or BASE
    rows = []
    for pos in range(n):
        rows.append({
            "collected_at": origin + dt.timedelta(seconds=step_seconds * pos),
            "device": device,
            "interface": interface,
            "metric": metric,
            "value": (10.0 + (pos % 7)) if value is None else value(pos),
            "unit": unit,
            "label": label(pos) if callable(label) else label,
        })
    return rows


def frame_of(*series: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([row for part in series for row in part])


def dataset(frame: pd.DataFrame, *,
            source_type: str = lab_dataset.SOURCE_HISTORICAL,
            source_id: str = "fixture", provenance: dict | None = None,
            **kwargs) -> lab_dataset.LabDataset:
    """Canonicalise a fixture frame with the clock pinned to ``BASE``."""
    return lab_dataset.canonicalize(
        frame, source_type=source_type, source_id=source_id,
        provenance=provenance or {"note": "test fixture"},
        now=BASE + dt.timedelta(hours=1), **kwargs)


def labelled_dataset(**overrides) -> lab_dataset.LabDataset:
    """Historical fixture carrying a normal/fault binary label column."""
    kwargs = {"n": 48}
    kwargs.update(overrides)
    label = kwargs.pop("label", lambda pos: 1 if pos >= 40 else 0)
    return dataset(frame_of(long_rows(label=label, **kwargs)))


def unlabelled_dataset(**overrides) -> lab_dataset.LabDataset:
    """Same shape, no label column at all - the unsupervised case."""
    kwargs = {"n": 48}
    kwargs.update(overrides)
    kwargs.pop("label", None)
    return dataset(frame_of(long_rows(**kwargs)))


def two_device_dataset() -> lab_dataset.LabDataset:
    """Two devices, two metrics, aligned timestamps (multivariate input)."""
    rows = []
    for device in ("R1", "R2"):
        for metric, unit in (("ping.avg_rtt_ms", "ms"),
                             ("cpu.usage_pct", "%")):
            rows.extend(long_rows(device=device, metric=metric, unit=unit,
                                  value=lambda pos, m=metric: (
                                      10.0 + (pos % 7) if m.endswith("rtt_ms")
                                      else 30.0 + (pos % 5) * 2.0)))
    return dataset(frame_of(rows))
