"""Page 4 - Connectivity Tests (allowlisted lab destinations only)."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from services.app_state import bootstrap, demo_banner_if_active, read, sidebar_status

from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Connectivity Tests",
                  "Ping / traceroute against the lab allowlist", icon="📡")
demo_banner_if_active()

from config import get_settings
from database import repository
from network import connectivity
from network.device_inventory import DestinationNotAllowed, load_inventory

settings = get_settings()
inventory = load_inventory()

if not inventory.nodes:
    st.error(f"Lab inventory unavailable: {inventory.source_error or 'empty'}",
             icon="🚫")
    st.stop()

st.caption(
    "Tests are executed by the collector host with fixed `ping`/`traceroute` "
    f"commands (timeouts: ping {settings.ping_timeout_seconds}s, "
    f"traceroute {settings.traceroute_timeout_seconds}s). Destinations are "
    "validated against the lab allowlist built from the topology file; "
    "arbitrary addresses are rejected."
)
if settings.demo_mode:
    st.warning(
        "Demo mode: results below are produced by the real ping binary "
        "against the *host's own loopback adapter* purely to exercise the "
        "toolchain - they are NOT lab measurements and are stored with "
        "source='demo'.", icon="⚠️")

col_src, col_dst = st.columns(2)
source = col_src.selectbox("Source", ["collector-host"] + inventory.node_names,
                           index=0)
destination = col_dst.selectbox("Destination (lab allowlist)",
                                 inventory.node_names, index=0 if inventory.node_names else 0)

run_ping = st.button("🛰 Run ping", type="primary")
run_tr = st.button("🧭 Run traceroute")

if run_ping:
    if settings.demo_mode:
        # Real ping binary against the host loopback only (no network):
        # exercises execution/parsing/storage, clearly labelled 'demo'.
        probe_result = connectivity.run_selftest_ping()
    else:
        probe_result = connectivity.run_ping(inventory, destination)

    if probe_result.success:
        st.success(
            f"Ping {probe_result.destination}: OK - "
            f"avg RTT {probe_result.avg_rtt_ms if probe_result.avg_rtt_ms is not None else 'n/a'} ms, "
            f"loss {probe_result.loss_pct if probe_result.loss_pct is not None else 'n/a'} %"
        )
    else:
        st.error(f"Ping {probe_result.destination}: FAILED - "
                 f"{probe_result.error or 'no reply (see loss below)'}")
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
            s, source=source, destination=destination, test_type="ping",
            success=probe_result.success,
            detail=(probe_result.error or "ok")[:500],
            avg_latency_ms=probe_result.avg_rtt_ms,
            packet_loss_pct=probe_result.loss_pct,
            data_source="demo" if settings.demo_mode else "live")
    read(_store)

if run_tr:
    if settings.demo_mode:
        st.info("Demo mode: traceroute targets do not exist without the lab; "
                "live mode only.")
    else:
        result = connectivity.run_traceroute(inventory, destination)
        if not result.supported:
            st.warning("traceroute is not supported on this host.", icon="🧭")
        elif result.success:
            st.success(f"Traceroute {result.destination}: reached destination")
        else:
            st.error(f"Traceroute {result.destination}: failed - "
                     f"{result.error or 'destination not reached'}")
        if result.hops:
            st.dataframe(pd.DataFrame(result.hops), hide_index=True,
                         width="stretch")

        def _store_tr(s):
            repository.add_connectivity_test(
                s, source="collector-host", destination=destination,
                test_type="traceroute", success=result.success,
                detail=(result.error or "ok")[:500],
                path_json=json.dumps(result.hops) if result.hops else None,
                data_source="live")
        read(_store_tr)

st.subheader("Recent tests (from database)")
history = read(lambda s: list(repository.get_connectivity_history(s, limit=25)))
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
    st.caption("No tests recorded yet.")
