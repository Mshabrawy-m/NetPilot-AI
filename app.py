"""NetPilot AI entry point and grouped workflow navigation."""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="NetPilot AI", page_icon="🛰️", layout="wide")


def _overview() -> None:
    """Evidence-backed landing page with the most useful network signals."""
    from services.app_state import (
        bootstrap, demo_banner_if_active, read, sidebar_status,
    )
    from ui import theme

    bootstrap()
    sidebar_status()
    theme.page_header(
        "Network overview",
        "Current inventory, observed network health, and collection freshness",
        icon="📊",
    )
    demo_banner_if_active()

    from database import repository
    from services import topology_service

    devices = read(lambda session: list(repository.get_devices(session)))
    status = topology_service.compute_network_status(devices)
    last_run = read(lambda session: repository.get_last_successful_run(session))

    tone = {
        "healthy": "ok", "degraded": "warn", "down": "bad", "unknown": "muted",
    }
    label = {
        "healthy": "Healthy", "degraded": "Degraded", "down": "Down", "unknown": "Unknown",
    }
    theme.kpi_row([
        ("Network status", label[status.overall],
         "Derived from registered device status", tone[status.overall]),
        ("Devices up", f"{status.up} / {status.total}",
         "Current inventory status", "ok" if status.up else "muted"),
        ("Devices down", status.down, "Current inventory status",
         "bad" if status.down else "muted"),
    ])

    if last_run is not None:
        st.caption(
            f"Latest successful collection · "
            f"{last_run.finished_at:%Y-%m-%d %H:%M:%S} UTC · "
            f"{last_run.devices_up}/{last_run.devices_total} devices up · "
            f"source: {last_run.data_source}"
        )
    else:
        st.info(
            "No successful collection is recorded yet. Start collection from "
            "the sidebar or explore clearly labelled demo data."
        )

    if status.total:
        st.subheader("Device inventory")
        st.dataframe(
            [{"Device": device.name, "Type": device.type, "Status": device.status,
              "Lab IP": device.lab_ip or "—", "Management IP": device.mgmt_ip or "—"}
             for device in devices],
            hide_index=True, width="stretch",
        )
    else:
        st.info(
            "The inventory is empty. Devices appear after a successful "
            "collection or when demo mode is enabled."
        )

    from config import get_settings
    from network import containerlab_manager
    settings = get_settings()
    availability = containerlab_manager.availability()
    if not settings.demo_mode and not availability["containerlab"]:
        st.warning(
            "Live telemetry is unavailable because Containerlab is not "
            "installed or reachable on this host."
        )


pages = st.navigation(
    {
        "Overview": [
            st.Page(_overview, title="Network overview", icon="📊", default=True),
        ],
        "Network operations": [
            st.Page("pages/2_Topology.py", title="Topology", icon="🕸️"),
            st.Page("pages/3_Devices.py", title="Devices & interfaces", icon="🖥️"),
            st.Page("pages/4_Connectivity_Tests.py", title="Connectivity tests", icon="📡"),
            st.Page("pages/5_Metrics_History.py", title="Metrics & history", icon="📈"),
            st.Page("pages/8_Anomalies.py", title="Anomalies", icon="🚨"),
            st.Page("pages/11_Ops_Center.py", title="Operations center", icon="🛎️"),
        ],
        "AI lab": [
            st.Page("pages/7_Data_Analysis.py", title="Data analysis", icon="🔬"),
            st.Page("pages/9_Forecasting.py", title="Forecasting", icon="🔮"),
            st.Page("pages/12_AI_Lab.py", title="Experiments", icon="🧪"),
            st.Page("pages/10_AI_Network_Assistant.py", title="Network assistant", icon="💬"),
        ],
        "System status": [
            st.Page("pages/6_System_Status.py", title="System status", icon="🩺"),
        ],
    },
    position="sidebar",
    expanded=True,
)
pages.run()
