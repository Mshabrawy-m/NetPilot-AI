"""Page 3 - Devices & Interfaces."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from services.app_state import bootstrap, demo_banner_if_active, read, sidebar_status

from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Devices & Interfaces",
                  "Lab inventory, status and interfaces", icon="🖥️")
demo_banner_if_active()

from config import get_settings
from database import repository, source_scope
from database.models import Interface
from ui import source_controls

settings = get_settings()
sources = read(lambda s: source_scope.available_sources(s))
source = source_controls.selector(
    sources, key="np_devices_source", demo_mode=settings.demo_mode)
devices = read(lambda s: repository.filter_reporting_devices(
    source_scope.filter_devices(list(repository.get_devices(s)), source)))
if not devices:
    st.info("No devices exist for the selected source yet. Run a collection "
            "cycle or `python tools/seed_simulated.py`.")
    st.stop()

st.dataframe(
    pd.DataFrame([{
        "Device": d.name, "Type": d.type, "Status": d.status,
        "Lab IP": d.lab_ip or "-", "Mgmt IP": d.mgmt_ip or "-",
        "Container": d.container or "-",
        "Updated (UTC)": d.updated_at,
    } for d in devices]),
    hide_index=True, width="stretch",
)

st.subheader("Device detail")
selected_name = st.selectbox("Select a device", [d.name for d in devices])
device = next(d for d in devices if d.name == selected_name)

col1, col2 = st.columns(2)
with col1:
    st.markdown(f"**{device.name}** - `{device.type}` - status: `{device.status}`")
    st.write(f"- Lab IP: {device.lab_ip or 'n/a'}")
    st.write(f"- Mgmt IP: {device.mgmt_ip or 'n/a (lab not deployed?)'}")
with col2:
    interfaces = read(lambda s: list(
        s.query(Interface).filter_by(device_id=device.id).all()))
    st.markdown("**Interfaces**")
    st.dataframe(
        pd.DataFrame([{"Interface": i.name, "IP/prefix": i.ip_cidr or "-",
                       "Status": i.status} for i in interfaces]),
        hide_index=True, width="stretch",
    )

st.markdown("**Latest measured values** (null = metric was not available; "
            "never invented)")
latest = read(lambda s: repository.latest_metric_values(s, device.name))
if latest:
    st.dataframe(
        pd.DataFrame([{
            "Metric": m.metric, "Interface": m.interface_name or "-",
            "Value": m.value, "Unit": m.unit or "-",
            "Collected (UTC)": m.collected_at, "Source": m.data_source,
        } for m in latest]),
        hide_index=True, width="stretch",
    )
else:
    st.caption("No measurements stored for this device yet.")
