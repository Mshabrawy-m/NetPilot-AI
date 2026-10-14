"""Page 9 - Forecasting (Phase 2 Scope E: baselines + chronological eval).

Models are compared on identical held-out test timestamps (never shuffled,
never leaking future data). Evaluation numbers from the SYNTHETIC demo
source are clearly labelled as such - they show the protocol works, not
real-network accuracy. Forecasts are persisted in dedicated tables and
never mixed with observations.
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
                  "Baselines with chronological (no-leak) evaluation", icon="🔮")
demo_banner_if_active()

from analytics.forecasting import FORECAST_MODELS
from analytics import backtest
from analytics.time_series import dedupe_frame, load_metric_frame
from config import get_settings
from database import repository
from database.models import db_utcnow
from services import forecast_service

st.caption(
    "Forecasting protocol: chronological train|val|test split, models fitted "
    "on past data only, MAE/RMSE computed on the same held-out timestamps "
    "for every compared model. Future predictions are estimates, not "
    "measurements - they are stored separately from observations."
)

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
horizon = st.slider("Forecast horizon (steps)", 3, 48,
                    int(settings.forecast_horizon_default))
model_names = st.multiselect("Models to compare",
                             list(FORECAST_MODELS),
                             default=list(forecast_service.DEFAULT_MODELS),
                             help="naive is the mandatory baseline; "
                                  "seasonal_naive needs >= 2 full days of "
                                  "data; ridge_lag needs a longer history.")

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
            device_level_only=(interface is None), since=since))
    st.session_state["_fc_result"] = result
    st.session_state["_fc_at"] = db_utcnow()

result = st.session_state.get("_fc_result")
if result:
    st.caption(f"Last run executed at {st.session_state['_fc_at']} UTC "
               "(results are persisted; refresh only via the button).")
    if result["status"] == "insufficient_data":
        st.error("Insufficient data for forecasting: "
                 + "; ".join(result["reasons"]), icon="📉")
        st.info("Forecasts need a defensible amount of regular history - "
                "collect more cycles or widen the collector interval "
                "rationally, never fake it.")
    else:
        # ------------------------------------------------- evaluation table
        st.subheader("Holdout evaluation (identical test timestamps)")
        eval_rows = []
        for r in result["runs"]:
            eval_rows.append({
                "model": r["model"],
                "status": r.get("status"),
                "MAE": None if r.get("mae") is None else round(r["mae"], 3),
                "RMSE": None if r.get("rmse") is None else round(r["rmse"], 3),
                "R²": None if r.get("r2") is None else round(r["r2"], 3),
                "n_eval": r.get("n_eval"),
                "n_train": r.get("n_train"),
                "note": r.get("reason") or "",
            })
        st.dataframe(pd.DataFrame(eval_rows), hide_index=True,
                     width="stretch")
        best = [r for r in result["runs"]
                if r.get("status") == "ok" and r.get("rmse") is not None]
        if best:
            winner = min(best, key=lambda r: r["rmse"])
            st.success(f"Lowest RMSE on the holdout window: "
                       f"**{winner['model']}** "
                       f"(RMSE {winner['rmse']:.3g}, MAE "
                       f"{winner['mae']:.3g}) — measured on stored "
                       f"{'DEMO (synthetic)' if data_source == 'demo' else 'lab'} "
                       "samples; not a general accuracy claim.")

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
        for r in result["runs"]:
            if r.get("status") != "ok" or not r.get("future"):
                continue
            fut = pd.DataFrame(r["future"])
            fig.add_trace(go.Scatter(
                x=fut["target_timestamp"], y=fut["predicted_value"],
                mode="lines+markers", name=f"forecast: {r['model']}",
                line=dict(dash="dash")))
            if "upper_bound" in fut and fut["upper_bound"].notna().all():
                # documented ~95% band: upper edge (invisible), then lower
                # edge filled "tonexty" to shade between them
                fig.add_trace(go.Scatter(
                    x=fut["target_timestamp"], y=fut["upper_bound"],
                    line=dict(width=0), showlegend=False,
                    hoverinfo="skip"))
                fig.add_trace(go.Scatter(
                    x=fut["target_timestamp"], y=fut["lower_bound"],
                    fill="tonexty", opacity=0.15, line=dict(width=0),
                    showlegend=False,
                    name=f"band: {r['model']} (approx. 95%)"))
        fig.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10),
                          xaxis_title="Time (UTC)",
                          yaxis_title=f"{metric}"
                          + (f" [{result['unit']}]" if result.get("unit")
                             else ""))
        st.plotly_chart(fig, width="stretch")
        st.caption("Dashed lines + shaded bands are model estimates "
                   "(≈95% band from the model's own training residuals); "
                   "only markers left of the origin are measurements.")

        # ------------------------------------------------ model limitations
        skipped = [r for r in result["runs"]
                   if r.get("status") not in ("ok",)]
        if skipped:
            st.markdown("**Models not run (with the honest reason):**")
            for r in skipped:
                st.caption(f"- `{r['model']}`: {r.get('reason') or r.get('status')}")

        # -------------------------------------------- forecast-based warnings
        st.markdown("**Forecast-based warnings** (estimates, not breaches)")
        thcol1, thcol2 = st.columns(2)
        with thcol1:
            threshold = st.number_input("Threshold on forecasted metric",
                                        value=None, step=1.0,
                                        key="_fc_threshold")
        with thcol2:
            direction = st.radio("Direction", ["above", "below"],
                                 horizontal=True, key="_fc_direction")
        if threshold is not None:
            warns = forecast_service.forecast_warnings(
                result["runs"], threshold=float(threshold),
                direction=direction)
            if warns:
                for w in warns:
                    st.warning(
                        f"⚠️ {w['kind']}: `{w['model']}` step {w['step']} "
                        f"projects {w['predicted_value']:.3g} "
                        f"{w['direction']} {threshold:g} at "
                        f"{w['target_timestamp']} UTC — model holdout MAE "
                        f"{w['model_mae'] if w['model_mae'] is not None else 'n/a'}"
                        " (uncertainty applies). This is NOT an observed "
                        "breach.", icon="🔮")
            else:
                st.info("No forecast point crosses the selected threshold "
                        "within the horizon.")

        with st.expander("Persisted runs & unverified predictions"):
            runs_db = read(lambda s: repository.get_forecast_runs(
                s, device_name=device_name, metric=metric,
                data_source=data_source))
            db_rows = pd.DataFrame([{
                "id": r.id, "model": r.model_name, "status": r.status,
                "created (UTC)": r.created_at, "horizon": r.horizon_steps,
                "MAE": r.mae, "RMSE": r.rmse, "n_test": r.n_test,
                "origin (UTC)": r.origin_at,
            } for r in runs_db[:20]])
            st.dataframe(db_rows, hide_index=True, width="stretch")
            if runs_db:
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
                        total += read(
                            lambda s, rid=r.id: forecast_service
                            .backfill_actuals(s, rid))
                    st.success(f"Backfilled {total} prediction(s) with "
                               "actual observed values.")
else:
    st.info("Choose a selection and press **Run forecast comparison**. "
            "Nothing is computed automatically on page load, keeping the "
            "app responsive.")

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
                models=("naive", "holt_damped", "seasonal_naive"))
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
