"""Page 5 - Metrics & History (time-series charts from stored measurements)."""
from __future__ import annotations

import statistics
from datetime import timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from services.app_state import bootstrap, demo_banner_if_active, read, samples_to_frame, sidebar_status

from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Metrics & History",
                  "Time-series charts from stored measurements", icon="📈")
demo_banner_if_active()

from database import repository
from database.models import db_utcnow

st.caption(
    "All timestamps are UTC. Only metrics with actual stored values can be "
    "selected - metrics with no data are not offered or plotted. Null "
    "samples (metric unavailable at that time) appear as gaps."
)

devices = read(lambda s: list(repository.get_devices(s)))
if not devices:
    st.info("No devices yet - collect data first.")
    st.stop()

device_name = st.selectbox("Device", [d.name for d in devices])
available = read(lambda s: repository.get_available_metrics(s, device_name))
# Drop rows for other devices (query is already filtered, keep tuples tidy).
options = [(m, u, src) for (dev, m, u, src) in available if dev == device_name]

if not options:
    st.warning(
        f"No measured values exist for device **{device_name}** yet. "
        "Nothing is plotted for metrics without real data.", icon="📭")
    st.stop()

labels = [f"{m} ({u or 'unit?'}) [{src}]" for m, u, src in options]
choice_idx = st.selectbox("Metric (measured values only)", range(len(options)),
                          format_func=lambda i: labels[i])
metric, unit, src = options[choice_idx]

range_options = {"1 hour": 1, "6 hours": 6, "24 hours": 24, "7 days": 168}
range_label = st.selectbox("Time range", list(range_options), index=1)
hours = range_options[range_label]

# Optional interface filter when the metric is per-interface.
iface_names = read(lambda s: sorted({
        i.interface_name for i in repository.get_metric_series(
            s, device_name, metric, data_source=src)
    if i.interface_name
}))
iface = st.selectbox("Interface", [None] + iface_names,
                     format_func=lambda v: v or "(device-level)") \
    if iface_names else None

since = db_utcnow() - timedelta(hours=hours)
samples = read(lambda s: repository.get_metric_series(
    s, device_name, metric, since=since, interface_name=iface,
    data_source=src))
frame = samples_to_frame(samples)

st.subheader(f"{device_name} · {metric}"
             + (f" · {iface}" if iface else "")
             + (f" [{unit}]" if unit else ""))
st.caption(f"Data source: **{src}** · collected {len(frame)} samples "
           f"in the last {range_label}")

if frame.empty:
    st.info("No samples in the selected time range - widen the range or "
            "wait for the next collection cycle.")
elif frame["value"].dropna().empty:
    st.info("Stored samples exist but all values are null (metric was not "
            "available). Nothing is plotted - values are never invented.")
else:
    valid = frame.dropna(subset=["value"]).sort_values("collected_at")
    values = valid["value"].astype(float).tolist()
    latest = values[-1]
    p95 = float(valid["value"].quantile(0.95))
    summary = st.columns(4)
    summary[0].metric("Latest", f"{latest:,.4g} {unit or ''}".strip())
    summary[1].metric("Median", f"{statistics.median(values):,.4g} {unit or ''}".strip())
    summary[2].metric("95th percentile", f"{p95:,.4g} {unit or ''}".strip())
    summary[3].metric("Unavailable samples", int(frame["value"].isna().sum()))

    if len(values) >= 6:
        half = max(3, len(values) // 2)
        earlier = statistics.median(values[:len(values) - half])
        recent = statistics.median(values[-half:])
        delta = recent - earlier
        change = (f"{delta:+,.4g} {unit or ''}".strip())
        if earlier != 0:
            change += f" ({delta / abs(earlier) * 100:+.1f}%)"
        if delta == 0:
            trend_summary = "The recent-half median matches the earlier-half median."
        else:
            direction = "higher" if delta > 0 else "lower"
            trend_summary = (
                f"The recent-half median is {direction} than the earlier-half "
                f"median by {change}."
            )
        st.info(f"{trend_summary} This describes the selected samples; it does "
                "not establish a cause.")
    elif len(values) > 1:
        st.caption("Trend comparison needs at least six non-null samples; "
                   "widen the time range to see a recent-versus-earlier comparison.")

    if frame["value"].isna().any():
        st.warning("Some timestamps have unavailable values. They are shown as "
                   "gaps and excluded from summary statistics.")

    if st.button("Investigate this series with the Network Assistant", type="primary"):
        start = valid["collected_at"].iloc[0]
        end = valid["collected_at"].iloc[-1]
        trend_note = ""
        if len(values) >= 6:
            trend_note = f" The recent-half median changed by {change}."
        st.session_state["np_ai_request_draft"] = (
            f"Investigate the observed {metric} series on {device_name}"
            f"{f' interface {iface}' if iface else ''} from {src} data, "
            f"covering {start} through {end} UTC. The selected series has "
            f"{len(values)} non-null readings and {int(frame['value'].isna().sum())} "
            f"unavailable readings.{trend_note} Explain what the stored evidence "
            "supports, disclose missing or stale evidence, and suggest read-only "
            "checks only. Do not propose or apply a network change."
        )
        st.switch_page("pages/10_AI_Network_Assistant.py")

    fig = px.line(valid, x="collected_at",
                  y="value", markers=True,
                  labels={"value": f"{metric}{' [' + unit + ']' if unit else ''}",
                          "collected_at": "Time (UTC)"})
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width="stretch")

with st.expander("Raw samples"):
    st.dataframe(frame, hide_index=True, width="stretch")
