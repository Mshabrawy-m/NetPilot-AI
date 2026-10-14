"""Page 6 - System Status (database, lab, collection runs, errors)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from services.app_state import bootstrap, demo_banner_if_active, read, sidebar_status

from ui import theme

bootstrap()
sidebar_status()
theme.page_header("System Status",
                  "Database, lab, collection runs and errors", icon="🩺")
demo_banner_if_active()

from config import get_settings
from database import repository
from database.connection import check_connection
from network import containerlab_manager
from services import collection_service

settings = get_settings()

st.subheader("Database")
db_ok, db_info = check_connection()
if db_ok:
    st.success(f"Database reachable. Dialect: `{db_info.split(':///')[0]}` "
               f"(passwords are never displayed)")
else:
    st.error(f"Database unreachable: {db_info}", icon="💥")

st.subheader("Virtual lab")
avail = containerlab_manager.availability()
c1, c2, c3 = st.columns(3)
c1.metric("Docker installed", "yes" if avail["docker"] else "no")
c2.metric("Containerlab installed", "yes" if avail["containerlab"] else "no")
nodes = containerlab_manager.inspect_nodes() if avail["containerlab"] else []
c3.metric("Lab nodes running", len([n for n in nodes
                                    if str(n.get("state", "")).lower() in {"running", "up"}]))
if not avail["containerlab"]:
    st.warning(
        "Containerlab is not available on this host, so the live lab cannot "
        "be inspected or measured here. Deploy the lab on a Docker-supported "
        "system (README → Lab deployment). This is reported honestly instead "
        "of shown as fake data.", icon="🧪")
elif not nodes:
    st.warning("Containerlab exists but no running 'netpilot' lab was found. "
               "Deploy it with `containerlab deploy -t lab/topology.clab.yml`."
               , icon="🕸")

st.subheader("Collector scheduler")
scheduler = st.session_state["_scheduler"]
status = scheduler.status()
sc1, sc2, sc3, sc4 = st.columns(4)
sc1.metric("State", "running" if status.running else "idle")
sc2.metric("Interval", f"{status.interval_seconds}s")
sc3.metric("Cycles done", status.cycles_completed)
sc4.metric("Skipped overlaps", status.skipped_overlaps)

st.subheader("Recent collection runs")
runs = read(lambda s: list(repository.get_recent_runs(s, limit=15)))
if runs:
    st.dataframe(
        pd.DataFrame([{
            "Started (UTC)": r.started_at, "Finished (UTC)": r.finished_at,
            "Status": r.status, "Devices": f"{r.devices_up}/{r.devices_total}",
            "Source": r.data_source, "Error": (r.error_message or "")[:80],
        } for r in runs]),
        hide_index=True, width="stretch",
    )
else:
    st.caption("No collection runs recorded yet.")

if status.last_error:
    st.error(f"Last scheduler error: {status.last_error}", icon="⚠️")

st.subheader("Open incidents")
incidents = read(lambda s: list(repository.get_open_incidents(s)))
if incidents:
    st.dataframe(
        pd.DataFrame([{
            "Opened (UTC)": i.opened_at, "Device": i.device_name,
            "Kind": i.kind, "Detail": (i.detail or "")[:80],
        } for i in incidents]),
        hide_index=True, width="stretch",
    )
else:
    st.caption("No open incidents.")

with st.expander("Reset disposable lab data"):
    db_kind = "demo DB" if settings.demo_mode else "live DB"
    st.caption("Deletes all rows from NetPilot's own database "
               f"({db_kind} only). "
               "Never touches the lab itself.")
    confirm = st.checkbox("I understand: wipe all stored measurements")
    if st.button("Reset database rows", disabled=not confirm):
        from database.connection import reset_lab_data
        tables = reset_lab_data()
        st.success(f"Cleared data from {tables} tables.")
