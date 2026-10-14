"""Telemetry evidence: latest metric values + short series summaries.

Read-only and source-filtered. A missing metric yields a ``missing`` record
(value None) rather than an invented number, exactly like the Phase 1/2
collector contract.
"""
from __future__ import annotations

import datetime as dt

from ai.tools._common import is_stale, make_record
from config import get_settings
from database import repository

#: A latest value older than (interval x this factor) is reported stale.
_STALE_FACTOR = 4


def latest_metrics(session, *, device_name: str, data_source: str,
                   now: dt.datetime, max_rows: int, max_bytes: int) -> list[dict]:
    limit = get_settings().collect_interval_seconds * _STALE_FACTOR
    # Source-scoped latest: avoids the cross-source tie collapse documented in
    # the Phase 3 acceptance audit (F-2) without changing the shared
    # latest_metric_values() used by the Phase 1/2 pages.
    rows = list(repository.latest_metric_values_for_source(
        session, device_name, data_source))
    records: list[dict] = []
    for r in rows[:max_rows]:
        scope = r.interface_name or "device-level"
        records.append(make_record(
            kind="metric", tool="telemetry_latest", data_source=data_source,
            device_name=device_name, observed_at=r.collected_at, unit=r.unit,
            content=(f"{r.metric} [{scope}] = "
                     + ("NOT AVAILABLE" if r.value is None else f"{r.value}")),
            source_ref=[f"metric_sample:{r.id}"],
            stale=is_stale(r.collected_at, now, limit),
            missing=r.value is None, max_bytes=max_bytes))
    if not records:
        records.append(make_record(
            kind="metric", tool="telemetry_latest", data_source=data_source,
            device_name=device_name, observed_at=now,
            content=f"no stored metrics for {device_name} ({data_source})",
            missing=True, max_bytes=max_bytes))
    return records


def series_summary(session, *, device_name: str, metric: str, data_source: str,
                   now: dt.datetime, max_bytes: int,
                   window: int = 50) -> dict | None:
    """Min/max/avg/last over the most recent ``window`` non-null samples."""
    samples = list(repository.get_metric_series(
        session, device_name, metric, data_source=data_source))
    values = [s.value for s in samples if s.value is not None][-window:]
    if not values:
        return make_record(
            kind="metric_series", tool="telemetry_series",
            data_source=data_source, device_name=device_name, observed_at=now,
            content=f"{metric}: no non-null samples in source {data_source}",
            missing=True, max_bytes=max_bytes)
    last = samples[-1]
    unit = last.unit
    limit = get_settings().collect_interval_seconds * _STALE_FACTOR
    return make_record(
        kind="metric_series", tool="telemetry_series", data_source=data_source,
        device_name=device_name, unit=unit, observed_at=last.collected_at,
        content=(f"{metric}: n={len(values)} min={min(values):.3f} "
                 f"max={max(values):.3f} avg={sum(values)/len(values):.3f} "
                 f"last={values[-1]:.3f} {unit or ''}"),
        source_ref=[f"metric_sample:{last.id}"],
        stale=is_stale(last.collected_at, now, limit), max_bytes=max_bytes)
