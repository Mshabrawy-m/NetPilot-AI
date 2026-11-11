"""Page 9 - Forecasting (chronological no-leak evaluation, calibrated bands).

Models are compared on identical held-out test timestamps (never shuffled,
never leaking future data). Three things are reported side by side so a number
can never stand alone without the evidence behind it:

* **accuracy** - MAE/RMSE plus the scale-free MAPE/sMAPE/MASE and the skill
  against the mandatory naive baseline, all measured on the holdout;
* **uncertainty** - bands re-cut from the errors each model actually made on
  that holdout, with the requested nominal coverage next to the coverage that
  was measured, later checked again against observations that really arrived;
* **selection** - the best model on holdout MAE and an in-memory inverse-MAE
  blend, so "which model" is a measured answer, not a preference.

Evaluation numbers from the SYNTHETIC demo source are clearly labelled as such -
they show the protocol works, not real-network accuracy. Forecasts are persisted
in dedicated tables and never mixed with observations.
"""
from __future__ import annotations

import time
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from services.app_state import bootstrap, demo_banner_if_active, read, sidebar_status
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Forecasting",
                  "Chronological evaluation, calibrated bands, model selection",
                  icon="🔮")
demo_banner_if_active()

from analytics import backtest, derived_metrics, series_prep
from analytics.forecasting import FORECAST_MODELS
from analytics.time_series import (
    dedupe_frame,
    load_metric_frame,
    load_metric_frame_daily,
    load_metric_frame_with_rollups,
)
from config import get_settings
from database import repository
from database.models import db_utcnow
from services import forecast_service
from ui import source_controls

st.caption(
    "Protocol: chronological train|val|test split, models fitted on past data "
    "only, every model scored on the same held-out timestamps. Future "
    "predictions are estimates, not measurements - they are stored separately "
    "from observations and are verified later against what actually happened."
)

BAND_MODES = {
    "Empirical, grown by sqrt(h) (recommended)": "empirical_sqrt_h",
    "Empirical, constant width": "empirical",
    "Normal approximation (model's own)": "normal_sqrt_h",
}


def _pick_series(device: str, metric: str, scope) -> None:
    """Suggestion callback: switch the three selectors before the rerun."""
    st.session_state["np_forecast_device"] = device
    st.session_state["np_forecast_metric"] = metric
    st.session_state["np_forecast_scope"] = scope


@st.cache_data(show_spinner=False, max_entries=24)
def _cached_strong_forecast(series_hash: str, index_ns: tuple,
                            values: tuple, interval_seconds: float,
                            horizon: int, models: tuple, coverage: float,
                            n_folds: int, min_train: int, ensemble_size: int,
                            time_cap_seconds: float, max_lags: int) -> dict:
    """Cache the strong backtest by the prepared series hash.

    ``series_hash`` is the first cache key so two different selections can
    never collide. The values are passed too, which makes the key even stricter
    without changing the documented contract: the hash identifies the series.
    """
    series = pd.Series(list(values), index=pd.to_datetime(list(index_ns)))
    return forecast_service.compute_strong_forecast(
        series, horizon=horizon, models=tuple(models),
        interval_seconds=interval_seconds, coverage=coverage,
        n_folds=n_folds, min_train=min_train, ensemble_size=ensemble_size,
        time_cap_seconds=time_cap_seconds, max_lags=max_lags,
        series_hash=series_hash)


def _fmt(value, digits=3, suffix=""):
    if value is None:
        return "n/a"
    return f"{value:.{digits}g}{suffix}"


def _fmt_eta(seconds) -> str:
    if seconds is None:
        return "interval unknown"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"~{minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"~{hours}h {mins:02d}m"
    days, hours = divmod(hours, 24)
    return f"~{days}d {hours}h"


# ------------------------------------------------------------------- filters
# Stage A6-4: discover candidates from ONE inventory that merges raw samples
# AND daily rollups, so a series whose raw history has been thinned by
# retention (and now lives only as rollups) is still selectable. Demo/Live
# isolation holds because the inventory applies the same data_source filter to
# both sources and rollups are live-only.
inventory = read(lambda s: forecast_service.list_forecast_series(s))
if not inventory:
    st.info("No measured values stored yet - nothing to forecast.")
    st.stop()
sources = sorted({row["data_source"] for row in inventory
                  if row["data_source"]})
data_source = source_controls.selector(
    sources, key="np_forecast_source", demo_mode=get_settings().demo_mode)

# Rank the real inventory so first load lands on the richest usable series,
# never a placeholder and never an alphabetically-first two-sample series.
ranked = forecast_service.rank_forecast_series(inventory,
                                               data_source=data_source)
if not ranked:
    st.warning("Only placeholder series (raw IPs / target labels) are stored "
               "for this source - nothing measured to forecast.", icon="📭")
    st.stop()
devices = list(dict.fromkeys(row["device_name"] for row in ranked))
_previous = st.session_state.get("np_forecast_device")
st.session_state["np_forecast_device"] = (
    _previous if _previous in devices else devices[0])
device_name = st.selectbox("Device", devices, key="np_forecast_device")

metrics = list(dict.fromkeys(
    row["metric"] for row in ranked
    if row["device_name"] == device_name and row["metric"]))
if not metrics:
    st.warning(f"No stored metric values for **{device_name}** "
               f"({data_source}).", icon="📭")
    st.stop()
_previous = st.session_state.get("np_forecast_metric")
st.session_state["np_forecast_metric"] = (
    _previous if _previous in metrics else metrics[0])
metric = st.selectbox("Target metric", metrics, key="np_forecast_metric")

# Series scope comes from the merged inventory, so an interface that survives
# only in rollups is offered just like a raw-only one.
sel_rows = [row for row in ranked if row["data_source"] == data_source
            and row["device_name"] == device_name and row["metric"] == metric]
ifaces = list(dict.fromkeys(row["interface_name"] for row in sel_rows
                            if row["interface_name"]))
has_dev = any(row["interface_name"] is None for row in sel_rows)
scope_options = ([None] if has_dev else []) + ifaces
if not scope_options:
    st.info("This metric has no stored rows under this source.")
    st.stop()
# Default to the scope of the best-ranked row for this selection.
_best_scope = sel_rows[0]["interface_name"]
_previous = st.session_state.get("np_forecast_scope", _best_scope)
if _previous not in scope_options:
    _previous = _best_scope if _best_scope in scope_options else scope_options[0]
st.session_state["np_forecast_scope"] = _previous
interface = st.selectbox("Series scope", scope_options, key="np_forecast_scope",
                         format_func=lambda v: v or "(device-level)")
_scope_row = next((row for row in sel_rows
                   if row["interface_name"] == interface), None)
if _scope_row is not None:
    st.caption(
        f"History coverage: **{_scope_row['coverage']}** - "
        f"{_scope_row['n_raw_samples']} raw sample(s), "
        f"{_scope_row['n_rollup_days']} daily rollup bucket(s).")

settings = get_settings()
mcol1, mcol2 = st.columns([1, 1])
with mcol1:
    cadence_label = st.radio(
        "History cadence",
        ("Native sampling (raw interval)",
         "Daily (rollup-aware, uniform UTC-day)"),
        help="Daily cadence reconciles raw samples and daily rollups onto one "
             "UTC-day grid, so long mixed-granularity history can be forecast "
             "without ever silently blending sampling levels: a day with a "
             "rollup uses its stored daily mean, a day with only raw samples "
             "uses their mean, an empty day is omitted. Native cadence keeps "
             "the raw sampling interval and still refuses a mixed window.")
    cadence = "raw" if cadence_label.startswith("Native") else "daily"
    horizon = st.slider("Forecast horizon (steps)", 3, 48,
                        int(settings.forecast_horizon_default))
    model_names = st.multiselect("Models to compare",
                                 list(FORECAST_MODELS),
                                 default=list(forecast_service.DEFAULT_MODELS),
                                 help="naive is the mandatory baseline. "
                                      "seasonal_naive and holt_winters refuse "
                                      "out loud (with the reason recorded) when "
                                      "the sampling interval or the seasonal "
                                      "history is not defensible - they never "
                                      "produce a quiet wrong answer. ridge_lag "
                                      "needs a longer history.")
with mcol2:
    band_label = st.selectbox("Uncertainty band method", list(BAND_MODES),
                              index=0,
                              help="Empirical bands use the model's own "
                                   "holdout errors instead of an untested "
                                   "normal approximation. The normal option "
                                   "is kept so the difference stays visible.")
    coverage = st.slider("Nominal band coverage (target, measured value "
                         "is reported too)", 0.60, 0.99,
                         float(settings.band_nominal_coverage), 0.05)
    want_ensemble = st.checkbox("Blend the models (inverse-MAE ensemble)",
                                value=True,
                                help="In-memory comparison only: a blend is "
                                     "not stored as a forecast run.")

range_options = {"6 hours": 6, "24 hours": 24, "7 days": 168}
range_label = st.selectbox("History window for the run",
                           list(range_options), index=1)
since = db_utcnow() - timedelta(hours=range_options[range_label])

# Stage A6-4: gate the action on the real data-quality check, so forecasting
# is only enabled on a genuinely forecastable window (not a count estimate).
readiness = read(lambda s: forecast_service.check_series_forecastability(
    s, device_name=device_name, metric=metric, interface_name=interface,
    data_source=data_source, since=since,
    device_level_only=(interface is None), cadence=cadence))
if readiness["ready"]:
    st.success(
        f"Forecastable: {readiness['n_observations']} usable observation(s) "
        f"at {_fmt_eta(readiness['observed_interval_seconds'])} cadence.",
        icon="✅")
else:
    st.warning("Not forecastable yet: "
               + "; ".join(readiness["reasons"]), icon="📉")
    st.caption(
        f"This selection has **{readiness['n_observations']}** usable "
        f"observation(s); a forecast needs at least "
        f"{forecast_service.STRONG_MIN_POINTS}.")
    _alternatives = [
        row for row in ranked
        if (row["device_name"], row["metric"], row["interface_name"])
        != (device_name, metric, interface)
    ][:3]
    if _alternatives:
        st.caption("Best available series by stored usable points "
                   "(click to switch):")
        _cols = st.columns(len(_alternatives))
        for _col, _row in zip(_cols, _alternatives):
            _scope = _row["interface_name"]
            _label = f"{_row['device_name']} · {_row['metric']}"
            if _scope:
                _label += f" · {_scope}"
            _label += f" — {_row['total_points']} pt(s)"
            _col.button(
                _label,
                key=(f"np_fc_suggest_{_row['device_name']}_"
                     f"{_row['metric']}_{_scope or 'device'}"),
                on_click=_pick_series,
                args=(_row["device_name"], _row["metric"], _scope),
                width="stretch")

# ------------------------------------------------- explicit run (not on rerun)
run_btn = st.button("🚀 Run forecast comparison", width="stretch",
                    disabled=(not model_names) or (not readiness["ready"]))
if run_btn:
    with st.spinner("Evaluating and forecasting..."):
        result = read(lambda s: forecast_service.run_forecast_comparison(
            s, device_name=device_name, metric=metric,
            interface_name=interface, data_source=data_source,
            horizon=horizon, models=tuple(model_names),
            device_level_only=(interface is None), since=since,
            cadence=cadence,
            band_mode=BAND_MODES[band_label], band_coverage=coverage,
            ensemble=want_ensemble))
    st.session_state["_fc_result"] = result
    st.session_state["_fc_at"] = db_utcnow()

result = st.session_state.get("_fc_result")
if not result:
    st.info("Choose a selection and press **Run forecast comparison**. "
            "Nothing is computed automatically on page load, keeping the "
            "app responsive.")
    st.divider()
else:
    st.caption(f"Last run executed at {st.session_state['_fc_at']} UTC "
               "(results are persisted; refresh only via the button).")

if result and result["status"] == "insufficient_data":
    st.error("Insufficient data for forecasting: "
             + "; ".join(result["reasons"]), icon="📉")
    st.info("Forecasts need a defensible amount of regular history - "
            "collect more cycles or widen the collector interval "
            "rationally, never fake it.")
elif result:
    runs = result["runs"]
    ok = [r for r in runs if r.get("status") == "ok"]
    ensemble = result.get("ensemble") or {}
    ens_ok = ensemble.get("status") == "ok"
    best_name = result.get("best_model")
    best_row = next((r for r in ok if r["model"] == best_name), None)
    prov = "DEMO (synthetic)" if data_source == "demo" else "lab"

    theme.kpi_row([
        ("Best model (holdout MAE)", best_name or "none",
         f"criterion: {result.get('best_criterion')}", "ok" if best_row else
         "warn"),
        ("Best MAE", _fmt(best_row["mae"]) if best_row else "n/a",
         f"skill vs naive {_fmt(best_row['skill'], 2)}"
         if best_row and best_row["skill"] is not None else
         "no naive reference in this run", "default"),
        ("Models evaluated", f"{len(ok)}/{len(runs)}",
         "refusals are recorded with their reason", "info"),
        ("Holdout samples", ok[0]["n_test"] if ok else 0,
         f"train {ok[0]['n_train'] if ok else 0} · val "
         f"{ok[0]['n_val'] if ok else 0}", "default"),
        ("Band coverage (measured)",
         (f"{(best_row['band']['measured_coverage'] * 100):.0f}%"
          if best_row and best_row["band"]["measured_coverage"] is not None
          else "not measured"),
         f"target {coverage:.0%} · {best_row['band']['kind'] if best_row else 'n/a'}",
         "violet"),
        ("Ensemble MAE", _fmt(ensemble.get("mae")) if ens_ok else "n/a",
         ("beats best member" if ensemble.get("beats_best_member")
          else "blend is not better than the best member")
         if ens_ok else str(ensemble.get("reason") or "disabled")[:38],
         "good" if ens_ok and ensemble.get("beats_best_member") else "muted"),
    ])

    # ------------------------------------------------- evaluation table
    st.subheader("Holdout evaluation (identical test timestamps)")
    ranking = forecast_service.model_ranking(runs, ensemble
                                             if ens_ok else None)
    eval_df = pd.DataFrame([{
        "#": r["rank"], "model": r["model"], "status": r["status"],
        "MAE": r["mae"], "RMSE": r["rmse"], "R²": r["r2"],
        "MAPE %": r["mape"], "sMAPE %": r["smape"], "MASE": r["mase"],
        "skill vs naive": r["skill"], "n_eval": r["n_eval"],
        "band": r["band"], "note": r["reason"] or "",
    } for r in ranking])
    st.dataframe(eval_df, hide_index=True, width="stretch")
    st.caption("MASE < 1 means the error is better than the in-sample "
               "one-step naive error (it is not a comparison with a live naive "
               "forecast); skill > 0 means it beats the naive baseline on this "
               "holdout. 'n/a' is a metric that is not defensible for this "
               "data, not a zero.")
    if best_row:
        st.success(
            f"Selected **{best_row['model']}** on holdout MAE "
            f"(MAE {_fmt(best_row['mae'])}, RMSE {_fmt(best_row['rmse'])}, "
            f"MASE {_fmt(best_row['mase'], 3)}, skill "
            f"{_fmt(best_row['skill'], 2)}) — measured on stored {prov} "
            "samples; not a general accuracy claim.")
    if not ok:
        st.warning("No model could be evaluated on this series - see the "
                   "reasons below. Nothing was forced.", icon="🚫")

    skipped = [r for r in runs if r.get("status") != "ok"]
    if skipped:
        st.markdown("**Models not run (with the honest reason):**")
        for r in skipped:
            st.caption(f"- `{r['model']}`: {r.get('reason') or r.get('status')}")

    # -------------------------------------------- history + predictions
    st.subheader(f"History & forecast — {device_name} · "
                 f"{interface or '(device-level)'} · {metric}")
    prov = result.get("history_provenance") or {}
    run_cadence = result.get("cadence", "raw")
    if run_cadence == "daily":
        st.caption(
            f"Daily cadence: {prov.get('rollup', 0)} day(s) from stored daily "
            f"rollups + {prov.get('raw', 0)} day(s) aggregated from raw "
            "samples, all on one uniform UTC-day grid (rollup preferred where "
            "both exist, empty days omitted). Every point is one UTC-day mean.")
    elif prov.get("rollup"):
        st.caption(
            f"History combines {prov.get('raw', 0)} recent raw sample(s) with "
            f"{prov.get('rollup', 0)} older daily-rollup day(s) "
            "(rollups are the surviving long-term history once raw retention "
            "runs). Daily rollups plot at UTC midnight and carry the day's "
            "mean; they never overlap a day that still has raw samples.")
    history_loader = (load_metric_frame_daily if run_cadence == "daily"
                      else load_metric_frame_with_rollups)
    frame = dedupe_frame(read(lambda s: history_loader(
        s, device_name, metric, interface_name=interface,
        device_level_only=(interface is None),
        data_source=data_source)))
    hist = frame.dropna(subset=["value"])
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=hist["collected_at"], y=hist["value"], mode="lines+markers",
        name="observations"))
    split = result["split"]
    fig.add_vline(x=split.train_end, line_dash="dot",
                  annotation_text="train | val | test →")
    for r in ok:
        if not r.get("future"):
            continue
        fut = pd.DataFrame(r["future"])
        is_best = r["model"] == best_name
        fig.add_trace(go.Scatter(
            x=fut["target_timestamp"], y=fut["predicted_value"],
            mode="lines+markers", name=f"forecast: {r['model']}",
            line=dict(dash="dash", width=3 if is_best else 1)))
        if ("upper_bound" in fut and fut["upper_bound"].notna().all()
                and (is_best or len(ok) == 1)):
            # Band shading is drawn for the selected model only: overlapping
            # error bands read as a single grey blob and convince nobody.
            fig.add_trace(go.Scatter(
                x=fut["target_timestamp"], y=fut["upper_bound"],
                line=dict(width=0), showlegend=False, hoverinfo="skip"))
            band_kind = (r["band"].get("kind") or "")
            fig.add_trace(go.Scatter(
                x=fut["target_timestamp"], y=fut["lower_bound"],
                fill="tonexty", opacity=0.18, line=dict(width=0),
                name=f"band: {r['model']} "
                     f"({coverage:.0%} {band_kind})"))
    if ens_ok and ensemble.get("future"):
        ef = pd.DataFrame(ensemble["future"])
        fig.add_trace(go.Scatter(
            x=ef["target_timestamp"], y=ef["predicted_value"],
            mode="lines+markers", name="ensemble blend",
            line=dict(dash="dot", width=2)))
    fig.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_title="Time (UTC)",
                      yaxis_title=f"{metric}"
                      + (f" [{result['unit']}]" if result.get("unit") else ""))
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Only markers left of the origin are measurements. Bands are "
        f"**{result['band_mode']}**: the half-width is the "
        f"{coverage:.0%} quantile of the errors the model made on its own "
        "holdout (no holdout errors → the documented normal approximation is "
        "kept and labelled as such), never a claimed guarantee.")
    for r in ok:
        if r.get("future_note"):
            st.warning(f"`{r['model']}`: {r['future_note']}", icon="⚠️")

    # ------------------------------------------------ holdout detail
    if ok:
        st.markdown("**What each model would have predicted on the holdout**")
        hfig = go.Figure()
        hfig.add_trace(go.Scatter(
            x=list(split.test.index), y=split.test.to_numpy(),
            mode="lines+markers", name="actual (held out)",
            line=dict(width=2.5)))
        for r in ok:
            hfig.add_trace(go.Scatter(
                x=r["test_timestamps"], y=r["test_pred"],
                mode="lines+markers", name=r["model"],
                line=dict(dash="dot")))
        if ens_ok:
            hfig.add_trace(go.Scatter(
                x=ensemble["test_timestamps"], y=ensemble["test_pred"],
                mode="lines", name="ensemble", line=dict(dash="dash")))
        hfig.update_layout(height=330, margin=dict(l=10, r=10, t=30, b=10),
                           xaxis_title="Holdout time (UTC)",
                           yaxis_title=metric)
        st.plotly_chart(hfig, width="stretch")
        st.caption("Same timestamps for every curve - that is what makes the "
                   "MAE column comparable.")

    # ------------------------------------------------------ ensemble panel
    if ens_ok:
        with st.expander("Ensemble blend (in-memory, not stored)",
                         expanded=False):
            st.dataframe(pd.DataFrame(ensemble["members"]), hide_index=True,
                         width="stretch")
            st.caption(
                f"Weights are inverse holdout MAE with a ridge at 10% of the "
                f"best member's error, so a member that happens to score ~0 "
                f"cannot swallow the blend. Blend MAE "
                f"{_fmt(ensemble.get('mae'))} vs best single member "
                f"{ensemble['best_member']} {_fmt(ensemble['best_member_mae'])}"
                f" → {'the blend helps' if ensemble['beats_best_member'] else 'the blend does NOT beat the best member on this holdout'}. "
                "It is not persisted as a forecast run: a stored run must "
                "stay a real model fit with a real config.")
    elif ensemble:
        st.caption(f"Ensemble: {ensemble.get('reason') or 'not available'}")

    # ------------------------------------- threshold crossing & warnings
    st.markdown("**Forecast-based warnings** (estimates, not breaches)")
    thcol1, thcol2, thcol3 = st.columns(3)
    with thcol1:
        threshold = st.number_input("Threshold on forecasted metric",
                                    value=None, step=1.0,
                                    key="_fc_threshold")
    with thcol2:
        direction = st.radio("Direction", ["above", "below"],
                             horizontal=True, key="_fc_direction")
    with thcol3:
        st.markdown("<span class='np-sec'>Reading the crossing</span>",
                    unsafe_allow_html=True)
        st.caption("`possible` = the point estimate crossed; `certain` = the "
                   "whole band crossed, so the call survives the model's own "
                   "measured error.")
    if threshold is not None:
        crossings = forecast_service.threshold_crossing(
            runs, threshold=float(threshold), direction=direction)
        if crossings:
            st.dataframe(pd.DataFrame([{
                "model": c["model"], "certainty": c["certainty"],
                "first possible step": c["first_possible_step"],
                "first band-clear step": c["first_certain_step"],
                "reported step": c["step"], "at (UTC)": c["target_timestamp"],
                "ETA": _fmt_eta(c["eta_seconds"]),
                "predicted": c["predicted_value"],
                "band": f"{_fmt(c['lower_bound'])} … {_fmt(c['upper_bound'])}",
                "model MAE": c["model_mae"], "band kind": c["band_kind"],
            } for c in crossings]), hide_index=True, width="stretch")
            for c in crossings:
                if c["certainty"] == "certain":
                    st.warning(
                        f"🔒 `{c['model']}` step {c['step']}: the **entire "
                        f"band** is {c['direction']} {threshold:g} at "
                        f"{c['target_timestamp']} UTC ({c['note']}, "
                        f"{_fmt_eta(c['eta_seconds'])} after the origin). "
                        "Still a forecast, not an observed breach.", icon="🔮")
                else:
                    st.info(
                        f" `{c['model']}` point estimate {c['predicted_value']:.3g} "
                        f"is {c['direction']} {threshold:g} at step {c['step']}, "
                        f"but its band still overlaps the threshold "
                        f"(model MAE {_fmt(c['model_mae'])}) — treat as an "
                        "early signal only.")
        else:
            st.info("No forecast crosses the selected threshold within the "
                    "horizon.")
        warns = forecast_service.forecast_warnings(
            runs, threshold=float(threshold), direction=direction)
        st.caption(f"Per-step crossings counted: {len(warns)} "
                   f"({sum(1 for w in warns if w['certainty'] == 'certain')} "
                   "band-clear). Every warning carries the model's holdout MAE "
                   "because the estimate alone is not evidence.")

    # ---------------------------------------- persisted runs & band audit
    with st.expander("Persisted runs, published bands and realized coverage"):
        runs_db = read(lambda s: repository.get_forecast_runs(
            s, device_name=device_name, metric=metric,
            data_source=data_source))
        st.dataframe(pd.DataFrame([{
            "id": r.id, "model": r.model_name, "status": r.status,
            "created (UTC)": r.created_at, "horizon": r.horizon_steps,
            "MAE": r.mae, "RMSE": r.rmse, "MASE": getattr(r, "mase", None),
            "skill": getattr(r, "skill", None), "n_test": r.n_test,
            "origin (UTC)": r.origin_at,
        } for r in runs_db[:20]]), hide_index=True, width="stretch")
        if runs_db:
            audited = [r for r in runs_db if r.status == "ok"][:8]
            cov_rows = []
            for r in audited:
                cov = read(lambda s, rid=r.id:
                           forecast_service.realized_coverage(s, rid))
                cov_rows.append({
                    "run": r.id, "model": r.model_name,
                    "band kind": cov["band_kind"],
                    "nominal": cov["nominal_coverage"],
                    "holdout measured": cov["holdout_measured_coverage"],
                    "verified by real observations": cov["n_checked"],
                    "still unverified": cov["n_unverified"],
                    "realized coverage": cov["coverage"],
                    "gap vs nominal": cov["coverage_gap"],
                    "realized MAE": cov["mae"],
                })
            if cov_rows:
                st.markdown("**Did the published bands hold?** (measured only "
                            "on predictions time has since confirmed)")
                st.dataframe(pd.DataFrame(cov_rows), hide_index=True,
                             width="stretch")
                st.caption("A gap of 0 does not prove calibration: with one or "
                           "two verified points the number is coarse, which is "
                           "why the verified count is shown next to it. "
                           "Unverified steps are never counted as hits.")
            n_unfilled = sum(
                1 for r in runs_db[:5]
                for p in read(lambda s, rid=r.id:
                              repository.get_forecast_predictions(s, rid))
                if p.actual_value is None)
            st.caption(
                f"{n_unfilled} future prediction(s) awaiting real "
                "observations. Actuals are backfilled by the button "
                "below - predictions never alter observations.")
            if st.button("↩️ Backfill actuals for visible runs"):
                total = 0
                for r in runs_db[:20]:
                    total += read(lambda s, rid=r.id:
                                  forecast_service.backfill_actuals(s, rid))
                st.success(f"Backfilled {total} prediction(s) with actual "
                           "observed values. Re-open the coverage table to "
                           "see the bands checked against them.")

st.divider()
with st.expander("📐 Rolling-origin backtest - which baseline would have "
                 "actually been right?"):
    st.caption("Replays many forecast origins over the selected history. Each "
               "model is fit only on data BEFORE each window and scored on the "
               "next steps, so no prediction can see its own future. Reported "
               "honestly: refusals are shown, never back-filled with a guess.")
    if st.button("📉 Run backtest", key="run_backtest"):
        _bt_loader = (load_metric_frame_daily if cadence == "daily"
                      else load_metric_frame_with_rollups)
        frame = dedupe_frame(read(lambda s: _bt_loader(
            s, device_name, metric, interface_name=interface,
            device_level_only=(interface is None),
            data_source=data_source)))
        raw_times = pd.to_datetime(frame["collected_at"])
        raw_deltas = (raw_times.sort_values().diff().dropna()
                      .dt.total_seconds())
        raw_interval = (float(raw_deltas.median())
                        if not raw_deltas.empty else None)
        frame, counter_meta = derived_metrics.forecast_input_frame(
            frame, metric,
            max_gap_seconds=max(float(settings.max_rate_gap_seconds),
                                (raw_interval or 0) * 1.5))
        usable = frame.dropna(subset=["value"])
        if len(usable) < 40:
            st.info("Need at least 40 usable samples to backtest this series.")
            if counter_meta.get("counter"):
                st.caption(
                    f"Counter metric: only {counter_meta['n_usable_intervals']} "
                    f"defensible {counter_meta['rate_metric']} interval(s) were "
                    "derived; reset/wrap and oversized-gap intervals are "
                    "omitted, never turned into rates.")
        else:
            series = pd.Series(usable["value"].to_numpy(dtype=float),
                               index=pd.DatetimeIndex(usable["collected_at"]))
            deltas = series.index.to_series().diff().dropna().dt.total_seconds()
            interval_s = float(deltas.median()) if not deltas.empty else None
            bt = backtest.rolling_origin_backtest(
                series, horizon=6, interval_seconds=interval_s,
                season_hours=settings.season_hours,
                models=("naive", "holt_damped", "seasonal_naive",
                        "holt_winters"))
            if bt["status"] != "ok":
                st.warning("Backtest: " + "; ".join(
                    bt.get("reasons") or [bt["status"]]))
            else:
                bt_rows = [{
                    "model": m,
                    "MAE": round(v["mae"], 4),
                    "RMSE": round(v["rmse"], 4),
                    "MAPE %": (round(v["mape"], 2)
                               if v["mape"] is not None else None),
                    "MAE / naive MAE": (round(v["skill_vs_naive"], 3)
                                        if v["skill_vs_naive"] is not None
                                        else None),
                    "vs naive": v["wins_vs_naive"],
                    "naive perfect": v["naive_perfect"],
                    "skill points": v["skill_eval_points"],
                    "points": v["n_points"],
                } for m, v in bt["models"].items()]
                st.dataframe(pd.DataFrame(bt_rows), hide_index=True,
                             width="stretch")
                st.success(f"Best baseline over {bt['origins_used']} origins: "
                           f"**{bt['best_model']}** (lowest MAE).")
                if counter_meta.get("counter"):
                    st.caption(
                        f"Counter metric: forecasting the per-interval "
                        f"**{counter_meta['rate_metric']}** rate, not the "
                        f"cumulative value. {counter_meta['n_usable_intervals']} "
                        f"usable interval(s); {counter_meta['n_reset_intervals']} "
                        "reset/wrap and "
                        f"{counter_meta['n_gap_intervals']} oversized-gap "
                        "interval(s) omitted.")
                st.caption("MAE / naive MAE is the ratio on the exact points "
                           "both models scored (lower is better); < 0.95 is "
                           "clearly better, 0.95-1.05 is a tie, > 1.05 is "
                           "worse. When the naive baseline has zero error the "
                           "ratio is undefined (naive perfect).")
                if bt["skipped"]:
                    st.caption("Skipped: " + ", ".join(
                        f"{k} ({val})" for k, val in bt["skipped"].items()))

st.divider()
with st.expander("🚀 Strong models - SARIMA + gradient boosting + mean ensemble",
                 expanded=False):
    st.caption(
        "**Data preparation rule (applied before any backtest):** duplicate "
        "timestamps are collapsed (last write wins), then the series is placed "
        "on a fixed grid whose interval is chosen below (default 30 s). A gap "
        "of at most the configured number of steps is linearly interpolated "
        "and labelled as imputed; a longer gap stays NaN forever and ends a "
        "contiguous run. No model, metric or plot ever draws a line across a "
        "long gap - the backtest only trains and scores inside finite runs."
    )
    strong_cols = st.columns(3)
    with strong_cols[0]:
        observed_interval = readiness.get("observed_interval_seconds")
        default_interval = max(
            5, round(observed_interval
                     or series_prep.DEFAULT_INTERVAL_SECONDS))
        strong_interval = st.number_input(
            "Fixed grid interval (seconds)", min_value=5,
            max_value=max(3600, default_interval),
            value=default_interval, step=5, key="strong_interval",
            help="Defaults to the observed sampling interval. The upper bound "
                 "grows when this series is sampled less often than hourly, "
                 "so a sparse series can never make the default invalid.")
        strong_gap = st.number_input(
            "Interpolate gaps up to (steps)", min_value=0, max_value=10,
            value=series_prep.DEFAULT_MAX_GAP_STEPS, step=1, key="strong_gap")
    with strong_cols[1]:
        strong_folds = st.slider("Rolling-origin folds (expanding window)",
                                 3, 10, 5, key="strong_folds")
        strong_models = st.multiselect(
            "Strong models to compare", list(FORECAST_MODELS),
            default=list(forecast_service.STRONG_MODELS), key="strong_models")
    with strong_cols[2]:
        strong_coverage = st.slider(
            "Prediction interval coverage", 0.60, 0.99,
            float(coverage), 0.05, key="strong_coverage")
        strong_ensemble = st.slider("Mean ensemble of best N models", 2, 3, 3,
                                    key="strong_ensemble")
    if st.button("🧪 Run strong backtest & forecast", key="run_strong",
                 disabled=not strong_models):
        strong_frame = dedupe_frame(read(lambda s: load_metric_frame(
            s, device_name, metric, interface_name=interface,
            data_source=data_source, since=since,
            device_level_only=(interface is None))))
        raw_times = pd.to_datetime(strong_frame["collected_at"])
        raw_deltas = (raw_times.sort_values().diff().dropna()
                      .dt.total_seconds())
        raw_interval = (float(raw_deltas.median())
                        if not raw_deltas.empty else None)
        strong_frame, counter_meta = derived_metrics.forecast_input_frame(
            strong_frame, metric,
            max_gap_seconds=max(float(settings.max_rate_gap_seconds),
                                (raw_interval or 0) * 1.5))
        if strong_frame.empty or strong_frame["value"].notna().sum() == 0:
            st.info("No raw observations in the selected window for the "
                    "strong-model path.")
            if counter_meta.get("counter"):
                st.caption(
                    f"Counter metric: no defensible "
                    f"{counter_meta['rate_metric']} interval could be derived "
                    "from the stored counters (resets/wraps and oversized gaps "
                    "are omitted, never turned into rates).")
        else:
            prepared = series_prep.prepare_regular_series(
                strong_frame, interval_seconds=float(strong_interval),
                max_gap_steps=int(strong_gap))
            strong_series = prepared["series"]
            started = time.perf_counter()
            strong_result = _cached_strong_forecast(
                prepared["series_hash"],
                tuple(int(ts.value) for ts in strong_series.index),
                tuple(strong_series.tolist()),
                float(strong_interval), int(horizon),
                tuple(strong_models), float(strong_coverage),
                int(strong_folds), series_prep.MIN_POINTS,
                int(strong_ensemble), 4.0, 24)
            elapsed = time.perf_counter() - started
            st.session_state["_strong_result"] = strong_result
            st.session_state["_strong_series"] = strong_series
            st.session_state["_strong_context"] = {
                "data_source": data_source, "device_name": device_name,
                "metric": metric, "interface_name": interface,
                "n_raw_rows": prepared["n_raw_rows"],
                "n_duplicates": prepared["n_duplicates"],
                "n_points": prepared["n_points"],
                "n_observed": prepared["n_observed"],
                "n_imputed": prepared["n_imputed"],
                "n_missing": prepared["n_missing"],
                "n_long_gaps": prepared["n_long_gaps"],
                "series_hash": prepared["series_hash"],
                "interval_seconds": float(strong_interval),
                "elapsed_seconds": elapsed,
                "counter": counter_meta,
            }
            st.session_state["_strong_at"] = db_utcnow()

    strong_result = st.session_state.get("_strong_result")
    if strong_result:
        ctx = st.session_state.get("_strong_context") or {}
        source_controls.badge(ctx.get("data_source") or data_source)
        st.caption(
            f"Source **{ctx.get('data_source') or data_source}** · "
            f"device **{ctx.get('device_name') or device_name}** · "
            f"metric **{ctx.get('metric') or metric}** · "
            f"series_len **{strong_result.get('series_len')}** (usable points in "
            f"the contiguous run used for the final fit) · "
            f"eval_points **{strong_result.get('eval_points')}** (points scored "
            f"by the chosen model in the backtest) · "
            f"grid **{ctx.get('interval_seconds')} s** · "
            f"raw rows {ctx.get('n_raw_rows')} (duplicates collapsed "
            f"{ctx.get('n_duplicates')}) · imputed {ctx.get('n_imputed')} · "
            f"missing {ctx.get('n_missing')} · long gaps "
            f"{ctx.get('n_long_gaps')} · series hash "
            f"`{str(ctx.get('series_hash'))[:12]}` · "
            f"computed in **{ctx.get('elapsed_seconds', 0):.2f} s**.")
        counter_ctx = ctx.get("counter") or {}
        if counter_ctx.get("counter"):
            st.caption(
                f"Counter metric: the forecast target is the per-interval "
                f"**{counter_ctx['rate_metric']}** rate, not the cumulative "
                f"value. {counter_ctx['n_usable_intervals']} usable interval(s); "
                f"{counter_ctx['n_reset_intervals']} reset/wrap and "
                f"{counter_ctx['n_gap_intervals']} oversized-gap interval(s) "
                "omitted.")
        if strong_result.get("status") != "ok":
            st.error(strong_result.get("message")
                     or "The strong backtest did not produce a result.",
                     icon="📉")
        else:
            table = pd.DataFrame(
                forecast_service.strong_comparison_rows(strong_result)).rename(
                    columns={"skill_vs_naive": "MAE / naive MAE",
                             "wins_vs_naive": "vs naive",
                             "naive_perfect": "naive perfect",
                             "skill_eval_points": "skill points"})
            st.markdown("**Backtest comparison** (rolling origins, expanding "
                        "window, identical scoring points per fold)")
            st.dataframe(table, hide_index=True, width="stretch")
            st.caption("**series_len** = usable points in the contiguous run used "
                       "for the final fit. **eval_points** = points that model "
                       "actually scored across the rolling-origin folds (it can "
                       "exceed series_len because every fold is scored). "
                       "**MAE / naive MAE** is the ratio on the exact points "
                       "both models scored (**skill points**; lower is better): "
                       "< 0.95 clearly better, 0.95-1.05 tie, > 1.05 worse. "
                       "When the naive "
                       "baseline has zero error the ratio is undefined "
                       "(naive perfect). MASE < 1 is better than the in-sample "
                       "one-step naive error of each fold's training window - "
                       "not a live naive forecast. MAPE skips near-zero "
                       "actuals instead of reporting an exploded ratio. "
                       "Intervals are empirical residual quantiles from the "
                       "same backtest.")
            chosen = strong_result.get("chosen_model")
            st.success(
                f"Chosen model: **{chosen}** (criterion: "
                f"{strong_result.get('best_criterion')}; backtest folds: "
                f"{strong_result.get('n_folds')}, contiguous runs: "
                f"{strong_result.get('n_segments')}).")
            if strong_result.get("forecast"):
                fdf = pd.DataFrame(strong_result["forecast"])
                st.dataframe(
                    fdf[["target_timestamp", "step", "predicted_value",
                         "lower_bound", "upper_bound"]],
                    hide_index=True, width="stretch")
                fig = go.Figure()
                hist_series = st.session_state.get("_strong_series")
                if hist_series is not None:
                    hist = hist_series.dropna()
                    fig.add_trace(go.Scatter(
                        x=hist.index, y=hist.to_numpy(), mode="lines",
                        name="regular-grid observations"))
                fig.add_trace(go.Scatter(
                    x=fdf["target_timestamp"], y=fdf["predicted_value"],
                    mode="lines+markers", name=f"forecast: {chosen}",
                    line={"dash": "dash", "width": 3}))
                fig.add_trace(go.Scatter(
                    x=fdf["target_timestamp"], y=fdf["upper_bound"],
                    line={"width": 0}, showlegend=False, hoverinfo="skip"))
                fig.add_trace(go.Scatter(
                    x=fdf["target_timestamp"], y=fdf["lower_bound"],
                    fill="tonexty", opacity=0.18, line={"width": 0},
                    name=f"{float(strong_result.get('coverage', 0.95)):.0%} "
                         "empirical interval"))
                fig.update_layout(
                    height=390, margin={"l": 10, "r": 10, "t": 30, "b": 10},
                    xaxis_title="Time (UTC)", yaxis_title=metric)
                st.plotly_chart(fig, width="stretch")
            else:
                st.warning(strong_result.get("forecast_note")
                           or "No future steps were published.", icon="⚠️")
            notes = strong_result.get("fit_notes") or {}
            if notes:
                st.caption("Models that could not be refit for the future "
                           "(the backtest above is still their real measured "
                           "result): " + "; ".join(
                               f"{name}: {reason}"
                               for name, reason in notes.items()))
