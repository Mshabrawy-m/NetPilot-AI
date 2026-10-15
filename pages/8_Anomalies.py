"""Page 8 - Anomalies (Phase 2 Scope D: explainable anomaly events).

Events are produced by the documented baseline detectors and persisted by
``services.anomaly_service``. An anomaly is a *statistical deviation worth
investigating* - this page never claims an attack or a confirmed failure,
and demo events are always labelled and filtered separately from live lab
measurements.
"""
from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import streamlit as st

from services.app_state import (bootstrap, demo_banner_if_active, read,
                                sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Anomalies",
                  "Explainable, persisted detection events", icon="🚨")
demo_banner_if_active()

from analytics.time_series import dedupe_frame, load_metric_frame
from database import repository
from services import anomaly_service

st.caption(
    "Anomaly events are explainable statistical deviations (rolling "
    "MAD / z-score baselines), not confirmed attacks or failures. "
    "Detection runs only when you press the button below - never "
    "automatically on every page refresh."
)

# ------------------------------------------------------------------- filters
avail = read(lambda s: repository.get_available_metrics(s))
if not avail:
    st.info("No measured values stored yet - nothing to analyse.")
    st.stop()
sources = sorted({src for (_d, _m, _u, src) in avail})
data_source = st.selectbox("Data source", sources,
                           format_func=lambda v: f"{v} " + (
                               "(synthetic demo data - NOT real traffic)"
                               if v == "demo" else "(virtual lab measurements)"))

fcol1, fcol2 = st.columns([1, 1])
with fcol1:
    run_device = st.selectbox(
        "Device (detection target)",
        sorted({d for (d, _m, _u, s) in avail if s == data_source}))
    metrics = sorted({m for (d, m, _u, s) in avail
                      if s == data_source and d == run_device and m})
    run_metric = st.selectbox("Metric", metrics or ["(none)"])
with fcol2:
    st.markdown("**Detection settings**")
    st.caption("Baselines: rolling MAD + rolling z-score (strictly-past "
               "windows). Grouping/thresholds come from config and are "
               "snapshotted with every event.")
    detectors_txt = st.text_input("Detectors (comma-separated)",
                                  "rolling_mad,rolling_zscore")

# ------------------------------------------------- explicit detection action
if st.button("🔍 Run detection on this series", width="stretch"):
    if not run_metric:
        st.warning("No metric with stored values selected.")
    else:
        rows = read(lambda s: repository.get_metric_series(
            s, run_device, run_metric, data_source=data_source))
        ifaces = sorted({r.interface_name for r in rows if r.interface_name})
        has_dev = any(r.interface_name is None for r in rows)
        options = ([None] if has_dev else []) + ifaces
        if not options:
            st.warning("No stored rows for this metric/source selection - "
                       "nothing to detect on.")
            result = None
        else:
            # ``options`` may legitimately contain ``None`` (the
            # "(device-level)" scope). Compare the *retrieved* value against
            # it rather than subscripting the key directly: a missing key
            # makes ``.get()`` return ``None``, and ``None in options`` is
            # True, so the old form raised KeyError on first render.
            stored_iface = st.session_state.get("_anom_iface")
            chosen_default = (options.index(stored_iface)
                              if stored_iface in options
                              else 0)
            target_iface = st.selectbox(
                "Interface scope for detection (one series at a time)",
                options, index=chosen_default, key="_anom_iface",
                format_func=lambda v: v or "(device-level)")
            names = tuple(
                d.strip() for d in detectors_txt.split(",") if d.strip())
            with st.spinner("Detecting..."):
                result = read(lambda s: anomaly_service.run_detection(
                    s, device_name=run_device, metric=run_metric,
                    interface_name=target_iface, data_source=data_source,
                    detectors=names or None,
                    device_level_only=(target_iface is None)))
        if result is not None and result["status"] == "insufficient_data":
            st.warning("Insufficient data for detection: "
                       + "; ".join(result["reasons"]), icon="📉")
        elif result is not None:
            p = result["persisted"]
            st.success(
                f"Detection finished: {result['n_anomalies']} anomalous "
                f"point(s) on {result['n_series_points']} samples → "
                f"{p['created']} new event(s), {p['updated']} grouped "
                f"update(s), {p['skipped']} suppressed, "
                f"{result['resolved']} event(s) resolved by normal "
                "observations.")

st.divider()

# ------------------------------------------------------------- events table
lcol1, lcol2, lcol3 = st.columns([1, 1, 1])
with lcol1:
    status_filter = st.selectbox("Status", ["open", "resolved", "all"])
with lcol2:
    sev_filter = st.selectbox("Severity", ["all", "critical", "warning"])
with lcol3:
    dev_filter = st.selectbox(
        "Device filter", ["all"] + sorted({e.device_name for e in read(
            lambda s: repository.get_anomaly_events(s))}))

events = read(lambda s: anomaly_service.list_events(
    s, data_source=data_source,
    status=None if status_filter == "all" else status_filter,
    device_name=None if dev_filter == "all" else dev_filter))
if sev_filter != "all":
    events = [e for e in events if e.severity == sev_filter]

st.subheader(f"Anomaly events — source: {data_source} "
             f"({len(events)} shown)")
if not events:
    st.info("No persisted anomaly events match these filters for this "
            "data source. Run detection above, or widen the filters.")
    st.stop()

table = pd.DataFrame([{
    "id": e.id, "severity": e.severity, "status": e.status,
    "first seen (UTC)": e.first_seen_at, "last seen (UTC)": e.last_seen_at,
    "device": e.device_name, "interface": e.interface_name or "-",
    "metric": e.metric, "detector": e.detector,
    "observed": e.observed_value, "baseline": e.baseline_value,
    "threshold": e.threshold_upper if (e.observed_value or 0) >=
    (e.baseline_value or 0) else e.threshold_lower,
    "occurrences": e.occurrences, "source": e.data_source,
} for e in events])
st.dataframe(table, hide_index=True, width="stretch", height=260)

# ---------------------------------------------------- selected event detail
ev_ids = {e.id: e for e in events}
sel_id = st.selectbox("Inspect event", list(ev_ids),
                      format_func=lambda i: f"#{i} · {ev_ids[i].severity} · "
                      f"{ev_ids[i].device_name} · {ev_ids[i].metric} · "
                      f"{ev_ids[i].last_seen_at} UTC")
ev = ev_ids[sel_id]
st.markdown(
    f"**Explanation:** {ev.explanation}\n\n"
    f"- detector `{ev.detector}` v{ev.detector_version}, "
    f"config snapshot: `{ev.detector_config_json}`\n"
    f"- observed **{ev.observed_value}** vs baseline "
    f"**{ev.baseline_value}** (deviation {ev.deviation})\n"
    f"- episode: {ev.first_seen_at} → {ev.last_seen_at} UTC "
    f"({ev.occurrences} occurrence(s))"
    + (f"\n- resolved at **{ev.resolved_at}** UTC by a normal observation"
       if ev.status == "resolved" else ""))
st.caption(
    "⚠️ This is a statistical deviation worth investigating - not evidence "
    "of an attack, and not a confirmed device failure."
    + (" Event derives from SYNTHETIC demo data."
       if ev.data_source == "demo" else ""))

with st.expander("Time-series context (the stored observations)",
                 expanded=True):
    ctx = dedupe_frame(read(lambda s: load_metric_frame(
        s, ev.device_name, ev.metric, interface_name=ev.interface_name,
        data_source=ev.data_source)))
    if ctx.empty:
        st.info("The underlying series has no stored samples - no context "
                "can be shown (and none is invented).")
    else:
        fig = px.line(ctx, x="collected_at", y="value", markers=True,
                      labels={"collected_at": "Time (UTC)",
                              "value": ev.metric})
        fig.update_traces(connectgaps=False)
        # mark the event episode + its peak observation
        fig.add_vrect(x0=ev.first_seen_at, x1=ev.last_seen_at,
                      fillcolor="red", opacity=0.15, line_width=0,
                      annotation_text=ev.severity)
        if ev.observed_value is not None:
            fig.add_trace(px.scatter(x=[ev.last_seen_at],
                                     y=[ev.observed_value]).data[0])
            fig.data[-1].marker = dict(color="red", size=11,
                                       symbol="x")
            fig.data[-1].name = "observed (peak)"
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, width="stretch")
        st.caption("Context comes directly from metric_samples; the shaded "
                   "band spans the grouped episode (first → last seen).")

    try:
        ids = json.loads(ev.source_samples_json or "[]")
    except json.JSONDecodeError:
        ids = []
    if ids:
        st.caption(f"Traceable source sample ids: {ids[:20]}"
                   + (" …" if len(ids) > 20 else ""))
