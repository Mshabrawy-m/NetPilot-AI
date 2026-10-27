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

from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from services.app_state import (bootstrap, demo_banner_if_active, read,
                                sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header("Forecasting",
                  "Chronological evaluation, calibrated bands, model selection",
                  icon="🔮")
demo_banner_if_active()

from analytics.forecasting import FORECAST_MODELS
from analytics import backtest
from analytics.time_series import dedupe_frame, load_metric_frame
from config import get_settings
from database import repository
from database.models import db_utcnow
from services import forecast_service

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
avail = read(lambda s: repository.get_available_metrics(s))
if not avail:
    st.info("No measured values stored yet - nothing to forecast.")
    st.stop()
sources = sorted({src for (_d, _m, _u, src) in avail})
data_source = st.selectbox("Data source", sources,
                           format_func=lambda v: f"{v} " + (
                               "(synthetic demo data)" if v == "demo"
                               else "(virtual lab measurements)"))
devices = sorted({d for (d, _m, _u, s) in avail if s == data_source})
device_name = st.selectbox("Device", devices)
metrics = sorted({m for (d, m, _u, s) in avail
                  if s == data_source and d == device_name and m})
if not metrics:
    st.warning(f"No stored metric values for **{device_name}** "
               f"({data_source}).", icon="📭")
    st.stop()
metric = st.selectbox("Target metric", metrics)

rows = read(lambda s: repository.get_metric_series(
    s, device_name, metric, data_source=data_source))
ifaces = sorted({r.interface_name for r in rows if r.interface_name})
has_dev = any(r.interface_name is None for r in rows)
scope_options = ([None] if has_dev else []) + ifaces
if not scope_options:
    st.info("This metric has no stored rows under this source.")
    st.stop()
interface = st.selectbox("Series scope", scope_options,
                         format_func=lambda v: v or "(device-level)")

settings = get_settings()
mcol1, mcol2 = st.columns([1, 1])
with mcol1:
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

# ------------------------------------------------- explicit run (not on rerun)
run_btn = st.button("🚀 Run forecast comparison", width="stretch",
                    disabled=not model_names)
if run_btn:
    since = db_utcnow() - timedelta(hours=range_options[range_label])
    with st.spinner("Evaluating and forecasting..."):
        result = read(lambda s: forecast_service.run_forecast_comparison(
            s, device_name=device_name, metric=metric,
            interface_name=interface, data_source=data_source,
            horizon=horizon, models=tuple(model_names),
            device_level_only=(interface is None), since=since,
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
    st.caption("MASE < 1 means the model beats the naive one-step-drift "
               "scale; skill > 0 means it beats the naive baseline on this "
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
    frame = dedupe_frame(read(lambda s: load_metric_frame(
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
        frame = dedupe_frame(read(lambda s: load_metric_frame(
            s, device_name, metric, interface_name=interface,
            device_level_only=(interface is None),
            data_source=data_source)))
        usable = frame.dropna(subset=["value"])
        if len(usable) < 40:
            st.info("Need at least 40 usable samples to backtest this series.")
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
                    "skill vs naive": (round(v["skill_vs_naive"], 3)
                                       if v["skill_vs_naive"] is not None else None),
                    "points": v["n_points"],
                } for m, v in bt["models"].items()]
                st.dataframe(pd.DataFrame(bt_rows), hide_index=True,
                             width="stretch")
                st.success(f"Best baseline over {bt['origins_used']} origins: "
                           f"**{bt['best_model']}** (lowest MAE).")
                if bt["skipped"]:
                    st.caption("Skipped: " + ", ".join(
                        f"{k} ({val})" for k, val in bt["skipped"].items()))
