"""AI Lab - reproducible experiments on real and simulated telemetry.

This page is an experimentation platform, not another dashboard: everything it
shows is computed by the run you just made, and everything it refuses to show
is refused out loud with the reason recorded.

The flow is fixed: **select data source -> inspect data -> select task ->
configure model -> run experiment -> evaluate results -> export report**.

Four source types feed one validated representation
(``analytics.lab_dataset.LabDataset``):

* **simulated** - the built-in generator, with injected fault events and
  ground-truth labels, bounded and reproducible from its seed;
* **project history / uploaded CSV / allowlisted local file** - already
  recorded or operator-supplied data, imported with explicit
  rejected-vs-retained counts;
* **live read-only Prometheus** - disabled until an endpoint is configured, so
  the app can never claim a live pull it did not perform.

Honesty rules enforced by the backend and surfaced here:

* synthetic data is always badged as synthetic;
* supervised metrics only exist when the source really carries labels;
* fault classification refuses (and records the refusal) without labels - it is
  never faked;
* charts are drawn from computed results only: an empty result renders an
  empty state, not a decorative curve;
* this page performs no device configuration and no lab execution - the
  change-loop and isolated-lab execution gates stay disabled by default.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from services.app_state import (bootstrap, current_user,
                                demo_banner_if_active, flash, has_role, read,
                                render_flash, sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header(
    "AI Lab",
    "Real & simulated data experiments · anomaly comparison, forecasting, "
    "fault classification, statistics - with the protocol recorded",
    icon="🧪")
demo_banner_if_active()
render_flash()

from analytics import anomaly_detection, forecasting, lab_classification
from analytics import lab_dataset, lab_simulator, lab_sources
from config import get_settings
from services import lab_experiment, lab_tasks, prometheus_source

settings = get_settings()

if not has_role("operator"):
    st.info("Read-only access - sign in with an operator role to run and "
            "persist experiments.")

DISCLAIMER = (
    "Analysis only. Nothing on this page configures a device, proposes a "
    "change, or executes anything in the lab: "
    f"``change_loop_enabled`` = {str(settings.change_loop_enabled).lower()} and "
    f"``lab_execution_enabled`` = {str(settings.lab_execution_enabled).lower()} "
    "are read from the same configuration the rest of the app uses and this "
    "feature never alters them.")

st.caption(DISCLAIMER.replace("`", ""))


def _sec(label: str) -> None:
    st.markdown(f"<div class='np-sec'>{label}</div>", unsafe_allow_html=True)


def _fmt(value, digits=3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:,.{digits}g}" if isinstance(value, float) else f"{value:,}"
    return str(value)


def _interval(value) -> str:
    if not value:
        return "unknown"
    seconds = float(value)
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{minutes:.1f} min"
    return f"{minutes / 60.0:.1f} h"


def _tone_for(status: str) -> str:
    return {"ok": "ok", "refused": "warn", "insufficient_data": "warn",
            "no_labels": "bad", "error": "bad"}.get(status, "default")


# ----------------------------------------------------------------- sidebar
with st.sidebar:
    _sec("🧪 AI Lab boundary")
    st.caption(
        f"- samples per series ≤ {_fmt(settings.ai_lab_sim_max_points)}\n"
        f"- devices ≤ {settings.ai_lab_sim_max_devices}\n"
        f"- ML detector points ≤ {_fmt(settings.ai_lab_ml_max_points)}\n"
        f"- upload ≤ {settings.ai_lab_upload_max_bytes / 1e6:.0f} MB, "
        f"{lab_sources.MAX_UPLOAD_COLUMNS} columns, "
        f"{lab_sources.MAX_METRIC_COLUMNS} metric columns\n"
        f"- series per run ≤ {settings.scan_max_series}\n"
        f"- history rows shown ≤ {settings.ai_lab_history_limit}\n"
        f"- local file import: "
        f"{'enabled' if lab_sources.local_dir_status()['enabled'] else 'disabled (directory not set)'}\n"
        f"- live Prometheus: "
        f"{'configured' if prometheus_source.status()['configured'] else 'not configured (disabled)'}"
    )
    st.caption("Raise the bounds with the ``NETPILOT_AI_LAB_*`` variables in "
               "``.env`` - the app never silently truncates instead.")
    can_run = has_role("operator")


# ==================================================================== STEP 1
_sec("1️⃣ Select data source")

SOURCE_KEYS = {
    "Simulated telemetry (built-in generator, ground-truth labels)":
        "simulated",
    "Project history (measurements this app already collected)": "stored",
    "Upload a CSV/TSV file from the browser": "uploaded",
    "Local historical file (allowlisted directory)": "local",
    "Live read-only Prometheus query": "live",
}
choice_label = st.radio("Source type", list(SOURCE_KEYS), horizontal=False,
                        key="np_lab_source")
choice = SOURCE_KEYS[choice_label]


def _set_dataset(label: str, dataset) -> None:
    st.session_state["np_lab_dataset"] = dataset
    st.session_state["np_lab_dataset_label"] = label
    st.session_state.pop("np_lab_run", None)
    st.session_state.pop("np_lab_refs_sel", None)


def _stored_dataset():
    return st.session_state.get("np_lab_dataset")


_STORED = st.session_state.get("np_lab_dataset")
_STORED_LABEL = st.session_state.get("np_lab_dataset_label", "")

if choice == "simulated":
    st.caption("Synthetic data with known fault windows. It validates the "
               "method - it is never presented as a real-network measurement.")
    s1, s2 = st.columns([3, 2])
    with s1:
        devices = st.multiselect("Devices to simulate",
                                 ["R1", "R2", "R3", "R4", "SW1", "SW2"],
                                 default=["R1", "R2"],
                                 help=f"Bound: {settings.ai_lab_sim_max_devices} "
                                      "devices (NETPILOT_AI_LAB_SIM_MAX_DEVICES).")
        duration = st.slider("Duration (hours)", 1.0, 168.0, 12.0, step=1.0)
        interval = st.select_slider("Sampling interval (seconds)",
                                    options=[15, 30, 60, 120, 300, 600, 900],
                                    value=60)
        seed = st.number_input("Random seed (same seed = same data)",
                               min_value=0, max_value=2 ** 31 - 1,
                               value=lab_simulator.SimulationConfig().seed,
                               step=1)
    with s2:
        metrics = st.multiselect("Metrics",
                                 list(lab_simulator.METRIC_PROFILES),
                                 default=list(lab_simulator.DEFAULT_METRICS),
                                 help="cpu/memory are simulated-only: this lab "
                                      "collector does not measure them live.")
        st.markdown("**Injected fault events** (each one writes a ground-truth "
                    "label interval)")
        events: dict[str, int] = {}
        for name, profile in lab_simulator.EVENT_PROFILES.items():
            events[name] = st.number_input(
                f"{name} → {profile['metric']}", min_value=0, max_value=12,
                value=(2 if name in ("latency_spike", "packet_loss") else
                       1 if name in ("congestion", "cpu_overload",
                                     "interface_outage") else 0),
                step=1, key=f"np_lab_evt_{name}")
        events = {k: int(v) for k, v in events.items() if int(v) > 0}

    points = int(max(1, dt.timedelta(hours=duration).total_seconds()
                     // max(1, int(interval))))
    expected = points * max(1, len(devices)) * max(1, len(metrics))
    over = []
    if not devices:
        over.append("at least one device")
    if not metrics:
        over.append("at least one metric")
    if points > settings.ai_lab_sim_max_points:
        over.append(f"≤ {settings.ai_lab_sim_max_points} points per series")
    if expected > settings.analytics_max_points:
        over.append(f"≤ {settings.analytics_max_points} total rows")
    st.caption(f"That is {points:,} samples per series × {len(devices)} devices "
               f"× {len(metrics)} metrics = **{expected:,} rows**, "
               f"{'within' if not over else 'OUTSIDE - fix'} the configured "
               "bounds.")
    if over:
        st.warning("Request needs to satisfy: " + ", ".join(over),
                   icon="🚧")
    if st.button("Build simulated dataset", type="primary",
                 disabled=bool(over) or not can_run,
                 key="np_lab_build_sim"):
        try:
            dataset = lab_experiment.simulated_dataset(
                devices=tuple(devices), duration_hours=float(duration),
                interval_seconds=int(interval), seed=int(seed),
                metrics=tuple(metrics), events=events)
        except lab_dataset.LabDataError as exc:
            st.error(f"The simulator refused this request: {exc}",
                     icon="🚫")
        else:
            _set_dataset(f"simulated · seed {int(seed)} · {devices}", dataset)
            st.success("Simulated {:,} rows across {} series.".format(
                int(dataset.profile["n_rows"]), len(dataset.inventory)),
                icon="🧪")

elif choice == "stored":
    inventory = read(lambda s: lab_sources.stored_inventory(s))
    if not inventory:
        st.info("No stored measurements yet. Start the collector in the "
                "sidebar, or load demo mode - nothing is invented here.",
                icon="📭")
        st.stop()
    sources = sorted({item["data_source"] for item in inventory})
    data_source = st.selectbox(
        "Provenance (exactly one - demo and live rows are never mixed)",
        sources,
        format_func=lambda v: f"{v} " + (
            "(synthetic demo fixtures)" if v == "demo"
            else "(collected from the virtual lab)"))
    scoped = [item for item in inventory if item["data_source"] == data_source]
    device_names = sorted({item["device_name"] for item in scoped})
    device_sel = st.selectbox("Device", device_names)
    metric_names = sorted({item["metric"] for item in scoped
                           if item["device_name"] == device_sel})
    series_rows = [item for item in scoped if item["device_name"] == device_sel
                   and item["metric"] in metric_names]
    labels = [
        f"{item['metric']} · {item['interface_name'] or '(device-level)'} "
        f"· {item['unit'] or '?'} · {item['n_samples']:,} samples"
        for item in series_rows]
    picked_labels = st.multiselect("Series to import into the lab", labels,
                                   default=labels[:min(6, len(labels))],
                                   help=f"A run evaluates at most "
                                        f"{settings.scan_max_series} series "
                                        "(NETPILOT_SCAN_MAX_SERIES); extra "
                                        "selections are reported, not dropped "
                                        "silently.")
    r1, r2 = st.columns(2)
    with r1:
        since = st.date_input("From (date, UTC)",
                              value=dt.date.today() - dt.timedelta(days=7),
                              key="np_lab_since")
    with r2:
        until = st.date_input("To (date, UTC)", value=dt.date.today(),
                              key="np_lab_until")
    if st.button("Load from project database", type="primary",
                 disabled=not picked_labels or not can_run,
                 key="np_lab_build_stored"):
        selected = [series_rows[labels.index(l)] for l in picked_labels]
        try:
            dataset = read(lambda s: lab_sources.load_stored_data(
                s, data_source=data_source, series=[
                    {"device_name": item["device_name"],
                     "metric": item["metric"],
                     "interface_name": item["interface_name"]}
                    for item in selected],
                since=dt.datetime.combine(since, dt.time.min),
                until=dt.datetime.combine(until, dt.time.max)))
        except lab_dataset.LabDataError as exc:
            st.error(f"Import refused: {exc}", icon="🚫")
        else:
            _set_dataset(
                f"project history · {data_source} · {device_sel} · "
                f"{len(selected)} series", dataset)
            st.success(f"Loaded {dataset.profile['n_rows']:,} rows.",
                       icon="🗄️")

elif choice == "uploaded":
    guards = lab_sources.upload_guard_status()
    st.caption(f"Guards: ≤ {guards['max_upload_bytes'] / 1e6:.0f} MB, "
               f"≤ {guards['max_columns']} columns, "
               f"≤ {guards['max_metric_columns']} metric columns, "
               f"≤ {guards['max_dataset_rows']:,} dataset rows. The file is "
               "data, never code: it is parsed and validated, nothing in it is "
               "executed.")
    upload = st.file_uploader("CSV / TSV text file", type=["csv", "tsv", "txt"],
                              key="np_lab_upload")
    if upload is not None:
        raw = upload.getvalue()
        # (name, byte length) is the identity Streamlit exposes here; the
        # expensive parse below only re-runs when it changes.
        signature = (upload.name, len(raw))
        if st.session_state.get("np_lab_upload_sig") != signature:
            try:
                preview = lab_sources.preview_upload(raw, filename=upload.name)
                error = None
            except lab_dataset.LabDataError as exc:
                preview, error = None, str(exc)
            st.session_state["np_lab_upload_sig"] = signature
            st.session_state["np_lab_upload_raw"] = raw
            st.session_state["np_lab_preview"] = preview
            st.session_state["np_lab_preview_error"] = error
        if st.session_state.get("np_lab_preview_error"):
            st.error(st.session_state["np_lab_preview_error"], icon="🚫")
        else:
            preview = st.session_state["np_lab_preview"]
            parse = preview["parse"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Non-empty lines", f"{parse['n_text_lines']:,}")
            c2.metric("Rows parsed", f"{parse['n_parsed_rows']:,}")
            c3.metric("Malformed lines skipped",
                      f"{parse['n_rejected_lines']:,}")
            c4.metric("Duplicate rows", f"{preview['duplicate_rows']:,}")
            st.caption(f"Delimiter detected: **{parse['delimiter']}**, "
                       f"{parse['n_columns']} columns.")
            if parse["n_rejected_lines"]:
                st.warning(parse["rejected_note"], icon="✂️")
            with st.expander("Column preview and type guess", expanded=True):
                st.dataframe(pd.DataFrame(preview["columns"]).rename(columns={
                    "column": "column", "non_null": "non-null",
                    "missing": "missing", "missing_pct": "missing %",
                    "distinct": "distinct", "numeric_rows": "numeric rows",
                    "timestamp_rows": "timestamp rows", "guess": "guessed type",
                    "min": "min", "max": "max", "mean": "mean"}),
                    hide_index=True, width="stretch")
                st.dataframe(pd.DataFrame(preview["head"]), hide_index=True,
                             width="stretch")
            if preview["timestamp_coverage"]:
                cover = preview["timestamp_coverage"]
                st.caption(
                    f"Timestamp candidate **{cover['column']}**: "
                    f"{cover['first']} → {cover['last']} "
                    f"({cover['n_rows']:,} rows, median gap "
                    f"{_interval(cover['median_gap_seconds'])}, "
                    f"{cover['n_gaps_over_3x_median']} gap(s) over 3× the "
                    "median). Check this window before trusting the mapping - "
                    "an epoch-unit guess would show up here.")

            suggest = lab_sources.suggest_upload_mapping(preview)
            columns = [profile["column"] for profile in preview["columns"]]
            none_option = "(none)"
            st.markdown("**Confirm or change the column mapping**")
            m1, m2, m3 = st.columns(3)
            with m1:
                timestamp = st.selectbox("Timestamp column", columns,
                                         index=columns.index(suggest["timestamp"])
                                         if suggest["timestamp"] in columns else 0)
                device_col = st.selectbox(
                    "Device column", [none_option] + columns,
                    index=(1 + columns.index(suggest["device"]))
                    if suggest["device"] in columns else 0)
                interface_col = st.selectbox(
                    "Interface column", [none_option] + columns,
                    index=(1 + columns.index(suggest["interface"]))
                    if suggest["interface"] in columns else 0)
            with m2:
                metric_col = st.selectbox(
                    "Metric-name column (long format)", [none_option] + columns,
                    index=(1 + columns.index(suggest["metric"]))
                    if suggest["metric"] in columns else 0)
                value_col = st.selectbox(
                    "Value column (long format)", [none_option] + columns,
                    index=(1 + columns.index(suggest["value"]))
                    if suggest["value"] in columns else 0)
                unit_col = st.selectbox(
                    "Unit column", [none_option] + columns,
                    index=(1 + columns.index(suggest["unit"]))
                    if suggest["unit"] in columns else 0)
            with m3:
                label_col = st.selectbox(
                    "Label column (ground truth)", [none_option] + columns,
                    index=(1 + columns.index(suggest["label"]))
                    if suggest["label"] in columns else 0)
                device_default = st.text_input(
                    "Device name when the file has none", "(uploaded)")
            long_format = metric_col != none_option and value_col != none_option
            reserved = {c for c in (timestamp, device_col, interface_col,
                                    metric_col, value_col, unit_col, label_col)
                        if c != none_option}
            numeric_default = [c for c in suggest["metrics"] if c not in reserved]
            wide_metrics = st.multiselect(
                "Metric columns (wide format - used when no metric/value pair "
                "is mapped)", columns, default=numeric_default,
                disabled=long_format)
            with st.expander("Units and metric names as read (state them, do "
                             "not guess)"):
                unit_targets = ([metric_col] if long_format else wide_metrics)
                units: dict[str, str] = {}
                for name in unit_targets:
                    units[name] = st.text_input(
                        f"Unit for {name if long_format else 'column ' + name}",
                        value="", key=f"np_lab_unit_{name}",
                        placeholder="e.g. ms, %, bps (blank = unit unknown)")
                aliases_text = st.text_area(
                    "Metric aliases (optional), one 'original=renamed' per line",
                    key="np_lab_aliases", height=80,
                    help="Use it to merge spellings of the same metric. The "
                         "rename is recorded in the dataset lineage.")
            aliases = {}
            for line in (aliases_text or "").splitlines():
                if "=" in line:
                    original, renamed = line.split("=", 1)
                    aliases[original.strip()] = renamed.strip()
            note = st.text_input("Provenance note (what this file is, where it "
                                 "came from)", key="np_lab_upload_note")
            st.caption(suggest["notes"])

            if st.button("Validate and load upload", type="primary",
                         key="np_lab_build_upload", disabled=not can_run):
                mapping = {
                    "format": "long" if long_format else "wide",
                    "timestamp": timestamp,
                    "device": None if device_col == none_option else device_col,
                    "interface": None if interface_col == none_option
                    else interface_col,
                    "metric": None if metric_col == none_option else metric_col,
                    "value": None if value_col == none_option else value_col,
                    "unit": None if unit_col == none_option else unit_col,
                    "label": None if label_col == none_option else label_col,
                    "metrics": wide_metrics,
                }
                try:
                    dataset, report = lab_sources.load_uploaded_csv(
                        st.session_state["np_lab_upload_raw"],
                        filename=upload.name, mapping=mapping,
                        units={k: v for k, v in units.items() if v},
                        metric_aliases=aliases,
                        device_default=device_default or "(uploaded)",
                        source_note=note)
                except lab_dataset.LabDataError as exc:
                    st.error(f"Validation refused this file: {exc}", icon="🚫")
                else:
                    st.session_state["np_lab_upload_report"] = report
                    _set_dataset(f"upload · {upload.name}", dataset)
                    st.success("Retained {:,} of {:,} mapped rows "
                               "({:,} rejected).".format(
                                   report["rows_retained"],
                                   report["rows_after_mapping"],
                                   report["rows_rejected"]), icon="📥")

            report = st.session_state.get("np_lab_upload_report")
            if report and _STORED_LABEL.startswith(f"upload · {upload.name}"):
                st.markdown("**Retained vs rejected**")
                st.dataframe(pd.DataFrame([{
                    "lines in file": report["lines_in_file"],
                    "rows parsed": report["rows_parsed"],
                    "malformed skipped": report["rows_malformed_skipped"],
                    "rows after mapping": report["rows_after_mapping"],
                    "rows retained": report["rows_retained"],
                    "rows rejected": report["rows_rejected"],
                    "missing values": report["values_missing"],
                }]), hide_index=True, width="stretch")
                st.caption(report["explanation"])
                for warning in report["warnings"]:
                    st.caption(f"⚠️ {warning}")

elif choice == "local":
    status = lab_sources.local_dir_status()
    if not status["enabled"]:
        st.info(f"{status['reason']} Set ``NETPILOT_AI_LAB_LOCAL_DIR`` to one "
                "directory and only files inside it can be imported - a path "
                "that escapes it is refused.", icon="🔒")
    else:
        st.caption(f"Allowlisted directory: `{status['directory']}`")
        files = lab_sources.list_local_files()
        if not files:
            st.info("No CSV files in that directory yet.", icon="📁")
        else:
            st.dataframe(pd.DataFrame(files), hide_index=True, width="stretch")
            name = st.selectbox("File to import",
                                [f["name"] for f in files if "error" not in f])
            path = str((lab_sources.Path(status["directory"]) / name))
            try:
                text = open(path, encoding="utf-8-sig").read()
            except OSError as exc:
                st.error(f"Could not read the file: {exc}", icon="🚫")
                text = ""
            if text:
                preview = lab_sources.preview_upload(text, filename=name,
                                                     sample_rows=5)
                suggest = lab_sources.suggest_upload_mapping(preview)
                st.code(str(suggest), language="json")
                st.caption("The mapping above is the heuristic proposal. It "
                           "becomes an explicit mapping only after you "
                           "re-upload the same file through the upload tab "
                           "(mapping controls live there); this tab imports it "
                           "as declared historical data.")
                declared = st.text_input("Declared provenance of this file",
                                         key="np_lab_local_declared",
                                         placeholder="e.g. 'NOC export, "
                                         "router CPU, 2026-09, lab R1-R3'")
                if st.button("Import historical file", type="primary",
                             key="np_lab_build_local", disabled=not can_run):
                    try:
                        dataset, report = lab_sources.load_local_file(
                            name, mapping=suggest, dataset_label=declared)
                    except lab_dataset.LabDataError as exc:
                        st.error(f"Import refused: {exc}", icon="🚫")
                    else:
                        _set_dataset(f"local file · {name}", dataset)
                        st.success("Imported {:,} rows ({:,} rejected).".format(
                            report["rows_retained"], report["rows_rejected"]),
                            icon="📄")

elif choice == "live":
    status = prometheus_source.status()
    if not status["configured"]:
        st.warning(f"{status['reason']}", icon="🔌")
        st.caption("Once an endpoint is configured this tab becomes a read-only "
                   "range-query adapter: one bounded ``GET /api/v1/query_range`` "
                   "per expression, credentials stripped from any displayed "
                   "URL, and a failure recorded as a failure. Until then no "
                   "live result can be shown, and none is simulated.")
    else:
        st.caption(f"Configured endpoint: `{status['base_url']}` "
                   f"(timeout {status['timeout_seconds']} s)")
        health = None
        try:
            health = prometheus_source.health()
        except Exception as exc:
            st.error(f"Endpoint not reachable: {type(exc).__name__}: {exc}",
                     icon="📵")
        if health:
            version = health.get("build_version") or "version not reported"
            st.success(f"Reachable: {version} · checked {health['checked_at']} "
                       "UTC")
        expression = st.text_input("PromQL range expression",
                                   value="node_cpu_seconds_total",
                                   key="np_lab_prom_expr")
        p1, p2, p3 = st.columns(3)
        hours = p1.slider("Lookback (hours)", 1.0, 48.0, 3.0, step=1.0)
        step_seconds = p2.select_slider("Step (seconds)",
                                         options=[15, 30, 60, 120, 300],
                                         value=60)
        metric_name = p3.text_input("Series name to record", value="",
                                    key="np_lab_prom_name")
        if st.button("Fetch live window (read-only)", type="primary",
                     disabled=not expression.strip() or not can_run,
                     key="np_lab_build_live"):
            try:
                dataset = prometheus_source.fetch_range(
                    expression=expression.strip(), hours=float(hours),
                    step_seconds=int(step_seconds),
                    metric_name=metric_name.strip() or None)
            except Exception as exc:
                st.error(f"Live fetch failed and nothing was substituted: "
                         f"{type(exc).__name__}: {exc}", icon="📵")
            else:
                _set_dataset(f"live Prometheus · {expression.strip()}", dataset)
                st.success("Fetched {:,} samples.".format(
                    int(dataset.profile["n_rows"])), icon="📡")


# ==========================================================================
# Helpers shared by the run/result/export sections
# ==========================================================================
def _dataset_label() -> str:
    return st.session_state.get("np_lab_dataset_label", "")


def _series_selector(dataset, task: str) -> list[lab_dataset.SeriesRef]:
    """Bounded, explicit series selection - the bound is stated, never hidden."""
    refs = dataset.series_refs()
    labels = [str(ref) for ref in refs]
    default = labels[:min(settings.scan_max_series, len(labels))]
    chosen = st.multiselect(
        "Series to evaluate", labels, default=default, key="np_lab_refs_sel",
        help=f"At most {settings.scan_max_series} series per run "
             "(NETPILOT_SCAN_MAX_SERIES). Selecting more is reported as a "
             "truncated scan, not silently narrowed.")
    if len(chosen) > settings.scan_max_series:
        st.warning(f"{len(chosen)} series selected but the run will evaluate "
                   f"the first {settings.scan_max_series} and label the result "
                   "as a partial scan.", icon="✂️")
    return [refs[labels.index(label)] for label in chosen]


TASK_LABELS = {
    lab_dataset.TASK_ANOMALY:
        "anomaly - compare detectors against one ground truth",
    lab_dataset.TASK_FORECAST:
        "forecast - compare models on one identical holdout",
    lab_dataset.TASK_CLASSIFICATION:
        "classification - supervised fault classes (labels required)",
    lab_dataset.TASK_STATISTICS:
        "statistics - descriptive + association, no causal claims",
}


def _config_fields(dataset, task: str) -> dict:
    """Task configuration widgets -> the orchestrator's own keyword names."""
    defaults = lab_experiment.task_defaults(task, dataset)
    allowed = set(lab_experiment.accepted_params(task))
    params: dict[str, object] = {}
    if task == lab_dataset.TASK_ANOMALY:
        params["detectors"] = tuple(st.multiselect(
            "Detectors to compare", list(anomaly_detection.DETECTORS),
            default=defaults["detectors"],
            help="isolation_forest refits a model for every judged point, so "
                 f"its evaluated window is capped at "
                 f"{settings.ai_lab_ml_max_points} points and the run says so."))
        params["window"] = st.slider("Rolling window (samples)", 5, 200,
                                     int(defaults["window"]), step=5)
        with st.expander("Per-detector sensitivity (defaults come from the "
                         "project configuration)"):
            defaults_map = {"mad_threshold": settings.mad_threshold,
                            "zscore_threshold": settings.zscore_threshold,
                            "ewma_threshold": settings.ewma_threshold,
                            "cusum_drift": settings.cusum_drift_k,
                            "cusum_limit": settings.cusum_threshold_h,
                            "seasonal_threshold": settings.seasonal_threshold}
            for key, label, lo, hi in (
                    ("mad_threshold", "MAD threshold", 1.0, 8.0),
                    ("zscore_threshold", "z-score threshold", 1.0, 8.0),
                    ("ewma_threshold", "EWMA threshold", 1.0, 8.0),
                    ("cusum_drift", "CUSUM drift k", 0.1, 2.0),
                    ("cusum_limit", "CUSUM decision limit h", 2.0, 12.0),
                    ("seasonal_threshold", "Seasonal-residual threshold",
                     1.0, 8.0)):
                params[key] = st.slider(label, lo, hi,
                                        float(defaults_map[key]), step=0.1,
                                        key=f"np_lab_{key}")
        params["sweep_curve"] = st.checkbox(
            "Compute the sensitivity sweep against the labelled truth",
            value=bool(defaults["sweep_curve"]))
        params["include_multivariate"] = st.checkbox(
            "Add the multivariate Isolation Forest pass",
            value=bool(defaults["include_multivariate"]))
        if params["include_multivariate"]:
            params["multivariate_metrics"] = tuple(st.multiselect(
                "Metrics for the multivariate pass", dataset.metrics(),
                default=dataset.metrics()[:4]))
    elif task == lab_dataset.TASK_FORECAST:
        params["horizon"] = st.slider("Forecast horizon (steps)", 3, 48,
                                      int(defaults["horizon"]))
        params["models"] = tuple(st.multiselect(
            "Models to compare", list(forecasting.FORECAST_MODELS),
            default=defaults["models"],
            help="naive is the mandatory baseline; seasonal_naive refuses "
                 "when the series holds fewer than two full seasons."))
        params["test_frac"] = st.slider("Holdout fraction (chronological tail)",
                                        0.1, 0.5, float(defaults["test_frac"]),
                                        step=0.05)
        params["band_coverage"] = st.slider("Band nominal coverage", 0.5, 0.99,
                                            float(settings.band_nominal_coverage),
                                            step=0.01)
        params["ensemble"] = st.checkbox(
            "Add the in-memory inverse-MAE ensemble",
            value=bool(defaults["ensemble"]))
    elif task == lab_dataset.TASK_CLASSIFICATION:
        devices = dataset.devices()
        device = st.selectbox("Device whose fault classes are learned",
                              devices, key="np_lab_cls_device")
        params["device"] = device
        metric_names = dataset.metrics(device)
        scopes = sorted({ref.interface for ref in dataset.series_refs()
                         if ref.device == device},
                        key=lambda v: (v is not None, v or ""))
        options = [None] + [s for s in scopes if s]
        picked = st.selectbox(
            "Series scope", options,
            format_func=lambda v: v or "(device-level)",
            key="np_lab_cls_iface",
            index=options.index(scopes[0]) if scopes else 0,
            help="Interfaces this device's series are recorded under. A "
                 "device-level scope only exists if the source really has "
                 "device-level series.")
        params["interface"] = None if picked in (None, "") else picked
        labelled = sorted({item.ref.metric for item in dataset.inventory
                           if item.ref.device == device and item.n_labels > 1})
        params["label_metric"] = st.selectbox(
            "Labelled series (source of the target class)", metric_names,
            index=metric_names.index(labelled[0])
            if labelled and labelled[0] in metric_names else 0,
            help="Classes are read from the labels attached to this series. A "
                 "series without labels cannot be a target - the run refuses "
                 "instead of inventing classes.")
        params["feature_metrics"] = tuple(st.multiselect(
            "Feature metrics (the label series is excluded)",
            [m for m in metric_names if m != params["label_metric"]],
            default=[m for m in metric_names
                     if m != params["label_metric"]][:4]))
        params["models"] = tuple(st.multiselect(
            "Classifiers", list(lab_classification.CLASSIFIERS),
            default=defaults["models"]))
        params["split_mode"] = st.radio(
            "Split protocol",
            [lab_classification.SPLIT_CHRONOLOGICAL,
             lab_classification.SPLIT_STRATIFIED],
            format_func=lambda v: v, horizontal=True,
            help=lab_classification.SPLIT_NOTES[
                lab_classification.SPLIT_CHRONOLOGICAL])
        params["test_frac"] = st.slider("Holdout fraction", 0.1, 0.5,
                                        float(defaults["test_frac"]), step=0.05)
        params["add_lags"] = st.slider("Lag features (steps of history added)",
                                       0, 6, 0)
        params["random_seed"] = st.number_input("Classifier random seed",
                                                min_value=0, max_value=99999,
                                                value=42, step=1)
    elif task == lab_dataset.TASK_STATISTICS:
        params["lag_max"] = st.slider("Maximum cross-correlation lag (steps)",
                                      1, 48, int(defaults["lag_max"]))
        params["correlations"] = st.checkbox(
            "Pairwise correlations (Spearman on shared timestamps)",
            value=bool(defaults["correlations"]))
        params["group_by_labels"] = st.checkbox(
            "Compare label groups (needs labels)",
            value=bool(defaults["group_by_labels"]))
    unknown = [key for key in params if key not in allowed]
    if unknown:  # defensive: the form must never invent parameter names
        raise RuntimeError(f"form produced unknown parameter(s): {unknown}")
    return params


def _label_runs(timestamps, truth_set):
    """Contiguous runs of timestamps that are in ``truth_set`` (shading only)."""
    if not truth_set:
        return []
    marked = [pd.Timestamp(ts) in truth_set for ts in timestamps]
    runs = []
    start = None
    for index, flag in enumerate(marked):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((timestamps[start], timestamps[index - 1]))
            start = None
    if start is not None:
        runs.append((timestamps[start], timestamps[len(marked) - 1]))
    return runs


def _series_line(items):
    """First ``kind=series``/``kind=history`` payload for a chart group."""
    out = {}
    for entry in items:
        if entry.get("kind") in ("series", "history"):
            out[entry["series"]] = entry
    return out


def _render_anomaly_charts(result):
    series_map = _series_line(result["points"])
    truth = {}
    flags = []
    for entry in result["points"]:
        if entry.get("kind") == "truth":
            truth.setdefault(entry["series"], set()).add(
                pd.Timestamp(entry["t"]))
        elif entry.get("kind") == "flags":
            flags.append(entry)
    plotted = 0
    for name, entry in series_map.items():
        payload = entry.get("points") or []
        if not payload:
            st.info(f"**{name}** produced no plotted points - nothing is "
                    "drawn in its place.", icon="⬜")
            continue
        plotted += 1
        timestamps = [pd.Timestamp(point["t"]) for point in payload]
        values = [point["v"] for point in payload]
        fig = go.Figure()
        for begin, finish in _label_runs(timestamps, truth.get(name)):
            fig.add_vrect(x0=begin, x1=finish, line_width=0,
                          fillcolor="#F43F5E", opacity=.10)
        fig.add_trace(go.Scatter(x=timestamps, y=values, mode="lines",
                                 name="measured value",
                                 line=dict(color="#38BDF8")))
        palette = ["#22C55E", "#F59E0B", "#A78BFA", "#F43F5E", "#14B8A6",
                   "#EAB308", "#F97316", "#94A3B8"]
        lookup = dict(zip(timestamps, values))
        for index, flag in enumerate(flags):
            if flag.get("series") != name:
                continue
            at = [pd.Timestamp(ts) for ts in flag.get("points") or []]
            shown = [lookup[ts] for ts in at if ts in lookup]
            at = [ts for ts in at if ts in lookup]
            if not at:
                continue
            fig.add_trace(go.Scatter(
                x=at, y=shown, mode="markers",
                name=f"{flag.get('detector')} flag",
                marker=dict(symbol="x", size=9,
                            color=palette[index % len(palette)])))
        fig.update_layout(
            height=330, margin=dict(l=10, r=10, t=38, b=10),
            title=f"{name} - ground-truth shading vs detector flags",
            legend=dict(orientation="h", y=-0.25))
        st.plotly_chart(fig, width="stretch")
        if entry.get("note"):
            st.caption(entry["note"])
    extra_notes = [entry for entry in result["points"]
                   if entry.get("kind") == "flags"
                   and entry.get("series") not in series_map]
    for entry in extra_notes:
        st.caption(f"{entry['series']}: {len(entry.get('points') or [])} "
                   "flag(s) from "
                   f"{entry.get('detector')} - no shared measurement axis to "
                   "draw them on, so they are listed in the table instead.")
    if not plotted:
        st.info("This run produced no chart payload.", icon="⬜")


def _render_forecast_charts(result):
    series_map = _series_line(result["points"])
    boundaries = {entry["series"]: entry for entry in result["points"]
                  if entry.get("kind") == "split_boundary"}
    holdout = [entry for entry in result["points"]
               if entry.get("kind") == "holdout"]
    future = [entry for entry in result["points"]
              if entry.get("kind") == "future"]
    plotted = 0
    for name, entry in series_map.items():
        payload = entry.get("points") or []
        if not payload:
            st.info(f"**{name}** has no history to plot.", icon="⬜")
            continue
        plotted += 1
        timestamps = [pd.Timestamp(point["t"]) for point in payload]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=timestamps,
                                 y=[point["v"] for point in payload],
                                 mode="lines", name="measured",
                                 line=dict(color="#38BDF8")))
        boundary = boundaries.get(name)
        if boundary:
            fig.add_vline(x=pd.Timestamp(boundary["train_end"]),
                          line_dash="dot", line_color="#94A3B8",
                          annotation_text="train | holdout",
                          annotation_position="top left")
        for run in holdout:
            if run.get("series") != name:
                continue
            fig.add_trace(go.Scatter(
                x=pd.to_datetime([point["t"] for point in run["points"]]),
                y=[point["v"] for point in run["points"]],
                mode="lines", name=f"{run['model']} (holdout)",
                line=dict(dash="dash")))
        for run in future:
            if run.get("series") != name or not run.get("points"):
                continue
            xs = pd.to_datetime([point["t"] for point in run["points"]])
            upper = [point.get("upper") for point in run["points"]]
            lower = [point.get("lower") for point in run["points"]]
            if all(value is not None for value in upper + lower):
                fig.add_trace(go.Scatter(
                    x=list(xs) + list(xs[::-1]),
                    y=upper + lower[::-1], fill="toself",
                    line=dict(width=0), opacity=.12, showlegend=False,
                    hoverinfo="skip"))
            fig.add_trace(go.Scatter(
                x=xs, y=[point["v"] for point in run["points"]],
                mode="lines", name=f"{run['model']} (future)"))
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=38, b=10),
                          title=f"{name} - holdout fit and forward estimate",
                          legend=dict(orientation="h", y=-0.3))
        st.plotly_chart(fig, width="stretch")
        if entry.get("note"):
            st.caption(entry["note"])
    if not plotted:
        st.info("This run produced no chart payload.", icon="⬜")


def _render_confusion_chart(result):
    rows = [entry for entry in result["points"]
            if entry.get("kind") == "confusion"]
    if not rows:
        return False
    frame = pd.DataFrame(rows)
    fig = go.Figure()
    for model, group in frame.groupby("model"):
        fig.add_trace(go.Bar(x=group["predicted"], y=group["count"],
                             name=str(model)))
    fig.update_layout(barmode="group", height=340,
                      margin=dict(l=10, r=10, t=38, b=10),
                      title="Confusion counts per model",
                      xaxis_title="predicted class", yaxis_title="count",
                      legend=dict(orientation="h", y=-0.3))
    st.plotly_chart(fig, width="stretch")
    return True

# ==========================================================================
# Helpers for the result / export sections
# ==========================================================================
def _short(key, value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if value != value:
            return "n/a"
        if abs(value) >= 1e6 or (value != 0 and abs(value) < 1e-3):
            return f"{value:.3g}"
        return f"{value:,.4g}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (list, tuple, set, dict)):
        return f"{type(value).__name__}({len(value)})"
    text = str(value)
    return text if len(text) <= 90 else text[:87] + "..."


def _metrics_grid(metrics: dict, columns: int = 4) -> None:
    items = [(str(key), _short(key, value)) for key, value in (metrics or {}).items()]
    for offset in range(0, len(items), columns):
        cells = st.columns(columns)
        for index, (key, value) in enumerate(items[offset:offset + columns]):
            cells[index].metric(key.replace("_", " "), value)


def _table_frame(rows: list[dict]):
    """DataFrame from union-of-keys rows (rows may have different shapes)."""
    if not rows:
        return None
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return pd.DataFrame([[row.get(column) for column in columns]
                         for row in rows], columns=columns)


def _json_expander(label: str, payload) -> None:
    with st.expander(label, expanded=False):
        st.json(payload, expanded=False)


def _flat_dict(payload: dict) -> dict:
    out: dict[str, str] = {}
    for key, value in (payload or {}).items():
        if isinstance(value, dict):
            for inner, sub in value.items():
                out[f"{key} · {inner}"] = _short(inner, sub)
        elif isinstance(value, list):
            out[key] = f"{len(value)} item(s)"
        else:
            out[key] = _short(key, value)
    return out


def _render_charts(result: dict, task: str) -> None:
    points = result.get("points") or []
    if not points:
        st.info("This run returned no chart payload, so nothing is plotted. "
                "A chart here would otherwise be decoration, not evidence.",
                icon="⬜")
        return
    kinds = sorted({entry.get("kind", "?") for entry in points})
    st.caption("Plotted payload kinds: " + ", ".join(f"`{kind}`"
                                                    for kind in kinds)
               + " - every point is a computed result of this run.")
    if task == lab_dataset.TASK_ANOMALY:
        _render_anomaly_charts(result)
    elif task == lab_dataset.TASK_FORECAST:
        _render_forecast_charts(result)
    elif task == lab_dataset.TASK_CLASSIFICATION:
        if not _render_confusion_chart(result):
            st.info("No confusion payload: the run refused before any model "
                    "was fitted.", icon="⬜")
    else:
        st.caption("The statistics task reports tables - a correlation table "
                   "is more readable as numbers than as a curve.")


def _export_block(dataset, label: str) -> None:
    run = st.session_state.get("np_lab_run")
    if not run:
        st.info("Run an experiment first: exports describe a computed run, "
                "they are never generated from an empty template.", icon="📤")
        return
    result = run["result"]
    meta = run["meta"]
    from database.models import db_utcnow
    stamp = db_utcnow().strftime("%Y%m%dT%H%M%SZ")
    stem = f"netpilot_lab_{meta['task']}_{stamp}_{meta['experiment_uuid'][:8]}"
    columns = st.columns(4)
    columns[0].download_button(
        "Table CSV", data=lab_experiment.export_table_csv(result),
        file_name=f"{stem}_table.csv", mime="text/csv",
        key="np_lab_dl_table", width="stretch")
    columns[1].download_button(
        "Metrics CSV", data=lab_experiment.export_metrics_csv(result),
        file_name=f"{stem}_metrics.csv", mime="text/csv",
        key="np_lab_dl_metrics", width="stretch")
    columns[2].download_button(
        "Markdown report",
        data=lab_experiment.export_markdown(dataset, result, meta=meta),
        file_name=f"{stem}.md", mime="text/markdown",
        key="np_lab_dl_md", width="stretch")
    columns[3].download_button(
        "HTML report",
        data=lab_experiment.export_html(dataset, result, meta=meta),
        file_name=f"{stem}.html", mime="text/html",
        key="np_lab_dl_html", width="stretch")
    st.caption(f"Exports of `{label}` / experiment `{meta['experiment_uuid']}` "
               f"(status `{meta['status']}`). CSV cells that begin with an "
               "active character are prefixed with an apostrophe so a "
               "spreadsheet cannot execute them, and every report repeats the "
               "source provenance.")





# ==================================================================== STEP 2
st.divider()
_sec("2️⃣ Inspect data")
dataset = _stored_dataset()
if dataset is None:
    st.info("No dataset loaded yet - build or import one in step 1. Nothing "
            "below is pre-filled with sample numbers.", icon="🫙")
else:
    meta_badge = st.columns([2, 2, 3])
    with meta_badge[0]:
        # one rule, shared with the stored record, so the badge and the
        # history row can never disagree about what the data is
        synthetic = lab_experiment.is_synthetic(dataset)
        st.markdown(theme.status_pill(
            f"source: {dataset.source_type}",
            "violet" if synthetic else "info"), unsafe_allow_html=True)
        st.markdown(theme.status_pill(
            "SYNTHETIC - not a real-network measurement" if synthetic
            else "operator/collector supplied - not verified by this app",
            "warn" if synthetic else "muted"), unsafe_allow_html=True)
    with meta_badge[1]:
        summary = dataset.summary()
        st.metric("Rows / usable values",
                  f"{summary['n_rows']:,} / {summary['n_values']:,}")
        st.metric("Series / labels",
                  f"{summary['n_series']} / {summary['label_kind']}")
    with meta_badge[2]:
        st.metric("Time range (UTC)",
                  f"{(summary['time_min'] or '?')[:16]} → {(summary['time_max'] or '?')[:16]}",
    )
        st.caption(f"median interval {_interval(summary['median_interval_seconds'])}"
                   f" · fingerprint `{dataset.fingerprint}` · {_STORED_LABEL}")

    gate_col, quality_col = st.columns([3, 2])
    with gate_col:
        _sec("Task gates")
        for task in lab_dataset.TASKS:
            ok, why = lab_dataset.task_gate(dataset, task)
            state = "ready" if ok else "blocked"
            pill = theme.status_pill(f"{task}: {state}",
                                     "ok" if ok else "warn")
            st.markdown(f"- {pill} {why}", unsafe_allow_html=True)
    with quality_col:
        _sec("Data provenance")
        st.json(dataset.provenance, expanded=False)
    if dataset.warnings:
        with st.expander(f"Ingest warnings ({len(dataset.warnings)})",
                         expanded=False):
            for warning in dataset.warnings:
                st.caption(f"⚠️ {warning}")
    if dataset.lineage:
        with st.expander("Lineage - what happened to the rows", expanded=False):
            st.dataframe(pd.DataFrame([
                {"step": step.step, "detail": step.detail,
                 "rows in": step.rows_in, "rows out": step.rows_out}
                for step in dataset.lineage]), hide_index=True, width="stretch")

    _sec("Series inventory (readiness per task)")
    inv_rows = []
    for item in dataset.inventory:
        inv_rows.append({
            "series": str(item.ref), "unit": item.unit or "?",
            "rows": item.n_rows, "values": item.n_values,
            "labels": item.n_labels,
            "first": item.first_ts.isoformat(timespec="seconds")
            if item.first_ts else None,
            "last": item.last_ts.isoformat(timespec="seconds")
            if item.last_ts else None,
            "analysis": "✔" if item.ready.get("statistics") else "✖",
            "anomaly": "✔" if item.ready.get("anomaly") else "✖",
            "forecast": "✔" if item.ready.get("forecast") else "✖",
            "classification": "✔" if item.ready.get("classification") else "✖",
            "blockers": "; ".join(item.reasons),
        })
    st.dataframe(pd.DataFrame(inv_rows), hide_index=True, width="stretch")

    _sec("Data-quality summary (computed per series, nothing imputed here)")
    quality_rows = []
    for item in dataset.inventory:
        q = dataset.quality(item.ref)
        quality_rows.append({
            "series": str(item.ref),
            "rows": q.n_rows,
            "usable": q.n_non_null,
            "missing values": q.n_null_values,
            "duplicate timestamps": q.n_duplicates,
            "sampling gaps": q.gap_count,
            "longest gap": _interval(q.longest_gap_seconds),
            "missing slots": q.missing_expected,
            "complete %": (None if q.completeness_pct is None
                           else round(q.completeness_pct, 1)),
            "age of newest": _interval(q.stale_seconds),
            "stale": "yes" if q.is_stale else "no",
        })
    quality_frame = pd.DataFrame(quality_rows)
    st.dataframe(quality_frame, hide_index=True, width="stretch")
    stale_count = int((quality_frame["stale"] == "yes").sum())
    notes = [f"{stale_count}/{len(quality_frame)} series are older than the "
             "staleness window (age > max(4 × median interval, 30 min)) - the "
             "values are still real readings, just not fresh, so do not read a "
             "forecast here as a current condition"]
    if dataset.profile.get("n_constant_series"):
        notes.append(f"{dataset.profile['n_constant_series']} constant series "
                     "carry no variance (metrics such as an interface state "
                     "are legitimate constants, but a model gains nothing "
                     "from them)")
    if int(quality_frame["missing values"].sum()):
        notes.append(f"{int(quality_frame['missing values'].sum())} stored-but-"
                     "unavailable value(s) are kept as missing; they are never "
                     "filled with 0 before you choose a preprocessing step")
    for note in notes:
        st.caption("• " + note)

    _sec("Raw values as read (measurement, not a result)")
    ref_labels = [str(ref) for ref in dataset.series_refs()]
    preview_label = st.selectbox("Series to preview", ref_labels,
                                 key="np_lab_preview_series")
    preview_ref = dataset.series_refs()[ref_labels.index(preview_label)]
    pv = dataset.series_frame(preview_ref)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=pd.to_datetime(pv["collected_at"]),
                             y=pv["value"], mode="lines+markers",
                             name=f"{preview_ref.metric} (raw)"))
    labels = dataset.series_labels(preview_ref)
    if labels is not None and dataset.label_kind == "binary":
        flagged = [ts for ts, val in zip(pv["collected_at"], labels) if val]
        for ts in flagged:
            fig.add_vrect(x0=pd.Timestamp(ts) - pd.Timedelta(seconds=1),
                          x1=pd.Timestamp(ts) + pd.Timedelta(seconds=1),
                          line_width=0, fillcolor="#F43F5E", opacity=.15)
        if flagged:
            fig.add_trace(go.Scatter(x=[], y=[], mode="markers",
                                     marker=dict(color="#F43F5E"),
                                     name="ground-truth anomaly (label)"))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10),
                      title=f"{preview_ref} - {pv['unit'].iloc[0] if 'unit' in pv else ''}".strip())
    st.plotly_chart(fig, width="stretch")


    # ==================================================================== STEP 3
    st.divider()
    _sec("3️⃣ Select task")
    task_labels = {value: key for key, value in TASK_LABELS.items()}
    task_picked = st.radio(
        "Experiment task", list(TASK_LABELS.values()),
        format_func=lambda v: v, key="np_lab_task", horizontal=False)
    task = task_labels[task_picked]
    ok, why = lab_dataset.task_gate(dataset, task)
    if ok:
        st.success(why, icon="✅")
    else:
        st.warning(f"{why}\n\nYou can still run it: the refusal is computed, "
                   "explained and persisted as an outcome of this experiment.",
                   icon="🚧")
    if task == lab_dataset.TASK_CLASSIFICATION and dataset.label_kind == "none":
        st.error("This source carries no ground-truth labels. Supervised "
                 "fault classification will be refused and recorded as "
                 "`no_labels` - NetPilot does not label telemetry on your "
                 "behalf.", icon="🏷️")

    # ==================================================================== STEP 4
    _sec("4️⃣ Configure model / protocol")
    refs: list[lab_dataset.SeriesRef] = []
    if task == lab_dataset.TASK_CLASSIFICATION:
        st.caption("The classifier is scoped to one device and one labelled "
                   "series; feature series are named explicitly below.")
    else:
        refs = _series_selector(dataset, task)
    with st.expander("Sensitivity of this run's bounds", expanded=False):
        st.caption(
            f"- series evaluated: ≤ {settings.scan_max_series}\n"
            f"- per-point ML detectors (isolation_forest): ≤ "
            f"{settings.ai_lab_ml_max_points} most recent samples per series, "
            "and the labelled truth is scored over that same window\n"
            f"- chart points plotted: ≤ {lab_tasks.MAX_CHART_POINTS} per series "
            "(metrics always use every point)\n"
            f"- stored chart payload: ≤ "
            f"{lab_experiment.MAX_POINTS_JSON_CHARS // 1000}k characters, "
            "otherwise the row records that points were not persisted")
    try:
        params = _config_fields(dataset, task)
    except Exception as exc:  # a broken form must not silently run defaults
        st.error(f"The configuration form could not be built: "
                 f"{type(exc).__name__}: {exc}", icon="🛑")
        params = {}
    if not refs and task != lab_dataset.TASK_CLASSIFICATION:
        st.warning("Select at least one series, or the run reports "
                   "`insufficient data` for the empty selection.", icon="🎯")
    if not can_run:
        st.info("Read-only session: viewing and inspecting is allowed, running "
                "an experiment needs an operator role.", icon="🔐")
    run_clicked = st.button("Run experiment", type="primary",
                            disabled=not can_run, key="np_lab_run_btn")
    if run_clicked:
        actor = current_user() or {}
        seed_value = params.get("random_seed")
        try:
            payload = read(lambda s: lab_experiment.run_experiment(
                s, dataset, task, refs=refs, params=params,
                created_by=actor.get("username"),
                seed=int(seed_value) if seed_value is not None else None))
        except Exception as exc:
            st.error(f"The runner refused to start: {type(exc).__name__}: {exc}",
                     icon="🛑")
        else:
            st.session_state["np_lab_run"] = {
                "meta": payload["meta"],
                "result": payload["result"],
                "task": task,
                "row_id": payload["row_id"],
                "label": _dataset_label(),
            }
            flash(f"Experiment {payload['meta']['experiment_uuid']} finished "
                  f"with status `{payload['meta']['status']}` "
                  f"({payload['meta']['runtime_ms']} ms, "
                  f"row #{payload['row_id']}).",
                  level="success" if payload["result"]["status"] == "ok"
                  else "warning")
            st.rerun()

    # ==================================================================== STEP 5
    st.divider()
    _sec("5️⃣ Evaluate results")
    run = st.session_state.get("np_lab_run")
    if not run or run["task"] != task:
        st.info("No stored run for this task yet - configure it and press "
                "**Run experiment**. Results above this line are never "
                "pre-filled.", icon="▶️")
    else:
        result = run["result"]
        meta = run["meta"]
        header = st.columns([2, 2, 6])
        with header[0]:
            st.markdown(theme.status_pill(
                f"status: {result['status']}", _tone_for(result["status"])),
                unsafe_allow_html=True)
            st.markdown(theme.status_pill(
                "synthetic data" if meta["is_synthetic"] else
                ("operator-supplied" if meta["source_type"] in
                 ("uploaded", "historical") else "collected data"),
                "warn" if meta["is_synthetic"] else "muted"),
                unsafe_allow_html=True)
        with header[1]:
            st.metric("Runtime", f"{meta['runtime_ms']:,} ms")
            evaluated = (len(meta["series"]) or
                         meta["sample_counts"]["series"])
            st.metric("Series / rows in scope",
                      f"{evaluated} / {_fmt(meta['sample_counts']['rows'])}")
        with header[2]:
            st.caption(f"Experiment `{meta['experiment_uuid']}` · "
                       f"dataset fingerprint `{meta['dataset_fingerprint']}` · "
                       f"source `{meta['source_type']}` "
                       f"(`{meta['source_id'] or 'n/a'}`) · labels "
                       f"`{meta['label_kind']}` · seed "
                       f"{meta['seed'] if meta['seed'] is not None else 'not used'}")
            st.caption(result["message"] or "")
            if meta["series_selection"] != "operator-selected":
                st.warning(meta["series_selection"], icon="✂️")
        _metrics_grid(result["metrics"])
        if meta["label_kind"] == "none" and task in (
                lab_dataset.TASK_ANOMALY, lab_dataset.TASK_CLASSIFICATION):
            st.info("No ground-truth labels in this source: supervised "
                    "accuracy/Precision/Recall are reported as null instead of "
                    "invented, and only unsupervised or descriptive numbers "
                    "appear.", icon="🏷️")

        _sec("Comparison table")
        table = _table_frame(result["table"])
        if table is None:
            st.info("The run produced no comparison rows - it refused or had "
                    "nothing to compare, and that is the honest empty state.",
                    icon="🫙")
        else:
            st.dataframe(table, hide_index=True, width="stretch",
                         height=min(460, 40 + 28 * len(table)))

        _sec("Charts computed from this run")
        _render_charts(result, task)

        if result["warnings"]:
            _sec("How to read these numbers")
            for warning in result["warnings"]:
                st.warning(warning, icon="⚠️")
        _json_expander("Protocol actually applied (split / preprocessing / "
                       "task config)", {
                           "split": meta["split"],
                           "preprocessing": meta["preprocessing"],
                           "features given to the model": meta["features"],
                           "target": meta["target"],
                           "requested parameters": meta["params"],
                           "task config": result["config"],
                       })
        _json_expander("Run artifacts and ingest warnings", {
            "artifacts": result["artifacts"],
            "dataset warnings": meta["dataset_warnings"],
            "points persisted with the row": meta["points_stored"],
            "chart payload size": meta["points_json_chars"],
        })

    # ==================================================================== STEP 6
    st.divider()
    _sec("6️⃣ Export report")
    _export_block(dataset, _dataset_label())

# ==================================================================== HISTORY
st.divider()
_sec("📜 Experiment history (persisted, additive ``lab_experiments`` table)")
hist_task = st.selectbox("Filter by task",
                         ["(all)"] + list(lab_dataset.TASKS),
                         key="np_lab_hist_task")
hist_source = st.selectbox("Filter by source",
                           ["(all)"] + list(lab_dataset.SOURCE_TYPES),
                           key="np_lab_hist_source")
rows = read(lambda s: lab_experiment.history(
    s, task=None if hist_task == "(all)" else hist_task,
    source_type=None if hist_source == "(all)" else hist_source))
if not rows:
    st.info("No experiments stored yet - a run (including a refusal) is "
            "recorded as soon as it finishes.", icon="🗂️")
else:
    listing = []
    for row in rows:
        listing.append({
            "uuid": row["experiment_uuid"],
            "created (UTC)": row["created_at"].isoformat(timespec="seconds"),
            "task": row["task"],
            "source": row["source_type"],
            "synthetic": "yes" if row["is_synthetic"] else "no",
            "status": row["status"],
            "rows": _fmt(row["n_rows"]),
            "series": _fmt(row["n_series"]),
            "runtime ms": _fmt(row["runtime_ms"]),
            "points stored": "yes" if row["points_stored"] else "no",
            "by": row["created_by"] or "-",
        })
    st.dataframe(pd.DataFrame(listing), hide_index=True, width="stretch")
    pick = st.selectbox("Open a stored experiment",
                        [row["experiment_uuid"] for row in rows],
                        format_func=lambda value: next(
                            f"{item['created (UTC)']} · {item['task']} · "
                            f"{item['status']} · {value[:8]}"
                            for item, original in zip(listing, rows)
                            if original["experiment_uuid"] == value),
                        key="np_lab_hist_pick")
    detail = read(lambda s: lab_experiment.get_experiment(s, pick))
    if not detail:
        st.error("That experiment could not be re-read from the database.",
                 icon="❌")
    else:
        payload = detail["json"]
        st.markdown(
            f"- **task** `{detail['task']}` · **status** `{detail['status']}` "
            f"· **schema** `{detail['schema_version']}` "
            f"(known: {detail['schema_version_known']})\n"
            f"- **source** `{detail['source_type']}` / "
            f"`{detail['source_id'] or 'n/a'}` · "
            f"**synthetic** {_fmt(bool(detail['is_synthetic']))}\n"
            f"- **fingerprint** `{detail['dataset_fingerprint']}` · "
            f"**labels** `{detail['label_kind']}`\n"
            f"- **time range** {detail['range_start']} → "
            f"{detail['range_end']} · **rows** {_fmt(detail['n_rows'])} / "
            f"**values** {_fmt(detail['n_values'])} / "
            f"**series** {_fmt(detail['n_series'])}\n"
            f"- **target** `{detail['target'] or 'none'}` · "
            f"**seed** {detail['seed'] if detail['seed'] is not None else 'not used'} "
            f"· **runtime** {_fmt(detail['runtime_ms'])} ms",
            unsafe_allow_html=False)
        st.markdown(f"_{detail['message'] or '(no message)'}_")
        if payload.get("parse_errors"):
            st.error(f"Stored JSON could not be parsed for: "
                     f"{', '.join(payload['parse_errors'])}. Nothing was "
                     f"guessed in its place.", icon="🧾")
        st.dataframe(pd.DataFrame([_flat_dict(payload.get("metrics") or {})],
                                  columns=list(
                                      _flat_dict(payload.get("metrics") or {}))),
                     hide_index=True, width="stretch")
        stored_table = _table_frame(payload.get("table") or [])
        if stored_table is None:
            st.caption("The stored row has no comparison table.")
        else:
            st.dataframe(stored_table, hide_index=True, width="stretch")
        if not detail["points_stored"]:
            st.caption("The chart payload was not persisted with this row (it "
                       "exceeded the stored budget), so the history view shows "
                       "numbers without plots - exactly as recorded.")
        elif payload.get("points"):
            st.caption(f"{len(payload['points'])} stored chart payload entr(y/"
                       "ies) - plots are re-drawn only for the run currently "
                       "held in this session.")
        _json_expander("Stored protocol / config / warnings", {
            "features": payload.get("features"),
            "split": payload.get("split"),
            "preprocessing": payload.get("preprocessing"),
            "config": payload.get("config"),
            "warnings": payload.get("warnings"),
        })
        from database.models import db_utcnow as _now
        stamp = (detail["created_at"] or _now()).strftime("%Y%m%dT%H%M%SZ")
        restored = lab_experiment.restore_result({
            "task": detail["task"],
            "status": detail["status"],
            "message": detail["message"] or "",
            "metrics": payload.get("metrics") or {},
            "table": payload.get("table") or [],
            "points": payload.get("points") or [],
            "warnings": payload.get("warnings") or [],
            "config": (payload.get("config") or {}).get("task_config") or {},
            "effective_features": list(payload.get("features") or []),
        })
        colA, colB = st.columns(2)
        colA.download_button(
            "Download stored table CSV",
            data=lab_experiment.export_table_csv(restored),
            file_name=f"netpilot_lab_history_{detail['experiment_uuid'][:8]}"
                      f"_{stamp}_table.csv", mime="text/csv",
            key="np_lab_hist_table_csv", width="stretch")
        colB.download_button(
            "Download stored metrics CSV",
            data=lab_experiment.export_metrics_csv(restored),
            file_name=f"netpilot_lab_history_{detail['experiment_uuid'][:8]}"
                      f"_{stamp}_metrics.csv", mime="text/csv",
            key="np_lab_hist_metrics_csv", width="stretch")
        st.caption("The Markdown/HTML report is produced from the run held in "
                   "this session, because the dataset itself is never "
                   "duplicated into the history row - only the protocol and "
                   "the numbers are.")

st.divider()
st.caption(DISCLAIMER.replace("`", ""))
