"""Page 7 - read-only, service-backed Data Analysis.

One explicit source, one or more devices and one metric are selected; every
summary, chart, anomaly, gate and export is computed from that selection by
``services.data_analysis_service``. The page itself only renders widgets and
Plotly figures. Missing data and unsupported statistics are reported instead
of being filled with demonstration values.
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from services.app_state import (
    bootstrap,
    demo_banner_if_active,
    read,
    samples_to_frame,
    sidebar_status,
)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header(
    "Data Analysis",
    "Read-only exploratory analysis over stored measurements",
    icon="🔬",
)
demo_banner_if_active()

from analytics.derived_metrics import RATE_OK
from analytics.time_series import load_metric_frame
from config import get_settings
from database import repository
from database.models import db_utcnow
from services import data_analysis_service as das
from ui import source_controls, time_controls

CACHE_KWARGS = {"ttl": "1m", "max_entries": 64, "show_spinner": False}


@st.cache_data(**CACHE_KWARGS)
def _available_metrics() -> tuple[tuple[str, str | None, str | None, str], ...]:
    return tuple(read(lambda session: repository.get_available_metrics(session)))


@st.cache_data(**CACHE_KWARGS)
def _scope_rows(
    source: str,
    device: str,
    metric: str,
    unit: str | None,
    unit_is_null: bool,
) -> pd.DataFrame:
    rows = read(lambda session: repository.get_metric_series(
        session, device, metric, data_source=source,
        unit=unit, unit_is_null=unit_is_null))
    return samples_to_frame(rows)


@st.cache_data(**CACHE_KWARGS)
def _analysis(
    source: str,
    devices: tuple[str, ...],
    metric: str,
    unit: str | None,
    unit_is_null: bool,
    interface_name: str | None,
    device_level_only: bool,
    range_label: str,
    resample: str,
    related_metrics: tuple[str, ...],
) -> das.AnalysisResult:
    """Read the selected rows once and return a fully computed bundle."""
    primary = devices[0]
    since = time_controls.since_for(range_label)

    def series_frame(session, name: str, other_metric: str | None = None):
        return load_metric_frame(
            session, primary, other_metric or metric,
            interface_name=interface_name,
            device_level_only=device_level_only,
            data_source=source, since=since)

    frame = read(lambda session: series_frame(session, primary))
    comparison_rows = read(lambda session: repository.get_metric_series(
        session, primary, metric, since=since, data_source=source))
    device_rows: list[object] = []
    for device in devices:
        device_rows.extend(read(lambda session, d=device: repository.get_metric_series(
            session, d, metric, since=since, data_source=source,
            unit=unit, unit_is_null=unit_is_null)))
    related: dict[str, pd.DataFrame] = {}
    for other in related_metrics:
        related[other] = read(
            lambda session, m=other: series_frame(session, primary, m))

    return das.analyze(
        frame=frame,
        device_name=primary,
        metric=metric,
        interface_name=interface_name,
        data_source=source,
        unit=unit,
        expected_interval_seconds=float(get_settings().collect_interval_seconds),
        now=db_utcnow(),
        resample=resample,
        comparison_frame=samples_to_frame(comparison_rows),
        device_frame=samples_to_frame(device_rows),
        related_frames=related,
    )


st.caption(
    "All timestamps are UTC. Source, scope and sampling interval are shown "
    "with every result; sparse or empty selections report the missing "
    "precondition instead of displaying a placeholder. Nothing is written."
)

available = _available_metrics()
if not available:
    st.info("No measurements are stored yet. Collect data or load sample data to use Data Analysis.")
    with st.expander("Technical details", expanded=False):
        st.markdown("- **Developer command:** `python tools/seed_simulated.py`")
    st.stop()

sources = sorted({row[3] for row in available})
data_source = source_controls.selector(
    sources, key="np_analysis_source", demo_mode=get_settings().demo_mode)
device_options = sorted({row[0] for row in available if row[3] == data_source})
selected_devices = st.multiselect(
    "Devices", device_options, default=device_options[:1],
    key="np_analysis_devices")
if not selected_devices:
    st.info("Select at least one device to analyse. No data is loaded while "
            "the selection is empty.")
    st.stop()
primary_device = selected_devices[0]

metric_options = sorted({
    (row[1], row[2]) for row in available
    if row[3] == data_source and row[0] in selected_devices and row[1]
})
if not metric_options:
    st.warning(f"No stored metric values for **{primary_device}** under source "
               f"**{data_source}**.", icon="📭")
    st.stop()
metric_index = st.selectbox(
    "Metric", range(len(metric_options)), key="np_analysis_metric",
    format_func=lambda index: (
        f"{metric_options[index][0]} ({metric_options[index][1] or 'unit?'})"))
metric, unit = metric_options[metric_index]
unit_is_null = unit is None

scope_frame = _scope_rows(
    data_source, primary_device, metric, unit, unit_is_null)
interface_names = sorted({
    str(value) for value in scope_frame["interface"].dropna().unique()
}) if "interface" in scope_frame else []
has_device_level = (
    "interface" in scope_frame and scope_frame["interface"].isna().any())
scope_options: list[str] = []
if has_device_level:
    scope_options.append("(device-level)")
scope_options.extend(interface_names)
scope = st.selectbox("Interface scope", scope_options or ["(none)"],
                     key="np_analysis_scope")
if not scope_options:
    st.info("Stored samples for this metric exist but carry no usable "
            "interface scope - nothing to select.")
    st.stop()
interface_name = None if scope == "(device-level)" else scope
device_level_only = scope == "(device-level)"

timestamps = pd.to_datetime(scope_frame["collected_at"], errors="coerce").dropna()
earliest = timestamps.min() if not timestamps.empty else None
latest = timestamps.max() if not timestamps.empty else None
default_label = time_controls.smart_range_label(latest=latest)
time_controls.ensure_default(
    st.session_state, "np_analysis_range",
    (data_source, tuple(selected_devices), metric, unit, scope), default_label)
range_label = st.selectbox(
    "Time range", list(time_controls.RANGE_OPTIONS), key="np_analysis_range")
resample = st.selectbox(
    "Resample", list(das.RESAMPLE_OPTIONS), key="np_analysis_resample",
    help="Charts use this step; quality, summaries and anomalies use the full "
         "prepared series.")

with st.expander("Correlation options", expanded=False):
    st.caption(f"Pearson correlation needs at least {das.MIN_CORR_POINTS} "
               "exactly aligned observations.")
    other_metrics = sorted({name for name, _u in metric_options if name != metric})
    related_metrics = st.multiselect(
        "Compare with metric(s)", other_metrics, max_selections=3,
        key="np_analysis_related")

results = st.container()
with st.spinner("Computing analysis…", show_time=True):
    result = _analysis(
        data_source, tuple(selected_devices), metric, unit, unit_is_null,
        interface_name, device_level_only, range_label, resample,
        tuple(related_metrics))

with results:
    st.subheader(f"{primary_device} · {scope} · {metric}"
                 + (f" [{unit}]" if unit else ""))
    prep = result.prepared.info
    st.caption(
        f"Source **{data_source}** · range **{range_label.lower()}** · "
        f"resample **{resample}** · {prep['input_rows']} stored rows, "
        f"{prep['computed_rows']} prepared, {prep['plot_rows']} plotted"
        + (" (bounded downsample; step "
           f"{prep['downsample_step']})." if prep["downsampled"] else ".")
        + (f" Observed median interval: "
           f"{result.quality.observed_interval_seconds or 'n/a'} s."
           if result.quality.observed_interval_seconds else "")
        + (f" Stored bounds: {time_controls.describe_bounds(earliest, latest)}."
           if earliest is not None else "")
    )

    with st.container(horizontal=True):
        st.metric("Non-null samples", f"{result.quality.n_non_null}"
                  f"/{result.quality.n_rows}", border=True)
        st.metric("Sampling gaps", result.quality.gap_count, border=True)
        st.metric("Completeness",
                  "n/a" if result.quality.completeness_pct is None
                  else f"{result.quality.completeness_pct:.1f}%", border=True)
        st.metric("Detector points", len(result.anomalies), border=True)

    if prep["computed_rows"] == 0:
        st.info("No samples in the selected range and source. Widen the time "
                "range or wait for the next collection cycle; nothing is "
                "invented.")
        if earliest is not None:
            st.caption("Stored history: "
                       + time_controls.describe_bounds(earliest, latest))
        if range_label != time_controls.ALL_HISTORY:
            st.button("Show all stored history", key="np_analysis_all_history",
                      on_click=time_controls.select_all_history,
                      args=("np_analysis_range",))
    elif result.quality.n_non_null == 0:
        st.info("Stored rows exist but every value is NULL (the metric was not "
                "available when collected). A missing value is never treated "
                "as zero.")
    else:
        line = px.line(
            das.chart_series(result), x="collected_at", y="value", markers=True,
            labels={"value": f"{metric}{' [' + unit + ']' if unit else ''}",
                    "collected_at": "Time (UTC)"})
        line.update_traces(connectgaps=False)
        if not result.anomalies.empty:
            line.add_trace(go.Scatter(
                x=result.anomalies["timestamp"],
                y=result.anomalies["observed"], mode="markers",
                marker={"color": "#ff4b4b", "size": 9, "symbol": "diamond"},
                name="Detector point"))
        line.update_layout(height=380, margin={"l": 10, "r": 10, "t": 30, "b": 10})
        st.plotly_chart(
            line, width="stretch",
            alt=f"{metric} over time for {primary_device} with sampling gaps "
                "left as breaks")
        if result.quality.gap_count:
            st.caption(f"{result.quality.gap_count} sampling gap(s) larger than "
                       "3x the interval are drawn as line breaks.")

        st.subheader("Summary statistics")
        st.dataframe(das.summary_table(result), hide_index=True,
                     width="stretch",
                     alt="Summary statistics for the selected series")

        with st.container(horizontal=True):
            with st.container(border=True):
                st.markdown("**Distribution**")
                histogram = result.histogram
                if histogram.get("available"):
                    edges = list(histogram["edges"])
                    centers = [(edges[i] + edges[i + 1]) / 2
                               for i in range(len(edges) - 1)]
                    fig = px.bar(
                        x=centers, y=histogram["counts"],
                        labels={"x": metric, "y": "Count"})
                    fig.update_layout(height=280,
                                      margin={"l": 10, "r": 10, "t": 20, "b": 10})
                    st.plotly_chart(
                        fig, width="stretch",
                        alt=f"Distribution of {histogram['n']} stored values")
                    st.caption(f"{histogram['n']} stored observation(s); no "
                               "values are synthesized.")
                else:
                    st.info(f"Distribution skipped: {histogram['reason']}.")
            with st.container(border=True):
                st.markdown("**Time-of-day profile**")
                profile = result.profile
                if profile.get("available"):
                    fig = px.bar(
                        profile["profile"], x="utc_hour", y="mean",
                        text="count",
                        labels={"utc_hour": "Hour (UTC)", "mean": "Mean value"})
                    fig.update_layout(height=280,
                                      margin={"l": 10, "r": 10, "t": 20, "b": 10})
                    st.plotly_chart(
                        fig, width="stretch",
                        alt="Mean value by UTC hour with sample counts")
                else:
                    st.info(f"Time-of-day profile skipped: {profile['reason']}.")

        st.subheader("Rolling band and anomalies")
        if result.band.empty:
            st.info("Rolling band skipped: too few prepared samples for a "
                    "defensible trailing estimate.")
        else:
            band = result.band
            figure = go.Figure()
            figure.add_trace(go.Scatter(
                x=band["collected_at"], y=band["value"],
                mode="lines", name="Observed"))
            figure.add_trace(go.Scatter(
                x=band["collected_at"], y=band["upper"],
                mode="lines", name="Upper 2 sigma",
                line={"width": 1, "dash": "dot"}))
            figure.add_trace(go.Scatter(
                x=band["collected_at"], y=band["lower"],
                mode="lines", name="Lower 2 sigma", fill="tonexty",
                fillcolor="rgba(75, 112, 255, 0.15)",
                line={"width": 1, "dash": "dot"}))
            figure.update_layout(
                height=320, margin={"l": 10, "r": 10, "t": 20, "b": 10})
            st.plotly_chart(
                figure, width="stretch",
                alt="Observed values inside a trailing mean plus or minus two "
                    "standard deviations band")
        for note in result.anomaly_notes:
            st.caption(note)
        if not result.anomalies.empty:
            st.dataframe(result.anomalies, hide_index=True, width="stretch",
                         alt="Detector points with baselines and explanations")

        counter = result.counter
        if counter is not None:
            st.subheader("Derived throughput")
            if counter.get("unsupported"):
                st.info(str(counter.get("reason", "No rate can be derived.")))
            else:
                rates = counter["rates"]
                usable = rates[rates["status"] == RATE_OK]
                rejected = rates[rates["status"] != RATE_OK]
                if usable.empty:
                    st.info("No defensible rate intervals in this selection "
                            "(need at least two consecutive valid counter "
                            "readings).")
                else:
                    fig = px.line(
                        usable, x="collected_at", y="value", markers=True,
                        labels={"value": f"{counter['rate_metric']} "
                                         f"[{counter['unit']}]",
                                "collected_at": "Time (UTC) (interval end)"})
                    fig.update_layout(height=320,
                                      margin={"l": 10, "r": 10, "t": 20, "b": 10})
                    st.plotly_chart(
                        fig, width="stretch",
                        alt=f"Derived {counter['rate_metric']} from cumulative "
                            "counters")
                    st.caption(f"{len(usable)} usable interval(s); "
                               f"{len(rejected)} excluded or flagged. Raw "
                               "samples are never modified.")
                    if not rejected.empty:
                        with st.expander("Rejected / unreliable intervals"):
                            st.dataframe(rejected, hide_index=True,
                                         width="stretch",
                                         alt="Rejected counter intervals")

    for title, key in ((f"Across interfaces ({metric})", "comparison"),
                       (f"Across selected devices ({metric})",
                        "device_comparison")):
        comparison = getattr(result, key)
        if not comparison:
            continue
        st.subheader(title)
        if comparison.get("available"):
            rows = pd.DataFrame(comparison["rows"])
            st.dataframe(rows, hide_index=True, width="stretch",
                         alt=f"Group comparison of {metric}")
            if comparison.get("insufficient_groups"):
                st.caption("Not compared (too few values): "
                           + ", ".join(comparison["insufficient_groups"]))
        else:
            st.info(f"Comparison skipped: {comparison['reason']}.")

    with st.expander("Correlations with other metrics (same device and source)"):
        if not related_metrics:
            st.info("Select another metric above to compute aligned Pearson "
                    "correlations. No correlation is inferred by default.")
        else:
            for row in result.correlations:
                first, second = row["pair"]
                if row.get("available"):
                    st.markdown(f"**{first} ↔ {second}**: r = "
                                f"{row['r']:.3f} (n = {row['n']} aligned "
                                f"points, {row['method']})")
                else:
                    st.markdown(f"**{first} ↔ {second}**: _not reported_ — "
                                f"{row['reason']}")

    with st.expander("Data-quality report"):
        st.dataframe(das.quality_table(result), hide_index=True,
                     width="stretch", alt="Sampling and missingness summary")
        ready = ", ".join(
            f"{name}: {'pass' if ok else 'blocked'}"
            for name, ok in result.quality.ready_for.items())
        st.caption(f"Readiness gates — {ready}")
        if result.quality.reasons:
            for reason in result.quality.reasons:
                st.caption(f"• {reason}")
        else:
            st.caption("No quality issues detected for this selection.")

    st.subheader("CSV exports")
    exports = das.export_frames(result)
    export_columns = st.columns(min(4, len(exports)))
    for index, (name, frame) in enumerate(exports.items()):
        export_columns[index % len(export_columns)].download_button(
            name.replace(".csv", "").replace("_", " ").title(),
            data=frame.to_csv(index=False).encode("utf-8"),
            file_name=f"{primary_device}_{metric}_{name}",
            mime="text/csv", key=f"np_analysis_export_{index}")
