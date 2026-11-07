"""Seed clearly-labelled simulated lab data for full-feature Live Mode demos.

The operator has no Containerlab lab, and real probes only reach three
targets.  This tool runs the **existing AI Lab simulator** and writes its
output through the ingest role (``netpilot_collector``) into the same tables
the application reads:

* ``devices`` / ``interfaces`` / ``links`` - the demo topology from
  ``lab/topology.clab.yml``, with every structural name prefixed ``sim:`` so
  the simulated provenance can never be confused with live rows;
* ``metric_samples`` - the simulator's long-format measurements tagged
  ``data_source='simulated'``;
* ``incidents`` - the simulator's injected ground-truth event windows;
* ``collection_runs`` - one audit row tagged ``simulated``.

Nothing is ever labelled ``live``.  The seed is idempotent: if simulated
measurements already exist, the command reports them and writes nothing new.
There is no DELETE privilege, so re-seeding a different window requires the
schema administrator to clear the rows first.

Usage
-----
    python tools/seed_simulated.py
    python tools/seed_simulated.py --duration-hours 6 --interval 60
    python tools/seed_simulated.py --no-wcm          # environment DSN only

Exit codes: 0 ok/no-op, 2 configuration error, 3 database unavailable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from analytics import lab_simulator
from config import get_settings
from database import connection, repository, roles, source_scope
from database.models import Incident
from network.device_inventory import load_inventory
from tools import live_collector

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_DB = 3

SIMULATED_INTERFACE = "eth0"


def _to_datetime(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    return value.to_pydatetime()


def _seed_structural(session, inventory) -> dict:
    """Upsert the demo topology under the reserved ``sim:`` namespace."""
    counts = {"devices": 0, "interfaces": 0, "links": 0}
    for node in inventory.nodes:
        name = source_scope.tag_simulated(node.name)
        device = repository.upsert_device(
            session, name=name, type_=node.type or "unknown",
            lab_ip=node.lab_ip, status="up")
        counts["devices"] += 1
        for iface in node.interfaces:
            repository.upsert_interface(
                session, device, iface.name, ip_cidr=iface.ip_cidr,
                status="up")
            counts["interfaces"] += 1
        # The simulator measures every device on this logical interface.
        repository.upsert_interface(
            session, device, SIMULATED_INTERFACE, status="up")
        counts["interfaces"] += 1
    for link in inventory.links:
        repository.upsert_link(
            session, source_scope.tag_simulated(link.node_a), link.if_a,
            source_scope.tag_simulated(link.node_b), link.if_b,
            subnet=link.subnet, status="up")
        counts["links"] += 1
    return counts


def _seed_metrics(session, dataset) -> int:
    frame = dataset.frame
    rows: list[dict] = []
    for record in frame.itertuples(index=False):
        rows.append({
            "device_name": source_scope.tag_simulated(record.device),
            "interface_name": record.interface or None,
            "metric": record.metric,
            "value": None if record.value is None else float(record.value),
            "unit": record.unit,
            "data_source": source_scope.SIMULATED,
            "collected_at": _to_datetime(record.collected_at),
        })
    return repository.add_metric_samples_bulk(session, rows)


def _seed_incidents(session, dataset) -> int:
    """Persist the simulator's injected event windows as resolved incidents."""
    existing = session.scalars(
        select(Incident.id).where(
            Incident.device_name.like(source_scope.SIMULATED_PREFIX + "%")
        ).limit(1)
    ).first()
    if existing is not None:
        return 0
    events = (dataset.provenance or {}).get("events") or []
    for event in events:
        session.add(Incident(
            opened_at=dt.datetime.fromisoformat(event["start"]),
            resolved_at=dt.datetime.fromisoformat(event["end"]),
            device_name=source_scope.tag_simulated(event["device"]),
            kind=f"simulated_{event['type']}",
            detail=(f"AI Lab simulator ground truth: {event['meaning']} on "
                    f"{event['metric']} ({event['n_points']} points). "
                    "NOT real telemetry."),
        ))
    return len(events)


def seed_simulated(
    session,
    *,
    dataset=None,
    inventory=None,
    duration_hours: float = 6.0,
    interval_seconds: int = 60,
    seed: int = 20261027,
) -> dict:
    """Write one idempotent batch of simulated rows. Returns a count summary."""
    dataset = dataset or lab_simulator.simulate(
        duration_hours=duration_hours, interval_seconds=interval_seconds,
        seed=seed, interface=SIMULATED_INTERFACE)
    inventory = inventory or load_inventory()
    if not inventory.nodes:
        raise ValueError("the demo topology could not be read: "
                         + (inventory.source_error or "no nodes"))

    structural = _seed_structural(session, inventory)

    existing = any(
        source == source_scope.SIMULATED
        for (_device, _metric, _unit, source)
        in repository.get_available_metrics(session)
    )
    if existing:
        return {
            "skipped": True,
            **structural,
            "metrics": 0,
            "incidents": 0,
            "devices_total": len(inventory.nodes),
        }

    metrics = _seed_metrics(session, dataset)
    incidents = _seed_incidents(session, dataset)
    run = repository.start_collection_run(
        session, source_scope.SIMULATED)
    repository.finish_collection_run(
        session, run, "ok", devices_total=len(inventory.nodes),
        devices_up=len(inventory.nodes),
        error_message=None)
    return {
        "skipped": False,
        **structural,
        "metrics": metrics,
        "incidents": incidents,
        "devices_total": len(inventory.nodes),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed the AI Lab simulator into the live database with "
                    "data_source='simulated' (never labelled live).")
    parser.add_argument("--duration-hours", type=float, default=6.0)
    parser.add_argument("--interval", type=int, default=60,
                        help="sampling interval in seconds (default: 60)")
    parser.add_argument("--seed", type=int, default=20261027)
    parser.add_argument("--no-wcm", action="store_true",
                        help="do not read the collector DSN from Windows "
                             "Credential Manager")
    parser.add_argument("--wcm-target",
                        default=live_collector.WCM_TARGET_COLLECTOR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    credential = live_collector._load_collector_dsn(
        args.wcm_target, not args.no_wcm)
    if credential is None:
        print(
            "ERROR: no collector credential found. Store the "
            f"netpilot_collector DSN in Windows Credential Manager target "
            f"{args.wcm_target!r}, or set NETPILOT_DB_URL_COLLECTOR.",
            file=sys.stderr)
        return EXIT_CONFIG
    errors = live_collector.check_configuration(settings)
    if errors:
        for message in errors:
            print(f"ERROR: {message}", file=sys.stderr)
        return EXIT_CONFIG

    connection.set_database_role(roles.ROLE_COLLECTOR)
    try:
        connection.require_schema()
    except Exception as exc:  # noqa: BLE001 - message is credential-safe
        print(f"ERROR: collector database is not ready: "
              f"{type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return EXIT_DB

    try:
        with connection.session_scope() as session:
            summary = seed_simulated(
                session,
                duration_hours=args.duration_hours,
                interval_seconds=args.interval,
                seed=args.seed)
    except Exception as exc:  # noqa: BLE001 - never echo a DSN
        print(f"ERROR: seeding failed: {type(exc).__name__}: "
              f"{str(exc)[:300]}", file=sys.stderr)
        return EXIT_DB

    print("Simulated seed summary")
    print(f"  devices: {summary['devices']} (prefix "
          f"{source_scope.SIMULATED_PREFIX!r})")
    print(f"  interfaces: {summary['interfaces']}")
    print(f"  links: {summary['links']}")
    print(f"  metric samples: {summary['metrics']}")
    print(f"  incidents: {summary['incidents']}")
    if summary["skipped"]:
        print("  status: already seeded - no new simulated samples written")
    else:
        print("  status: seeded (data_source='simulated')")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
