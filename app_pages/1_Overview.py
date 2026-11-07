"""Page 1 - Overview."""
from __future__ import annotations

import streamlit as st

from services.app_state import bootstrap, demo_banner_if_active, read, sidebar_status
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Overview", "Live health of the NetPilot lab at a glance", icon="📊")
demo_banner_if_active()

from config import get_settings
from database import repository, source_scope
from services import topology_service
from ui import source_controls

settings = get_settings()
sources = read(lambda s: source_scope.available_sources(s))
source = source_controls.selector(
    sources, key="np_overview_page_source", demo_mode=settings.demo_mode)
devices = read(lambda s: source_scope.filter_devices(
    list(repository.get_devices(s)), source))
status = topology_service.compute_network_status(devices)
# Ensure data_source is properly typed: None for demo mode, string otherwise
data_source_value = None if source == source_scope.DEMO else str(source)
last_run = read(lambda s: repository.get_last_successful_run(
    s, data_source=data_source_value))

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
        "No successful collection is recorded yet for this source. Run the "
        "native collector or seed the labelled simulated source; Containerlab "
        "is not required for Live Mode.",
        icon="⏱",
    )

if not devices:
    st.info("No rows exist yet for the selected source. Devices appear after "
            "a collection cycle or after `python tools/seed_simulated.py`.")

if status.total:
    st.subheader("Devices at a glance")
    st.dataframe(
        [{"Device": d.name, "Type": d.type, "Status": d.status,
          "Lab IP": d.lab_ip or "-", "Mgmt IP": d.mgmt_ip or "-"}
         for d in devices],
        hide_index=True, width="stretch",
    )
