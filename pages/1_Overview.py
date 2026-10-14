"""Page 1 - Overview."""
from __future__ import annotations

import streamlit as st

from services.app_state import bootstrap, demo_banner_if_active, read, sidebar_status
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Overview", "Live health of the NetPilot lab at a glance", icon="📊")
demo_banner_if_active()

from database import repository
from services import topology_service

devices = read(lambda s: list(repository.get_devices(s)))
status = topology_service.compute_network_status(devices)
last_run = read(lambda s: repository.get_last_successful_run(s))

_tone = {"healthy": "ok", "degraded": "warn", "down": "bad", "unknown": "muted"}
_badge = {"healthy": "🟢 HEALTHY", "degraded": "🟡 DEGRADED",
          "down": "🔴 DOWN", "unknown": "⚪ UNKNOWN"}
theme.kpi_row([
    ("Total devices", status.total),
    ("Available (up)", status.up, "", "ok"),
    ("Unavailable (down)", status.down, "", "bad" if status.down else "muted"),
])
st.markdown(
    "<div style='display:flex;align-items:center;gap:.6rem;margin:.35rem 0 .2rem 0'>"
    "<span style='font-size:.72rem;letter-spacing:1.2px;text-transform:uppercase;"
    "color:#8FA1BF'>Network status</span>"
    + theme.status_pill(_badge[status.overall], _tone[status.overall]) + "</div>",
    unsafe_allow_html=True)

st.caption(
    "Status rule (documented in services/topology_service.py): "
    "HEALTHY = all devices up, DEGRADED = at least ceil(N/2) up, "
    "DOWN = fewer, UNKNOWN = no devices registered."
)

if last_run is not None:
    st.success(
        f"Last successful data collection (UTC): "
        f"{last_run.finished_at:%Y-%m-%d %H:%M:%S} "
        f"- {last_run.devices_up}/{last_run.devices_total} devices up "
        f"[source: {last_run.data_source}]"
    )
else:
    st.warning(
        "No successful data collection yet. Deploy the lab and press "
        "'Collect now' in the sidebar, or enable demo mode (see README).",
        icon="⏱",
    )

from config import get_settings
from network import containerlab_manager
settings = get_settings()
avail = containerlab_manager.availability()
if not settings.demo_mode and not avail["containerlab"]:
    st.error(
        "Cannot connect to the virtual lab: Containerlab is not installed on "
        "this host. Live metrics are unavailable until the lab is deployed on "
        "a Docker-supported environment (README → Lab deployment).",
        icon="🚫",
    )
if not devices:
    st.info("The inventory is empty - devices appear after the first "
            "collection cycle or in demo mode.")

if status.total:
    st.subheader("Devices at a glance")
    st.dataframe(
        [{"Device": d.name, "Type": d.type, "Status": d.status,
          "Lab IP": d.lab_ip or "-", "Mgmt IP": d.mgmt_ip or "-"}
         for d in devices],
        hide_index=True, width="stretch",
    )
