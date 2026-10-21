"""Periodic network data collection from the running lab.

Real data sources used (in live mode):
  1. host -> node management ping      : device availability + RTT/loss.
     Only the containerlab-managed IP (Docker bridge, host-reachable) is
     pinged from the collector host. Lab-internal addresses (10.x) are NOT
     pinged from outside the lab, so no packets ever leave the lab boundary.
  2. docker exec <node> ping <peer loopback> : in-lab connectivity between
     nodes measured inside the lab (metric 'inlab_ping.*')
  3. docker exec <node> cat /sys/class/net/<if>/statistics/*
                                       : interface counters (rx/tx bytes,
                                         errors, drops) and operstate
  4. docker exec <node> vtysh -c "show ip route json"
                                       : FRR routing information (route
                                         count)

If a source is unavailable (no Docker, node stopped, tool missing inside
the container) the sample is stored with value=None and shown as
unavailable - values are NEVER invented.

For testability, the collector accepts injected probe functions; the
default implementations call the real tools above.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from config import get_settings, utc_now
from database import repository
from database.connection import session_scope
from database.models import db_utcnow
from network import containerlab_manager, connectivity
from network.device_inventory import LabInventory, load_inventory

logger = logging.getLogger("netpilot.collector")

#: Metric catalogue: name -> unit. Documented in the README deliverable.
METRIC_UNITS = {
    "ping.avg_rtt_ms": "ms",
    "ping.loss_pct": "%",
    "inlab_ping.avg_rtt_ms": "ms",
    "inlab_ping.loss_pct": "%",
    "if.rx_bytes": "bytes",
    "if.tx_bytes": "bytes",
    "if.rx_errors": "count",
    "if.tx_errors": "count",
    "if.rx_dropped": "count",
    "if.tx_dropped": "count",
    "if.operstate": "link",
    "frr.routes_total": "count",
}


@dataclass
class CollectionSummary:
    devices_total: int = 0
    devices_up: int = 0
    samples_stored: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "ok"          # ok | partial | error
    data_source: str = "live"


# --------------------------------------------------------------------------
# Real probe implementations (used in live mode)
# --------------------------------------------------------------------------
def _host_ping(destination: str, inventory: LabInventory) -> connectivity.PingResult:
    return connectivity.run_ping(inventory, destination)


def _inlab_ping(container: str, destination: str,
                inventory: LabInventory) -> connectivity.PingResult:
    """Execute a real ping INSIDE a lab container (never from the host).

    Destination is validated against the inventory first; the command is a
    fixed argv list executed via docker exec with a timeout. If the ping
    binary is missing in the image, the failure is reported honestly.
    """
    try:
        inventory.validate_destination(destination)
    except Exception as exc:
        return connectivity.PingResult(destination=destination,
                                       success=False, error=str(exc))
    result = containerlab_manager.docker_exec(
        container, ["ping", "-c", "2", "-W", "2", destination], timeout=15)
    if not result.ok and not result.stdout.strip():
        return connectivity.PingResult(destination=destination, success=False,
                                       error=(result.error or "ping failed")[:200])
    loss, sent, recv, avg = connectivity._parse_ping_output(result.stdout)
    success = (loss is not None and loss < 100.0)
    return connectivity.PingResult(destination=destination, success=success,
                                   packets_sent=sent, packets_received=recv,
                                   loss_pct=loss, avg_rtt_ms=avg)


def _docker_counter_reader(container: str, iface: str) -> dict[str, float | None] | None:
    """Read real interface statistics via one docker exec call."""
    base = f"/sys/class/net/{iface}/statistics/"
    script_args = [
        "sh", "-c",
        "for f in rx_bytes tx_bytes rx_errors tx_errors rx_dropped tx_dropped; "
        f"do printf '%s ' \"$f\"; cat {base}$f 2>/dev/null || printf 'NA'; "
        "printf '\\n'; done; printf 'operstate '; "
        f"cat /sys/class/net/{iface}/operstate 2>/dev/null || printf NA",
    ]
    result = containerlab_manager.docker_exec(container, script_args, timeout=15)
    if not result.ok or not result.stdout.strip():
        return None
    values: dict[str, float | None] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        key, raw = parts
        if raw == "NA":
            values[key] = None
        elif key == "operstate":
            values[key] = 1.0 if raw.strip().lower() == "up" else 0.0
        else:
            try:
                values[key] = float(raw)
            except ValueError:
                values[key] = None
    return values or None


def _frr_route_info(container: str) -> dict[str, float] | None:
    """Ask FRR for its RIB as JSON (vtysh is present in kind:frr nodes)."""
    result = containerlab_manager.docker_exec(
        container, ["vtysh", "-c", "show ip route json"], timeout=15)
    if not result.ok:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    routes = data.get("routes") or []
    if not isinstance(routes, list):
        return None
    return {"frr.routes_total": float(len(routes))}


# --------------------------------------------------------------------------
# Collection job
# --------------------------------------------------------------------------
def collect_once(
    inventory: LabInventory | None = None,
    host_ping: Callable[[str, LabInventory], connectivity.PingResult] | None = None,
    inlab_ping: Callable[[str, str, LabInventory], connectivity.PingResult] | None = None,
    counter_reader=_docker_counter_reader,
    route_reader=_frr_route_info,
    data_source: str | None = None,
) -> CollectionSummary:
    """Run one full collection cycle and persist everything it measured."""
    settings = get_settings()
    if settings.demo_mode:
        # Demo mode must never touch a real lab. The UI also hides the
        # collector controls, but the guard lives here too so a programmatic
        # or stale-session call cannot fire probes against demo data.
        return CollectionSummary(
            data_source="demo", status="error",
            errors=["demo mode: live collection is disabled "
                    "(no probes were run); demo samples come from seeded data"],
        )
    data_source = data_source or settings.data_source
    inventory = inventory or load_inventory()
    host_ping = host_ping or _host_ping
    inlab_ping = inlab_ping or _inlab_ping
    summary = CollectionSummary(data_source=data_source)

    started = db_utcnow()
    with session_scope() as session:
        run = repository.start_collection_run(session, data_source)

    if not inventory.nodes:
        msg = inventory.source_error or "Lab inventory is empty (topology unreadable)"
        summary.status, summary.errors = "error", [msg]
        with session_scope() as session:
            repository.finish_collection_run(session, run, "error",
                                             error_message=msg[:500])
        return summary

    # 1) Sync inventory into the DB (devices/interfaces/links from lab data).
    with session_scope() as session:
        for node in inventory.nodes:
            device = repository.upsert_device(
                session, name=node.name, type_=node.type,
                mgmt_ip=node.mgmt_ip, lab_ip=node.lab_ip,
                container=node.container,
            )
            for iface in node.interfaces:
                repository.upsert_interface(session, device, iface.name,
                                            ip_cidr=iface.ip_cidr)
        for link in inventory.links:
            repository.upsert_link(session, link.node_a, link.if_a,
                                   link.node_b, link.if_b, subnet=link.subnet)

    summary.devices_total = len(inventory.nodes)
    probes_ok = 0
    probes_failed = 0
    node_statuses: dict[str, str] = {}

    # 2) Per-device measurements.
    for node in inventory.nodes:
        samples: list[dict] = []
        # From the host we only touch the containerlab management address
        # (Docker bridge, host-local). Lab IPs are measured inside the lab.
        target = node.mgmt_ip
        ping_result = None
        if target is not None:
            ping_result = host_ping(target, inventory)
            ok = ping_result.success
            samples.append({"device_name": node.name, "metric": "ping.avg_rtt_ms",
                            "value": ping_result.avg_rtt_ms,
                            "unit": METRIC_UNITS["ping.avg_rtt_ms"]})
            samples.append({"device_name": node.name, "metric": "ping.loss_pct",
                            "value": ping_result.loss_pct,
                            "unit": METRIC_UNITS["ping.loss_pct"]})
            if ok:
                probes_ok += 1
                summary.devices_up += 1
            else:
                probes_failed += 1
                if ping_result.error:
                    summary.errors.append(f"{node.name}: {ping_result.error}")
        else:
            # No address at all (lab not deployed): record unavailability,
            # not a fake measurement.
            samples.append({"device_name": node.name, "metric": "ping.avg_rtt_ms",
                            "value": None, "unit": "ms"})
            probes_failed += 1

        status = "up" if (ping_result and ping_result.success) else (
            "down" if ping_result else "unknown")
        node_statuses[node.name] = status

        # Interface counters (only meaningful when docker runs the node).
        can_read_counters = (
            node.container is not None and containerlab_manager.availability()["docker"]
        )
        iface_states: dict[str, float | None] = {}
        if can_read_counters:
            # In-lab ping: node -> first router loopback that is not itself.
            peer = next((n.loopback_ip for n in inventory.nodes
                         if n is not node and n.loopback_ip), None)
            if peer:
                lab_res = inlab_ping(node.container, peer, inventory)  # type: ignore[arg-type]
                samples.append({"device_name": node.name,
                                "metric": "inlab_ping.avg_rtt_ms",
                                "value": lab_res.avg_rtt_ms,
                                "unit": METRIC_UNITS["inlab_ping.avg_rtt_ms"]})
                samples.append({"device_name": node.name,
                                "metric": "inlab_ping.loss_pct",
                                "value": lab_res.loss_pct,
                                "unit": METRIC_UNITS["inlab_ping.loss_pct"]})
            for iface in node.interfaces:
                stats = counter_reader(node.container, iface.name)  # type: ignore[arg-type]
                if stats is None:
                    samples.append({"device_name": node.name,
                                    "interface_name": iface.name,
                                    "metric": "if.operstate", "value": None,
                                    "unit": "link"})
                    continue
                for key in ("rx_bytes", "tx_bytes", "rx_errors", "tx_errors",
                            "rx_dropped", "tx_dropped", "operstate"):
                    if key in stats:
                        metric = f"if.{key}"
                        samples.append({"device_name": node.name,
                                        "interface_name": iface.name,
                                        "metric": metric,
                                        "value": stats[key],
                                        "unit": METRIC_UNITS.get(metric)})
                iface_states[iface.name] = stats.get("operstate")
            routes = route_reader(node.container)  # type: ignore[arg-type]
            if routes:
                for metric, value in routes.items():
                    samples.append({"device_name": node.name, "metric": metric,
                                    "value": value, "unit": METRIC_UNITS[metric]})

        with session_scope() as session:
            repository.set_device_status(session, node.name, status)
            for sample in samples:
                repository.add_metric_sample(session, data_source=data_source,
                                             collected_at=db_utcnow(), **sample)
            summary.samples_stored += len(samples)
            device = repository.get_device_by_name(session, node.name)
            if status == "down":
                repository.open_incident(session, node.name, "device_unreachable",
                                         (ping_result.error if ping_result else None))
                for iface in node.interfaces:
                    repository.upsert_interface(session, device, iface.name,
                                                status="down")
            elif status == "up":
                repository.resolve_open_incidents(session, node.name,
                                                  "device_unreachable")
                for iface in node.interfaces:
                    state = iface_states.get(iface.name)
                    repository.upsert_interface(
                        session, device, iface.name,
                        status="up" if state == 1.0 else (
                            "down" if state == 0.0 else "unknown"))

    # 3) Link status: up when both endpoint devices are up.
    with session_scope() as session:
        for link in inventory.links:
            status_a = node_statuses.get(link.node_a, "unknown")
            status_b = node_statuses.get(link.node_b, "unknown")
            if status_a == "up" and status_b == "up":
                link_status = "up"
            elif status_a == "unknown" or status_b == "unknown":
                link_status = "unknown"
            else:
                link_status = "down"
            repository.upsert_link(
                session, link.node_a, link.if_a, link.node_b, link.if_b,
                subnet=link.subnet, status=link_status,
            )

    # 4) Close the run record. Status rule (documented):
    #    error   - no device could be probed at all (lab not reachable)
    #    partial - at least one probe or metric source failed
    #    ok      - every device was probed successfully
    if summary.devices_total > 0 and probes_ok == 0:
        summary.status = "error"
    elif probes_failed or summary.errors:
        summary.status = "partial"
    error_text = "; ".join(summary.errors)[:500] if summary.errors else None
    with session_scope() as session:
        repository.finish_collection_run(
            session, run, summary.status,
            devices_total=summary.devices_total,
            devices_up=summary.devices_up, error_message=error_text)
    logger.info("collection finished: %s (status=%s)", summary, summary.status)
    return summary
