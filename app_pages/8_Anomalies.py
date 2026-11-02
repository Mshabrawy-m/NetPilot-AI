"""Page 8 - Anomalies (explainable detection events, batch scan, calibration).

Events are produced by documented detectors and persisted by
``services.anomaly_service``. Six detectors are available; the always-on pair
stays rolling MAD + rolling z-score, and EWMA, CUSUM change-point and
seasonal-residual are chosen explicitly, so a deployment is never silently
widened under someone's feet.

An anomaly is a *statistical deviation worth investigating* - this page never
claims an attack or a confirmed failure, and demo events are always labelled and
filtered separately from live lab measurements. A detector that cannot form an
opinion on a series (for example the seasonal one without a known sampling
interval) is reported as an abstention, never as "nothing found".
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import plotly.express as px
import streamlit as st

from services.app_state import (bootstrap, demo_banner_if_active, flash, read,
                                render_flash, sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Anomalies",
                  "Explainable, persisted detection events", icon="🚨")
demo_banner_if_active()
render_flash()

from analytics import data_quality
from analytics.anomaly_detection import (
    DEFAULT_DETECTORS, DETECTORS)
from analytics.time_series import dedupe_frame, load_metric_frame
from config import get_settings
from database import repository
from services import anomaly_service

st.caption(
    "Anomaly events are explainable statistical deviations, not confirmed "
    "attacks or failures. Detection runs only when you press a button below - "
    "never automatically on every page refresh.")

DETECTOR_HELP = {
    "rolling_mad": "robust trailing median + MAD (baseline)",
    "rolling_zscore": "trailing mean/std z-score (baseline)",
    "ewma": "exponentially weighted past - catches a slow creep",
    "cusum": "two-sided cumulative-sum change point - catches a sustained "
             "small shift a single-point test misses",
    "seasonal_residual": "compares a sample with the same phase of past "
                         "cycles (needs a known sampling interval)",
    "isolation_forest": "optional seeded ML detector - exploratory only",
}


def _fmt_duration(seconds) -> str:
    if seconds is None:
        return "n/a"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


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

settings = get_settings()

# ------------------------------------------------- store KPIs (read-only view)
summary = read(lambda s: anomaly_service.event_summary(s,
                                                       data_source=data_source))
theme.kpi_row([
    ("Open events", summary["open"], f"source: {data_source}",
     "warn" if summary["open"] else "ok"),
    ("Critical open", summary["critical_open"],
     "escalated by a stronger observation",
     "bad" if summary["critical_open"] else "muted"),
    ("Resolved", summary["resolved"],
     "only a real normal observation resolves", "ok"),
    ("Grouped occurrences", summary["total_occurrences"],
     f"across {summary['n_events']} event(s)", "info"),
    ("Median episode", _fmt_duration(summary["median_episode_seconds"]),
     "first seen → last seen", "violet"),
    ("Newest open", (summary["newest_open_at"].strftime("%m-%d %H:%M UTC")
                     if summary["newest_open_at"] else "none"),
     "last anomaly evidence", "default"),
])
if summary["truncated"]:
    st.caption("Event aggregates cover the most recent 1000 events only "
               "(bounded read).")

st.divider()

# ---------------------------------------------------------- detection controls
fcol1, fcol2 = st.columns([1, 1])
with fcol1:
    run_device = st.selectbox(
        "Device (detection target)",
        sorted({d for (d, _m, _u, s) in avail if s == data_source}))
    metrics = sorted({m for (d, m, _u, s) in avail
                      if s == data_source and d == run_device and m})
    run_metric = st.selectbox("Metric", metrics or ["(none)"])
with fcol2:
    st.markdown("**Detectors**")
    chosen_detectors = st.multiselect(
        "Detectors to run", list(DETECTORS),
        default=list(DEFAULT_DETECTORS),
        label_visibility="collapsed",
        help=" · ".join(f"{k}: {v}" for k, v in DETECTOR_HELP.items()))
    with st.expander("Sensitivity (defaults come from config)"):
        det_window = st.slider("Strictly-past window (samples)", 8, 120,
                               int(settings.detector_window))
        mad_thr = st.slider("Rolling MAD threshold (robust scales)",
                            1.5, 6.0, float(settings.mad_threshold), 0.1)
        z_thr = st.slider("Z-score threshold (σ)", 1.5, 6.0,
                          float(settings.zscore_threshold), 0.1)
        ewma_thr = st.slider("EWMA threshold (robust scales)", 1.5, 6.0,
                             float(settings.ewma_threshold), 0.1)
        cusum_k = st.slider("CUSUM drift allowance k (σ)", 0.1, 2.0,
                            float(settings.cusum_drift_k), 0.05)
        cusum_h = st.slider("CUSUM decision boundary h (σ)", 1.0, 10.0,
                            float(settings.cusum_threshold_h), 0.5)
        st.caption("Smaller thresholds / smaller h react sooner and flag more "
                   "often. Every value used is snapshotted inside the stored "
                   "event, so an old event can always be re-explained.")

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
        result = None
        if not options:
            st.warning("No stored rows for this metric/source selection - "
                       "nothing to detect on.")
        else:
            # ``options`` may legitimately contain ``None`` (the
            # "(device-level)" scope). Compare the *retrieved* value against
            # it rather subscripting the key directly: a missing key
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
            with st.spinner("Detecting..."):
                result = read(lambda s: anomaly_service.run_detection(
                    s, device_name=run_device, metric=run_metric,
                    interface_name=target_iface, data_source=data_source,
                    detectors=tuple(chosen_detectors) or None,
                    window=det_window, mad_threshold=mad_thr,
                    zscore_threshold=z_thr, ewma_threshold=ewma_thr,
                    cusum_drift=cusum_k, cusum_limit=cusum_h,
                    device_level_only=(target_iface is None)))
        if result is not None:
            # Persist nothing here: the counts are already in the store. Queue
            # a message and rerun so the KPI row at the top of the page (which
            # is computed before this button is handled) shows the new state
            # instead of contradicting the result printed below it.
            st.session_state["_anom_result"] = result
            if result["status"] == "insufficient_data":
                flash("Insufficient data for detection: "
                      + "; ".join(result["reasons"]), level="warning")
            elif result["status"] == "unknown_detector":
                flash("; ".join(result["reasons"]), level="error")
            else:
                p = result["persisted"]
                flash(
                    f"Detection finished: {result['n_anomalies']} anomalous "
                    f"point(s) on {result['n_series_points']} samples → "
                    f"{p['created']} new event(s), {p['updated']} grouped "
                    f"update(s), {p['skipped']} suppressed, "
                    f"{result['resolved']} event(s) resolved by normal "
                    "observations.")
            st.rerun()

# Detail of the run that just finished, shown once (the headline counts are
# the flash message above). Popping keeps it transient: a later rerun or a
# filter change does not resurrect an old run's detector table.
last_result = st.session_state.pop("_anom_result", None)
if last_result and last_result.get("status") == "ok":
    if last_result.get("by_detector"):
        st.dataframe(pd.DataFrame(
            [{"detector": k, "points flagged": v}
             for k, v in sorted(last_result["by_detector"].items())]),
            hide_index=True, width="stretch")
    for note in last_result.get("notes") or []:
        st.info(note, icon="🤷")

# ------------------------------------------------------------ batch scanning
st.markdown("**Batch scan** — one bounded pass over the stored series of this "
            "source (device-level series, each still gated individually).")
if st.button(f"📡 Scan all ready series (max "
             f"{settings.scan_max_series})", width="stretch",
             key="run_batch_scan"):
    with st.spinner("Scanning..."):
        scan = read(lambda s: anomaly_service.scan_ready_series(
            s, data_source=data_source,
            detectors=tuple(chosen_detectors) or None,
            window=det_window))
    st.session_state["_anom_scan"] = scan
    t = scan["totals"]
    flash(f"Scanned {scan['scanned']} of {scan['candidates']} ready series "
          f"(cap {scan['limit']}) → {t['points']} point(s), "
          f"{t['created']} new event(s), {t['updated']} grouped update(s), "
          f"{t['resolved']} resolved.")
    # Rerun for the same reason as single detection: the KPI row is rendered
    # before this button is handled, so it would still show pre-scan counts.
    st.rerun()
scan = st.session_state.get("_anom_scan")
if scan:
    if scan["capped"]:
        st.warning(f"More series were ready than one click may evaluate: "
                   f"{scan['candidates'] - scan['limit']} were NOT scanned "
                   f"(NETPILOT_SCAN_MAX_SERIES = {scan['limit']}).",
                   icon="🛑")
    if scan["results"]:
        st.dataframe(pd.DataFrame([{
            "series": r["series"], "samples": r["n_samples"],
            "points": r["n_points"],
            "detectors": ", ".join(f"{k}:{v}" for k, v
                                   in sorted(r["by_detector"].items()))
            or "-",
            "new events": r["persisted"].get("created"),
            "grouped": r["persisted"].get("updated"),
            "resolved": r["resolved"],
        } for r in scan["results"]]), hide_index=True, width="stretch")
    for note in scan["notes"]:
        st.caption(f"ℹ️ {note}")
    if scan["skipped"]:
        st.caption("Skipped (not detection-ready, nothing was judged): "
                   + "; ".join(f"{s['series']} ({', '.join(s['reasons'])})"
                               for s in scan["skipped"][:6]))

st.divider()

# ---------------------------------------------------------------- event store
if summary["n_events"]:
    with st.expander("Event distribution by detector and metric",
                     expanded=False):
        dcol1, dcol2 = st.columns(2)
        det_rows = [{"detector": k, "open": v["open"],
                     "events": v["events"], "occurrences": v["occurrences"]}
                    for k, v in sorted(summary["by_detector"].items())]
        met_rows = [{"metric": k, "open": v["open"], "events": v["events"],
                     "occurrences": v["occurrences"]}
                    for k, v in sorted(summary["by_metric"].items(),
                                       key=lambda kv: -kv[1]["events"])[:10]]
        with dcol1:
            df_det = pd.DataFrame(det_rows).melt(
                id_vars="detector", value_vars=["open", "events"],
                var_name="state", value_name="count")
            st.plotly_chart(px.bar(df_det, x="detector", y="count",
                                   color="state", barmode="group",
                                   title="Events per detector"),
                            width="stretch", height=300)
        with dcol2:
            df_met = pd.DataFrame(met_rows).melt(
                id_vars="metric", value_vars=["open", "events"],
                var_name="state", value_name="count")
            st.plotly_chart(px.bar(df_met, y="metric", x="count",
                                   color="state", barmode="group",
                                   orientation="h",
                                   title="Most eventful metrics"),
                            width="stretch", height=300)
        st.caption("Counts are grouped EVENTS, not raw points - a continuing "
                   "episode stays one event until a normal observation "
                   "resolves it.")

lcol1, lcol2, lcol3, lcol4 = st.columns([1, 1, 1, 1])
with lcol1:
    status_filter = st.selectbox("Status", ["open", "resolved", "all"])
with lcol2:
    sev_filter = st.selectbox("Severity", ["all", "critical", "warning"])
with lcol3:
    dev_filter = st.selectbox(
        "Device filter", ["all"] + sorted({e.device_name for e in read(
            lambda s: repository.get_anomaly_events(s))}))
with lcol4:
    det_filter = st.selectbox(
        "Detector filter", ["all"] + sorted(DETECTORS))

events = read(lambda s: anomaly_service.list_events(
    s, data_source=data_source,
    status=None if status_filter == "all" else status_filter,
    device_name=None if dev_filter == "all" else dev_filter,
    detector=None if det_filter == "all" else det_filter))
if sev_filter != "all":
    events = [e for e in events if e.severity == sev_filter]

st.subheader(f"Anomaly events — source: {data_source} "
             f"({len(events)} shown)")
if not events:
    st.info("No persisted anomaly events match these filters for this "
            "data source. Run detection above, or widen the filters.")
else:
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

    # ------------------------------------------------ selected event detail
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
        f"({ev.occurrences} occurrence(s), "
        f"{_fmt_duration((ev.last_seen_at - ev.first_seen_at).total_seconds())}"
        " long)"
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
            fig.add_vrect(x0=ev.first_seen_at, x1=ev.last_seen_at,
                          fillcolor="red", opacity=0.15, line_width=0,
                          annotation_text=ev.severity)
            if ev.baseline_value is not None:
                fig.add_hline(y=ev.baseline_value, line_dash="dot",
                              annotation_text="detector baseline")
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

# ------------------------------------------------------------- calibration
st.divider()
with st.expander("🧪 Calibrate detector sensitivity against your own labels"):
    st.caption(
        "Precision/recall only exist relative to labels. **No live network "
        "provides them**, so the labels here are the incident windows YOU "
        "mark (or a documented synthetic fixture in the test suite). The "
        "result is a comparison of sensitivities on those labels - never a "
        "claim about real-world accuracy.")
    if not run_metric:
        st.info("Select a device and metric above first.")
    else:
        cal_frame, cal_quality = read(
            lambda s: anomaly_service.load_ready_series(
                s, run_device, run_metric, None, data_source,
                device_level_only=True))
        if not cal_quality.ready(data_quality.DETECTION):
            st.warning("This series is not detection-ready: "
                       + "; ".join(
                           cal_quality.reasons_for(data_quality.DETECTION)
                           or cal_quality.reasons), icon="📉")
        else:
            ccol1, ccol2 = st.columns([1, 1])
            with ccol1:
                label_dates = st.date_input(
                    "Incident day(s) you label as true anomalies (UTC)",
                    value=(),
                    help="Every stored sample inside the marked range counts "
                         "as a true anomaly. Points outside it are treated "
                         "as normal, so an unfinished label set understates "
                         "recall.")
            with ccol2:
                cal_detectors = st.multiselect(
                    "Detectors to compare",
                    [d for d in DETECTORS if d != "isolation_forest"],
                    default=list(DEFAULT_DETECTORS) + ["ewma"],
                    help="isolation_forest has no single sensitivity knob to "
                         "calibrate, so it is not offered.")
                cal_grid = st.multiselect(
                    "Sensitivity grid", [2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
                    default=[2.0, 2.5, 3.0, 3.5, 4.0])
            if st.button("📐 Score sensitivities", key="run_calibration"):
                if len(label_dates) < 1 or not cal_detectors or not cal_grid:
                    st.warning("Pick at least one labelled day, one detector "
                               "and one grid value.")
                else:
                    start = dt.datetime.combine(min(label_dates), dt.time.min)
                    end = dt.datetime.combine(max(label_dates), dt.time.max)
                    labels = [ts for ts in cal_frame["collected_at"]
                              if start <= ts.to_pydatetime() <= end]
                    if not labels:
                        st.info("The marked window contains no samples of "
                                "this series - there is nothing to label, so "
                                "no score is produced.")
                    else:
                        rows = anomaly_service.calibrate_detectors(
                            cal_frame, device_name=run_device,
                            metric=run_metric, interface_name=None,
                            labelled_anomalies=labels,
                            detectors=tuple(cal_detectors),
                            thresholds=tuple(cal_grid),
                            window=det_window,
                            interval_seconds=cal_quality
                            .observed_interval_seconds,
                            season_hours=settings.season_hours)
                        scored = [r for r in rows if r.get("scoreable")]
                        st.caption(
                            f"{len(labels)} labelled sample(s) out of "
                            f"{int(cal_frame['value'].notna().sum())} - "
                            "labels supplied by the operator, not ground truth.")
                        if scored:
                            st.dataframe(pd.DataFrame([{
                                "detector": r["detector"],
                                "threshold": r["threshold"],
                                "TP": r["true_positives"],
                                "FP": r["false_positives"],
                                "FN": r["false_negatives"],
                                "precision": r["precision"],
                                "recall": r["recall"],
                                "F1": r["f1"],
                                "false alarm rate": r["false_alarm_rate"],
                                "flagged": r["n_flagged"],
                            } for r in sorted(scored, key=lambda r: (
                                r.get("rank") is None,
                                r.get("rank") or 0))]),
                                hide_index=True, width="stretch")
                            best = next((r for r in sorted(
                                scored, key=lambda r: (r.get("rank") is None,
                                                       r.get("rank") or 0))
                                if r.get("f1") is not None), None)
                            if best:
                                st.success(
                                    f"Best on these labels: `{best['detector']}` "
                                    f"at threshold {best['threshold']:g} "
                                    f"(F1 {best['f1']:.3f}, recall "
                                    f"{best['recall']:.3f}, "
                                    f"{best['false_positives']} false "
                                    "alarm(s)).")
                        for r in rows:
                            if not r.get("scoreable"):
                                st.info(f"`{r['detector']}`: {r['note']}",
                                        icon="🤷")
