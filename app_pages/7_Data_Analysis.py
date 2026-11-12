"""Page 7 - Data Analysis (Phase 2 Scope C: EDA over stored measurements).

Everything on this page is computed from database queries through the
analytics modules - no demonstration values are charted. Demo and live
measurements are never mixed: one explicit source is selected. Charts
preserve sampling gaps, and distributions/correlations/comparisons only
appear when the data is statistically meaningful.
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from services.app_state import (bootstrap, demo_banner_if_active, read,
                                samples_to_frame, sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Data Analysis",
                  "Exploratory analysis over stored measurements", icon="🔬")
demo_banner_if_active()

from analytics import data_quality, eda
from analytics.derived_metrics import COUNTER_METRICS, RATE_OK, derive_for_counter_metric
from analytics.time_series import dedupe_frame, load_metric_frame
from config import get_settings
from database import repository
from database.models import db_utcnow
from ui import source_controls, time_controls

st.caption(
    "All timestamps are UTC. Units and the observed sampling interval are "
    "shown with every chart. Empty selections and insufficient data produce "
    "an explicit status instead of a chart."
)


def _series_for(f: pd.DataFrame) -> pd.Series:
    valid = f.dropna(subset=["value"])
    return pd.Series(valid["value"].to_numpy(),
                     index=pd.DatetimeIndex(valid["collected_at"]))


# ------------------------------------------------------------------ filters
avail = read(lambda s: repository.get_available_metrics(s))
if not avail:
    st.info("No measured values stored yet - run the native collector "
            "(sidebar: 'Collect now') or seed the labelled simulated source "
            "with `python tools/seed_simulated.py`.")
    st.stop()

sources = sorted({src for (_d, _m, _u, src) in avail})
data_source = source_controls.selector(
    sources, key="np_analysis_source", demo_mode=get_settings().demo_mode)
devices = sorted({d for (d, _m, _u, src) in avail if src == data_source})
device_name = st.selectbox("Device", devices)
opts = [(m, u) for (_d, m, u, src) in avail
        if src == data_source and _d == device_name and m is not None]
if not opts:
    st.warning(f"No stored values for device **{device_name}** under source "
               f"**{data_source}**.", icon="📭")
    st.stop()
labels = [f"{m} ({u or 'unit?'})" for m, u in opts]
idx = st.selectbox("Metric", range(len(opts)),
                   format_func=lambda i: labels[i])
metric, unit = opts[idx]

# Interface scope: only offer what actually exists for this selection. The
# full stored history is read once here so the scope list and the smart
# default range are based on what is actually stored, not a guess.
iface_rows = read(lambda s: repository.get_metric_series(
    s, device_name, metric, data_source=data_source,
    unit=unit, unit_is_null=unit is None))
iface_names = sorted({r.interface_name for r in iface_rows
                      if r.interface_name})
has_device_level = any(r.interface_name is None for r in iface_rows)
scope_options: list[str] = []
if has_device_level:
    scope_options.append("(device-level)")
scope_options.extend(iface_names)
scope = st.selectbox("Interface scope", scope_options or ["(none)"],
                     format_func=lambda v: v) if scope_options else None

if scope is None:
    st.info("Stored samples for this metric exist but carry no interface "
            "scope - nothing to select.")
    st.stop()

scope_iface = None if scope == "(device-level)" else scope
scope_rows = [r for r in iface_rows if r.interface_name == scope_iface]
earliest = min((r.collected_at for r in scope_rows
                if r.collected_at is not None), default=None)
latest = max((r.collected_at for r in scope_rows
              if r.collected_at is not None), default=None)

# Default to a window that actually contains the newest stored observation.
# A fixed 6-hour default used to hide series whose last sample was older,
# producing "0 non-null of 0 stored samples" even though data existed.
default_label = time_controls.smart_range_label(latest=latest)
time_controls.ensure_default(
    st.session_state, "np_analysis_range",
    (data_source, device_name, metric, unit, scope), default_label)
range_label = st.selectbox(
    "Time range", list(time_controls.RANGE_OPTIONS), key="np_analysis_range")
since = time_controls.since_for(range_label)

frame = read(lambda s: load_metric_frame(
    s, device_name, metric,
    interface_name=scope_iface,
    device_level_only=(scope == "(device-level)"),
    data_source=data_source, since=since))
frame = dedupe_frame(frame)
quality = data_quality.summarize_quality(
    frame, device_name, metric,
    interface_name=scope_iface,
    expected_interval_seconds=float(get_settings().collect_interval_seconds),
    now=db_utcnow())

st.caption(
    f"Source: **{data_source}** · range: **{range_label.lower()}** · "
    f"unit: **{unit or 'unknown'}** · observed median interval: "
    f"**{quality.observed_interval_seconds or 'n/a'} s** · "
    f"{quality.n_non_null} non-null of {quality.n_rows} stored samples"
    + (f" · {time_controls.describe_bounds(earliest, latest)}"
       if earliest is not None else "")
)

# ------------------------------------------------------------ raw time series
st.subheader(f"{device_name} · {scope} · {metric}"
             + (f" [{unit}]" if unit else ""))
if frame.empty:
    st.info("No samples in the selected range/source - widen the time range "
            "or wait for the next collection cycle. Nothing is invented.")
    if earliest is not None:
        st.caption("Stored history for this selection: "
                   + time_controls.describe_bounds(earliest, latest)
                   + f" · newest sample {latest:%Y-%m-%d %H:%M:%S} UTC.")
    if range_label != time_controls.ALL_HISTORY:
        st.button(
            "Show all stored history", key="np_analysis_all_history",
            on_click=time_controls.select_all_history,
            args=("np_analysis_range",))
elif quality.n_non_null == 0:
    st.info("Stored samples exist but every value is NULL (metric was not "
            "available at collection time). A missing value is not a zero, "
            "so nothing is plotted.")
else:
    chart = eda.insert_gap_breaks(frame)
    fig = px.line(chart, x="collected_at", y="value", markers=True,
                  labels={"value": f"{metric}{' [' + unit + ']' if unit else ''}",
                          "collected_at": "Time (UTC)"})
    fig.update_traces(connectgaps=False)  # gaps stay visible as breaks
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width="stretch")
    if quality.gap_count:
        st.caption(
            f"⏸ {quality.gap_count} sampling gap(s) > "
            f"{data_quality.GAP_FACTOR}x interval are drawn as line breaks, "
            "not connected through.")

# -------------------------------------------------------- summary statistics
st.subheader("Summary statistics")
stats = eda.summary_stats(frame)
if not stats["available"]:
    st.info(stats["reason"])
else:
    stats_tbl = pd.DataFrame([stats]).drop(columns=["available"]).T
    st.dataframe(stats_tbl.rename(columns={0: "value"})
                 .astype(str), width="stretch")
    if stats["unit"]:
        st.caption(f"Unit: **{stats['unit']}**")

c1, c2 = st.columns(2)
hist = eda.histogram(frame)
prof = eda.time_of_day_profile(frame)
dist_frame, dist_note = frame, None
# A narrow window can be legitimate but too sparse for a distribution or a
# diurnal profile. Fall back to the full stored history for THIS selection
# (clearly labelled) instead of reporting "skipped" merely because the
# default window was short. No value is invented or extended.
if range_label != time_controls.ALL_HISTORY and (
        not hist["available"] or not prof["available"]):
    full_frame = dedupe_frame(read(lambda s: load_metric_frame(
        s, device_name, metric, interface_name=scope_iface,
        device_level_only=(scope == "(device-level)"),
        data_source=data_source, since=None)))
    full_hist = eda.histogram(full_frame)
    full_prof = eda.time_of_day_profile(full_frame)
    if ((full_hist["available"] and not hist["available"])
            or (full_prof["available"] and not prof["available"])):
        dist_frame, hist, prof = full_frame, full_hist, full_prof
        dist_note = (
            f"The selected **{range_label.lower()}** window was too sparse for "
            "these views; they use all stored history for this selection "
            + time_controls.describe_bounds(earliest, latest) + ".")
if dist_note:
    st.caption(dist_note)

with c1:
    if hist["available"]:
        fig = px.histogram(x=dist_frame["value"].dropna(), nbins=20,
                           labels={"x": f"{metric}{' [' + unit + ']' if unit else ''}"})
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, width="stretch")
        st.caption(f"Distribution of {hist['n']} stored observation(s); "
                   "no values are synthesized.")
    else:
        st.info(f"Distribution skipped: {hist['reason']}.")
with c2:
    if prof["available"]:
        fig = px.bar(prof["profile"], x="utc_hour", y="mean",
                     labels={"utc_hour": "Hour (UTC)", "mean": "Mean value"},
                     text="count")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, width="stretch")
        st.caption("Bars labelled with the number of samples per hour.")
    else:
        st.info(f"Time-of-day profile skipped: {prof['reason']}.")

# ------------------------------------------------------- derived throughput
if metric in COUNTER_METRICS:
    st.subheader("Derived throughput (from cumulative counters)")
    der = derive_for_counter_metric(frame, metric)
    rates = der["rates"]
    ok = rates[rates["status"] == RATE_OK] if not rates.empty else rates
    if ok.empty:
        st.info("No defensible rate intervals in this selection "
                "(need at least two consecutive valid counter readings).")
    else:
        fig = px.line(ok, x="collected_at", y="value", markers=True,
                      labels={"value": f"{der['rate_metric']} [{der['unit']}]",
                              "collected_at": "Time (UTC) (interval end)"})
        fig.update_traces(connectgaps=False)
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, width="stretch")
        rejected = rates[rates["status"] != RATE_OK]
        st.caption(
            f"{len(ok)} usable interval(s); {len(rejected)} interval(s) "
            "excluded or flagged (counter resets/wraps, invalid elapsed "
            "time, oversized gaps). Rates are derived on demand - raw "
            "samples are never modified.")
        if not rejected.empty:
            with st.expander("Rejected / unreliable intervals"):
                st.dataframe(rejected, hide_index=True, width="stretch")

# ----------------------------------------------- interface/device comparison
if iface_names and len(iface_names) > 1:
    st.subheader(f"Comparison across interfaces ({metric})")
    cmp_frame = samples_to_frame(
        read(lambda s: repository.get_metric_series(
            s, device_name, metric, since=since, data_source=data_source)))
    cmp_res = eda.group_comparison(cmp_frame, by="interface")
    if cmp_res["available"]:
        st.dataframe(pd.DataFrame(cmp_res["rows"]),
                     hide_index=True, width="stretch")
        if cmp_res["insufficient_groups"]:
            st.caption("Not compared (too few values): "
                       + ", ".join(cmp_res["insufficient_groups"]))
    else:
        st.info(f"Comparison skipped: {cmp_res['reason']}.")

# ----------------------------------------------------------- correlations
with st.expander("Correlations with other metrics (same device & source)"):
    st.caption("Pearson correlation on time-aligned observations only; "
               f"pairs need >= {eda.MIN_CORR_POINTS} aligned points.")
    others = [m for m, _u in opts if m != metric]
    picks = st.multiselect("Compare with metric(s)", others, max_selections=3) \
        if others else []
    if picks:
        named = {metric: _series_for(frame)}
        for m2 in picks:
            f2 = dedupe_frame(read(lambda s, m2=m2: load_metric_frame(
                s, device_name, m2,
                interface_name=None if scope == "(device-level)" else scope,
                device_level_only=(scope == "(device-level)"),
                data_source=data_source, since=since)))
            named[m2] = _series_for(f2)
        for res in eda.pairwise_correlation(named):
            a, b = res["pair"]
            if res["available"]:
                st.markdown(f"**{a} ↔ {b}**: r = {res['r']:.3f} "
                            f"(n = {res['n']} aligned points, "
                            f"{res['method']})")
            else:
                st.markdown(f"**{a} ↔ {b}**: _not reported_ — {res['reason']}")

# --------------------------------------------------------- data-quality panel
with st.expander("Data-quality report (missing data & sampling gaps)"):
    st.dataframe(pd.DataFrame(eda.gap_summary(quality))
                 .astype({"value": str}), hide_index=True, width="stretch")
    ready_txt = ", ".join(
        f"{p}: {'✅' if ok else '❌'}"
        for p, ok in quality.ready_for.items())
    st.caption(f"Readiness gates — {ready_txt}")
    for r in quality.reasons:
        st.caption(f"• {r}")
    if not quality.reasons:
        st.caption("No quality issues detected for this selection.")
