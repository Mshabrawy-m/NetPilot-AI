"""NetPilot AI - entry point (Streamlit).

Run with:  streamlit run app.py
The functional pages live in ``pages/`` (Streamlit multipage app). This
landing page presents a branded overview and steers the user into the app.
"""
from __future__ import annotations

import streamlit as st

from services.app_state import bootstrap, demo_banner_if_active, sidebar_status
from ui import theme

bootstrap()
sidebar_status()

theme.page_header(
    "NetPilot AI",
    "Virtual network lab · real telemetry · anomaly & forecast analytics · "
    "Arabic/English AI operations assistant  (Phases 1–5)",
    icon="🛰️")
demo_banner_if_active()

st.markdown(
    "**NetPilot AI** gives you a reproducible virtual network lab "
    "(Containerlab + FRRouting), periodic collection of *real* measurements, "
    "a history database, deep analytics, and an evidence-grounded AI assistant "
    "that always stops at *proposal + human review* — it never deploys changes."
)


def _section(label: str) -> None:
    st.markdown(f"<div class='np-sec'>{label}</div>", unsafe_allow_html=True)


c1, c2, c3, c4 = st.columns(4, gap="medium")
with c1:
    _section("🧭 Foundation")
    st.page_link("pages/1_Overview.py", label="Overview", icon="📊")
    st.page_link("pages/2_Topology.py", label="Network Topology", icon="🕸️")
    st.page_link("pages/3_Devices.py", label="Devices & Interfaces", icon="🖥️")
    st.page_link("pages/4_Connectivity_Tests.py", label="Connectivity Tests", icon="📡")
with c2:
    _section("📈 Analysis")
    st.page_link("pages/5_Metrics_History.py", label="Metrics & History", icon="📈")
    st.page_link("pages/6_System_Status.py", label="System Status", icon="🩺")
    st.page_link("pages/7_Data_Analysis.py", label="Data Analysis", icon="🔬")
    st.page_link("pages/8_Anomalies.py", label="Anomalies", icon="🚨")
with c3:
    _section("🤖 Intelligence")
    st.page_link("pages/9_Forecasting.py", label="Forecasting", icon="🔮")
    st.page_link("pages/10_AI_Network_Assistant.py", label="AI Network Assistant", icon="💬")
    st.page_link("pages/11_Ops_Center.py", label="Ops Center", icon="🛎️")
with c4:
    _section("🧪 Experimentation")
    st.page_link("pages/12_AI_Lab.py", label="AI Lab", icon="🧪")
    st.caption(
        "Simulated, historical, uploaded or live telemetry run through the "
        "same anomaly / forecasting / classification / statistics pipeline. "
        "Read-only: nothing here touches a device.")

st.divider()
s1, s2 = st.columns([2, 1])
with s1:
    st.markdown("<div class='np-sec'>Safety guarantees</div>", unsafe_allow_html=True)
    st.caption(
        "- Network commands run only against the NetPilot lab allowlist.\n"
        "- No arbitrary shell input is executed; generated config is never applied.\n"
        "- An independent deterministic verifier gates every proposal.\n"
        "- Demo data is always labelled and stored separately from live data.\n"
        "- No credentials are stored in the app or the database."
    )
with s2:
    st.markdown("<div class='np-sec'>Getting started</div>", unsafe_allow_html=True)
    st.caption(
        "Start the collector in the sidebar, or explore in clearly-labelled "
        "demo mode. Open the AI assistant and ask why something is wrong — in "
        "English or Arabic."
    )

from config import get_settings
settings = get_settings()
if not settings.demo_mode:
    from network import containerlab_manager
    avail = containerlab_manager.availability()
    if not avail["containerlab"]:
        st.info(
            "Containerlab/Docker is not available on this host, so live lab "
            "data cannot be collected yet. Deploy the lab on a supported "
            "environment (see README), or run the dashboard in clearly "
            "labelled demo mode: `NETPILOT_DEMO_MODE=true streamlit run app.py`.",
            icon="ℹ️",
        )
