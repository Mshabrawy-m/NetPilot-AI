"""Topology / inventory evidence (read-only, reuses Phase 1 services)."""
from __future__ import annotations

import datetime as dt

from ai.tools._common import make_record
from database import repository
from services import topology_service


def device_status_records(session, *, data_source: str, now: dt.datetime,
                          max_rows: int, max_bytes: int,
                          device_names: set[str] | None = None) -> list[dict]:
    """One summary + a bounded per-device status snapshot."""
    devices = list(repository.get_devices(session))
    if device_names:
        devices = [d for d in devices if d.name in device_names]
    status = topology_service.compute_network_status(devices)
    records = [make_record(
        kind="topology", tool="topology_devices", data_source=data_source,
        content=status.summary(), observed_at=now, max_bytes=max_bytes,
        missing=not devices)]
    for d in devices[:max_rows]:
        records.append(make_record(
            kind="device", tool="topology_devices", data_source=data_source,
            device_name=d.name, observed_at=d.updated_at or now,
            content=(f"device {d.name} type={d.type} status={d.status} "
                     f"lab_ip={d.lab_ip or '-'} mgmt_ip={d.mgmt_ip or '-'}"),
            source_ref=[f"device:{d.name}"], max_bytes=max_bytes))
    return records


def link_records(session, *, data_source: str, now: dt.datetime,
                 max_rows: int, max_bytes: int) -> list[dict]:
    links = list(repository.get_links(session))[:max_rows]
    return [make_record(
        kind="link", tool="topology_links", data_source=data_source,
        observed_at=l.discovered_at or now,
        content=(f"link {l.device_a}:{l.if_a} <-> {l.device_b}:{l.if_b} "
                 f"subnet={l.subnet or '-'} status={l.status}"),
        source_ref=[f"link:{l.id}"], max_bytes=max_bytes) for l in links]
