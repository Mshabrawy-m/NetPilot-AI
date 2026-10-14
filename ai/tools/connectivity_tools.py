"""Connectivity evidence - reads only STORED test results.

This tool never triggers a new probe; the assistant reasons about the
allowlisted ping/traceroute history the collector/UI already persisted.
"""
from __future__ import annotations

import datetime as dt

from ai.tools._common import make_record
from database import repository


def recent_tests(session, *, data_source: str, now: dt.datetime,
                 max_rows: int, max_bytes: int,
                 destination: str | None = None) -> list[dict]:
    rows = list(repository.get_connectivity_history(
        session, limit=max_rows * 3, destination=destination))
    rows = [r for r in rows if (r.data_source or "live") == data_source]
    records: list[dict] = []
    for r in rows[:max_rows]:
        detail = (f"{r.test_type} {r.source}->{r.destination}: "
                  + ("OK" if r.success else "FAILED")
                  + (f" avg={r.avg_latency_ms}ms" if r.avg_latency_ms else "")
                  + (f" loss={r.packet_loss_pct}%"
                     if r.packet_loss_pct is not None else ""))
        records.append(make_record(
            kind="connectivity", tool="connectivity_history",
            data_source=data_source, device_name=r.destination,
            observed_at=r.tested_at, unit="ms", content=detail,
            source_ref=[f"connectivity_test:{r.id}"], max_bytes=max_bytes))
    if not records:
        records.append(make_record(
            kind="connectivity", tool="connectivity_history",
            data_source=data_source, observed_at=now,
            content=f"no stored connectivity tests for source {data_source}",
            missing=True, max_bytes=max_bytes))
    return records
