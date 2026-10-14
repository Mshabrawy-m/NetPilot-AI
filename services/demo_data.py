"""DEMO MODE fixtures - synthetic, clearly labelled, and never mixed with
live lab data.

Enabled only with NETPILOT_DEMO_MODE=true. In that mode the default
database file switches to ``data/netpilot_demo.db`` (a separate store), and
every row written carries ``data_source='demo'``. The UI shows a persistent
warning banner whenever demo data is present.

Purpose: let the dashboard, charts and history pages be developed and
verified on machines without Docker/Containerlab. The values below are
deterministic pseudo-random fixtures for UI verification ONLY - they are
not measurements of any real network.
"""
from __future__ import annotations

import math
import random
from datetime import timedelta

from config import get_settings, utc_now
from database import repository
from database.connection import session_scope, init_db
from network.device_inventory import load_inventory

DEMO_BANNER = (
    "DEMO MODE: the data on this page is synthetic fixture data "
    "(NETPILOT_DEMO_MODE=true), NOT measurements from a real network."
)


def seed_demo_data(hours: int = 6, interval_minutes: int = 5,
                   reset_first: bool = True) -> dict:
    """Populate the demo database with a labelled history for all pages.

    Mirrors the real lab inventory (topology file) so the demo topology
    matches what a live deployment would show. Returns counts for logging.
    """
    settings = get_settings()
    if not settings.demo_mode:
        raise RuntimeError(
            "seed_demo_data() must only run when NETPILOT_DEMO_MODE=true")
    init_db()
    if reset_first:
        with session_scope() as session:
            repository.purge_metric_samples(session)

    inventory = load_inventory()
    nodes = inventory.nodes or []
    if not nodes:
        raise RuntimeError("Topology file unreadable - cannot seed demo data")

    rng = random.Random(20260101)          # deterministic demo series
    now = utc_now().replace(tzinfo=None)   # naive UTC, storage convention
    points = max(4, int(hours * 60 / interval_minutes))
    step = timedelta(minutes=interval_minutes)
    devices_up = {n.name: rng.random() > 0.15 for n in nodes}
    # Keep at least one device down so 'degraded' rendering can be verified.
    if all(devices_up.values()) and nodes:
        devices_up[nodes[-1].name] = False

    counts = {"devices": 0, "links": 0, "samples": 0, "tests": 0}
    with session_scope() as session:
        for node in nodes:
            status = "up" if devices_up[node.name] else "down"
            device = repository.upsert_device(
                session, name=node.name, type_=node.type,
                lab_ip=node.lab_ip, container=f"demo-{node.name}",
                status=status)
            for iface in node.interfaces:
                repository.upsert_interface(
                    session, device, iface.name, ip_cidr=iface.ip_cidr,
                    status="up" if status == "up" else "down")
            counts["devices"] += 1
        for link in inventory.links:
            both_up = devices_up.get(link.node_a, False) and \
                devices_up.get(link.node_b, False)
            repository.upsert_link(session, link.node_a, link.if_a,
                                   link.node_b, link.if_b, subnet=link.subnet,
                                   status="up" if both_up else "down")
            counts["links"] += 1

    with session_scope() as session:
        run = repository.start_collection_run(session, "demo")
        for i in range(points):
            ts = now - step * (points - 1 - i)
            for node in nodes:
                if not devices_up[node.name]:
                    # Down device: RTT is genuinely unavailable -> NULL,
                    # loss is 100%. Mirrors live collector semantics.
                    repository.add_metric_sample(
                        session, node.name, "ping.avg_rtt_ms", None,
                        unit="ms", data_source="demo", collected_at=ts)
                    repository.add_metric_sample(
                        session, node.name, "ping.loss_pct", 100.0,
                        unit="%", data_source="demo", collected_at=ts)
                    counts["samples"] += 2
                    continue
                base_rtt = 1.0 + (hash(node.name) % 7)
                jitter = rng.uniform(0, 1.5) + (0.6 * math.sin(i / 3.0))
                rtt = round(max(0.05, base_rtt + jitter), 3)
                loss = round(max(0.0, rng.choice([0.0, 0.0, 0.0, 33.3])), 1)
                repository.add_metric_sample(
                    session, node.name, "ping.avg_rtt_ms", rtt,
                    unit="ms", data_source="demo", collected_at=ts)
                repository.add_metric_sample(
                    session, node.name, "ping.loss_pct", loss,
                    unit="%", data_source="demo", collected_at=ts)
                counts["samples"] += 2
                for iface in node.interfaces[:1]:
                    rx = 1_000_000 * i + rng.randint(0, 50_000)
                    tx = 800_000 * i + rng.randint(0, 40_000)
                    for metric, value, unit in (
                            ("if.rx_bytes", float(rx), "bytes"),
                            ("if.tx_bytes", float(tx), "bytes"),
                            ("if.rx_errors",
                             float(rng.choice([0, 0, 0, 1, 2])), "count"),
                            ("if.operstate", 1.0, "link")):
                        repository.add_metric_sample(
                            session, node.name, metric, value, unit=unit,
                            interface_name=iface.name, data_source="demo",
                            collected_at=ts)
                        counts["samples"] += 1
                repository.add_connectivity_test(
                    session, source="collector-host", destination=node.name,
                    test_type="ping", success=True,
                    avg_latency_ms=rtt, packet_loss_pct=loss,
                    detail="demo fixture", data_source="demo", tested_at=ts)
                counts["tests"] += 1
        repository.finish_collection_run(
            session, run, "partial", devices_total=len(nodes),
            devices_up=sum(1 for v in devices_up.values() if v),
            error_message="demo mode: synthetic data, lab not connected")
    return counts


def seed_demo_fabric(core: int = 2, dist: int = 6, access: int = 12,
                     hosts_per_access: int = 6, seed: int = 4242) -> dict:
    """DEMO-ONLY: expand the demo store with a large synthetic campus fabric.

    Builds a hierarchical network - ``core`` routers, ``dist`` distribution
    and ``access`` switches and ``hosts_per_access`` hosts under each access
    switch (default ~90 devices / ~100 links) - with redundant uplinks so the
    graph views (layered / force / circular / grid) have meaningful structure.
    Everything is written to the separate demo database with clearly
    synthetic, deterministic values; it never touches a live lab. Refuses to
    run unless NETPILOT_DEMO_MODE=true (same guard as ``seed_demo_data``).

    Returns counts {"devices", "links", "samples"}.
    """
    settings = get_settings()
    if not settings.demo_mode:
        raise RuntimeError(
            "seed_demo_fabric() must only run when NETPILOT_DEMO_MODE=true")
    if core < 1 or dist < 1 or access < 1 or hosts_per_access < 0:
        raise ValueError("core/dist/access must be >= 1 (hosts >= 0)")
    init_db()

    rng = random.Random(seed)
    now = utc_now().replace(tzinfo=None)          # naive UTC, storage norm
    cores = [f"CORE{i + 1}" for i in range(core)]
    dists = [f"DIST{i + 1}" for i in range(dist)]
    accs = [f"ACC{i + 1}" for i in range(access)]
    hosts = [f"HOST{i + 1}" for a in range(access)
             for i in range(a * hosts_per_access,
                            a * hosts_per_access + hosts_per_access)]

    def tier_status(name: str) -> str:
        # A deterministic sprinkle of down devices so degraded/down render.
        if hosts and name == hosts[-1]:             # guarantee one down host
            return "down"
        return "up" if rng.random() > 0.08 else "down"

    devices: dict[str, tuple[str, str]] = {}
    for i, n in enumerate(cores):
        devices[n] = ("router", f"10.0.0.{i + 1}")
    for i, n in enumerate(dists):
        devices[n] = ("switch", f"10.1.0.{i + 1}")
    for i, n in enumerate(accs):
        devices[n] = ("switch", f"10.2.0.{i + 1}")
    for i, n in enumerate(hosts):
        devices[n] = ("host", f"10.3.{i // 256}.{i % 256 + 1}")

    # (device_a, if_a, device_b, if_b, subnet) triples for the fabric.
    links: list[tuple[str, str, str, str, str]] = []
    for di, d in enumerate(dists):                 # every dist -> every core
        for ci, c in enumerate(cores):
            links.append((d, f"eth0/{di + 1}", c, f"eth0/{di + 1}",
                          f"10.10.{di}.{ci * 4}/30"))
    for ai, a in enumerate(accs):                  # each acc -> two dists
        primary = dists[ai % dist]
        backup = dists[(ai + 1) % dist]
        links.append((a, "eth0/1", primary, f"eth0/{100 + ai}",
                      f"10.20.{ai}.0/24"))
        links.append((a, "eth0/2", backup, f"eth0/{200 + ai}",
                      f"10.21.{ai}.0/24"))
    for hi, h in enumerate(hosts):                  # each host -> its access
        a = accs[hi // hosts_per_access] if hosts_per_access else None
        if a is None:
            continue
        links.append((h, "eth0", a, f"eth1/{hi % hosts_per_access + 1}",
                      f"10.30.{hi // 6}.{hi % 6 * 16}/28"))

    counts = {"devices": 0, "links": 0, "samples": 0}
    with session_scope() as session:
        for name, (dtype, ip) in devices.items():
            dev = repository.upsert_device(
                session, name=name, type_=dtype, lab_ip=ip,
                container=f"demo-{name}", status=tier_status(name))
            repository.upsert_interface(session, dev, "eth0",
                                        ip_cidr=f"{ip}/24",
                                        status=dev.status)
            counts["devices"] += 1
        for a, ifa, b, ifb, subnet in links:
            both_up = (tier_up(session, a) and tier_up(session, b))
            repository.upsert_link(session, a, ifa, b, ifb, subnet=subnet,
                                   status="up" if both_up else "down")
            counts["links"] += 1
        # Light, honest ping history for the backbone only (keeps the store
        # small; hosts/switches are topology-only in this synthetic fabric).
        for i in range(12):
            ts = now - timedelta(minutes=5) * (11 - i)
            for name in cores + dists:
                if tier_up(session, name):
                    rtt = round(0.5 + rng.uniform(0, 1.2), 3)
                    loss = round(rng.choice([0.0, 0.0, 0.0, 1.0]), 1)
                else:
                    rtt, loss = None, 100.0
                repository.add_metric_sample(
                    session, name, "ping.avg_rtt_ms", rtt, unit="ms",
                    data_source="demo", collected_at=ts)
                repository.add_metric_sample(
                    session, name, "ping.loss_pct", loss, unit="%",
                    data_source="demo", collected_at=ts)
                counts["samples"] += 2
    return counts


def tier_up(session, name: str) -> bool:
    dev = repository.get_device_by_name(session, name)
    return bool(dev and dev.status == "up")
