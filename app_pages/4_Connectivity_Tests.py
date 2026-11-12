"""Page 4 - Connectivity Tests (allowlisted real targets, stored history).

The allowlist is built from the devices actually stored for the selected
source - not from Containerlab availability.  Live Mode therefore works with
the native collector's targets; a missing lab only means there are no rows to
probe, which is reported honestly.  Simulated rows have no real destination, so
execution is disabled for that source instead of fabricating a result.
"""
from __future__ import annotations

import ipaddress
import json

import pandas as pd
import streamlit as st

from services.app_state import (
    bootstrap,
    demo_banner_if_active,
    read,
    sidebar_status,
)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Connectivity Tests",
                  "Ping / traceroute against stored, allowlisted targets",
                  icon="📡")
demo_banner_if_active()

from config import get_settings
from database import repository, source_scope
from network import connectivity
from network.device_inventory import InventoryNode, LabInventory, load_inventory
from ui import source_controls

settings = get_settings()


def _is_private_target(target: str) -> bool:
    """True when the probed literal is an RFC1918/loopback-style address."""
    try:
        return ipaddress.ip_address(target).is_private
    except ValueError:
        return False


def _private_target_note(target: str) -> None:
    if _is_private_target(target):
        st.caption(
            "This is a private address. A collector running outside that LAN "
            "(for example Streamlit Community Cloud) cannot route to it; the "
            "failure describes the collector host's reachability, not the "
            "device's state. Run the collector inside the lab network for a "
            "real measurement.")

sources = read(lambda s: source_scope.available_sources(s))
source = source_controls.selector(
    sources, key="np_connectivity_source", demo_mode=settings.demo_mode)
source_filter = None if source == source_scope.DEMO else source

stored_devices = read(lambda s: repository.filter_reporting_devices(
    source_scope.filter_devices(list(repository.get_devices(s)), source)))

# Build a validated allowlist from stored rows (name -> address).  This is the
# same allowlist the probe helper validates against; arbitrary addresses are
# still rejected.
nodes = []
for device in stored_devices:
    address = device.mgmt_ip or device.lab_ip
    if not address:
        continue
    nodes.append(InventoryNode(
        name=device.name, type=device.type, loopback_ip=address,
        mgmt_ip=device.mgmt_ip))
allowlist = LabInventory(nodes=nodes, links=[])

if not allowlist.nodes and settings.demo_mode:
    # Demo mode may not have seeded its inventory yet; the topology file is a
    # valid demo allowlist and never a live claim.
    allowlist = load_inventory()

st.caption(
    "Tests are executed by the collector host with fixed `ping`/`traceroute` "
    f"commands (timeouts: ping {settings.ping_timeout_seconds}s, "
    f"traceroute {settings.traceroute_timeout_seconds}s). Destinations come "
    "from stored devices for the selected source; arbitrary addresses are "
    "rejected."
)
if settings.demo_mode:
    st.warning(
        "Demo mode: results below are produced by the real ping binary "
        "against the *host's own loopback adapter* purely to exercise the "
        "toolchain - they are NOT lab measurements and are stored with "
        "source='demo'.", icon="⚠️")
elif source == source_scope.SIMULATED:
    st.info(
        "Simulated rows have no real probe destination. This source uses a "
        "localhost self-test only; the result is clearly marked as simulated, "
        "not a live network probe.", icon="🧪")

# Capability check first: a runtime without a ping binary cannot measure
# anything, and saying so up front beats a misleading per-probe failure.
ping_ready = connectivity.ping_supported()
if not ping_ready:
    st.warning(
        "`ping` is not available in this runtime, so no probe can run here. "
        "Streamlit Community Cloud and `python:*-slim` base images omit the "
        "binary: add a `packages.txt` file containing `iputils-ping` at the "
        "repository root (the Docker image already installs it) and reboot "
        "the app.", icon="🚫")
elif not connectivity.traceroute_supported():
    st.warning(
        "`traceroute`/`tracert` is not installed, and the ping-TTL fallback is "
        "also unavailable in this runtime. Ping still works; add `traceroute` "
        "to `packages.txt` to enable native traces.", icon="🧭")

can_probe = bool(allowlist.nodes) and source != source_scope.SIMULATED
if not allowlist.nodes:
    st.info(
        "No stored device has a probe address for this source yet. Run the "
        "native collector (`python tools/live_collector.py --once`) or seed "
        "the labelled simulated source (`python tools/seed_simulated.py`). "
        "Containerlab is not required for Live Mode.", icon="📭")

if source == source_scope.SIMULATED:
    source_name = "loopback self-test"
    destination = "127.0.0.1"
    run_ping = st.button("🧪 Run simulated loopback self-test", type="primary")
    run_tr = False
elif can_probe:
    col_src, col_dst = st.columns(2)
    source_name = col_src.selectbox(
        "Source", ["collector-host"] + allowlist.node_names, index=0)
    destination = col_dst.selectbox(
        "Destination (allowlist)", allowlist.node_names, index=0)

    run_ping = st.button("🛰 Run ping", type="primary")
    run_tr = st.button("🧭 Run traceroute")
else:
    run_ping = run_tr = False
    source_name = destination = None

if run_ping:
    if settings.demo_mode or source == source_scope.SIMULATED:
        # Real ping binary against the host loopback only (no network):
        # exercises execution/parsing/storage, clearly labelled 'demo' or
        # 'simulated'. This is a self-test, never a real network probe.
        probe_result = connectivity.run_selftest_ping()
    else:
        probe_result = connectivity.run_ping(allowlist, destination)

    if probe_result.unsupported:
        # The probe never ran: report the environment limitation and store
        # nothing, so history holds no fake measurement or fake failure.
        st.warning(f"Ping {probe_result.destination}: NOT RUN - "
                   f"{probe_result.error or 'probe unsupported here'}")
        st.caption("No packet was sent and nothing was recorded. This is an "
                   "environment limitation, not a failed connectivity test.")
    else:
        if probe_result.success:
            st.success(
                f"Ping {probe_result.destination}: OK - "
                f"avg RTT {probe_result.avg_rtt_ms if probe_result.avg_rtt_ms is not None else 'n/a'} ms, "
                f"loss {probe_result.loss_pct if probe_result.loss_pct is not None else 'n/a'} %"
            )
        else:
            detail = probe_result.error
            if not detail:
                stats = []
                if (probe_result.packets_received is not None
                        and probe_result.packets_sent is not None):
                    stats.append(f"{probe_result.packets_received}/"
                                 f"{probe_result.packets_sent} replies")
                if probe_result.loss_pct is not None:
                    stats.append(f"{probe_result.loss_pct:.0f}% packet loss")
                detail = ", ".join(stats) or "no reply"
            st.error(f"Ping {probe_result.destination}: FAILED - {detail}")
            _private_target_note(probe_result.destination)
        st.dataframe(pd.DataFrame([{
            "destination": probe_result.destination,
            "sent": probe_result.packets_sent, "received": probe_result.packets_received,
            "loss %": probe_result.loss_pct, "avg RTT ms": probe_result.avg_rtt_ms,
            "success": probe_result.success,
        }]), hide_index=True, width="stretch")
        if probe_result.raw_output:
            with st.expander("Raw output (truncated by policy)"):
                st.code(probe_result.raw_output[:2000])

        def _store(s):
            repository.add_connectivity_test(
                s, source=source_name, destination=destination, test_type="ping",
                success=probe_result.success,
                detail=(probe_result.error or "ok")[:500],
                avg_latency_ms=probe_result.avg_rtt_ms,
                packet_loss_pct=probe_result.loss_pct,
                data_source=("demo" if settings.demo_mode
                             else source_scope.SIMULATED if source == source_scope.SIMULATED
                             else "live"))
        read(_store)

if run_tr:
    if settings.demo_mode:
        st.info("Demo mode: traceroute targets do not exist without the lab; "
                "live mode only.")
    else:
        result = connectivity.run_traceroute(allowlist, destination)
        if not result.supported:
            st.warning(
                "No traceroute tool is available on this host: neither "
                "`traceroute`/`tracert` nor `ping` could be executed. Add "
                "`traceroute` (and keep `iputils-ping`) to `packages.txt` for "
                "Streamlit Cloud, or rebuild the Docker image; the code also "
                "has a bounded ping-TTL fallback that needs only `ping`.",
                icon="🧭")
        elif result.success:
            st.success(f"Traceroute {result.destination}: reached destination"
                       + (f" (method: {result.method})" if result.method else ""))
        else:
            last = next((hop for hop in reversed(result.hops or [])
                         if hop.get("ip")), None)
            suffix = ""
            if last is not None:
                suffix = (f" (last responding hop: {last['ip']}, "
                          f"ttl {last['ttl']})")
            st.error(f"Traceroute {result.destination}: failed - "
                     f"{result.error or 'destination not reached'}{suffix}")
            _private_target_note(result.destination)
        if result.hops:
            st.dataframe(pd.DataFrame(result.hops), hide_index=True,
                         width="stretch")
            st.caption(
                f"Measured path via **{result.method or 'unknown method'}**. "
                "A `None` reply is an unanswered probe, not an invented hop."
                + (" The ping-TTL fallback sends one echo per TTL; it is a "
                   "real measurement, not a native traceroute trace."
                   if result.method == "ping_ttl" else ""))

        def _store_tr(s):
            repository.add_connectivity_test(
                s, source="collector-host", destination=destination,
                test_type="traceroute", success=result.success,
                detail=(result.error or "ok")[:500],
                path_json=json.dumps(result.hops) if result.hops else None,
                data_source="live")
        read(_store_tr)

st.subheader("Recent tests (from database)")
history = read(lambda s: list(repository.get_connectivity_history(
    s, limit=25, data_source=source_filter)))
if history:
    st.dataframe(
        pd.DataFrame([{
            "Tested (UTC)": h.tested_at, "Source": h.source,
            "Destination": h.destination, "Type": h.test_type,
            "Success": h.success, "Avg RTT ms": h.avg_latency_ms,
            "Loss %": h.packet_loss_pct, "Data": h.data_source,
            "Detail": (h.detail or "")[:60],
        } for h in history]),
        hide_index=True, width="stretch",
    )
else:
    st.caption("No tests recorded yet for this source.")
