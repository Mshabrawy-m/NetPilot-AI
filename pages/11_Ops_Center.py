"""Page 11 - Ops Center (v2 Slice 1): proactive alerting + graph insights.

Brings the two new engines to the UI:

* **Alerting** - define threshold rules, run an evaluation cycle over the
  latest telemetry for the selected source, and work the fired-event queue
  (open / acknowledge / resolve). Rules and events are stored per data
  source so live and demo provenance never mix.
* **Graph intelligence** - connectivity posture, single points of failure,
  and the blast radius of losing a device or a link, computed from the
  documented topology (never invented).

All data access goes through ``database.repository`` / the services; this
page only presents and triggers actions.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
import json

from services.app_state import (bootstrap, current_user, demo_banner_if_active,
                                has_role, read, sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header(
    "Ops Center",
    "Proactive alerting & topology risk intelligence (v2 · Slice 1)",
    icon="🛰️")
demo_banner_if_active()

from analytics import graph
from config import get_settings
from database import repository
from database.connection import get_session
from database.models import ALERT_SEVERITIES
from database.models import CHANGE_RISK_LEVELS
from database.models import USER_ROLES
from services import alert_service, auth as auth_service, change_service


def _write(fn):
    """Run a repository mutation in its own committed session."""
    session = get_session()
    try:
        result = fn(session)
        session.commit()
        return result
    finally:
        session.close()


# ------------------------------------------------------------------ source
avail = read(lambda s: repository.get_available_metrics(s))
sources = sorted({src for (_d, _m, _u, src) in avail})
default_src = "demo" if get_settings().demo_mode else "live"
data_source = st.selectbox(
    "Data source", sources or [default_src],
    index=(sources.index(default_src) if default_src in sources else 0),
    format_func=lambda v: f"{v} " + (
        "(synthetic demonstration data)" if v == "demo"
        else "(measurements from the virtual lab)" if v == "live" else ""),
)
devices = sorted({d for (d, _m, _u, src) in avail if src == data_source})

tab_alerts, tab_events, tab_graph, tab_insights, tab_change = st.tabs(
    ["🚨 Alert rules", "🔔 Fired alerts", "🕸️ Graph insights",
     "🧠 Insights", "🔁 Change loop"])

# Actions that change state require the 'operator' role. In open mode
# (auth disabled) has_role always returns True, so behaviour is unchanged.
can_operate = has_role("operator")
if not can_operate:
    st.info("Read-only access - sign in with an operator/admin role to "
            "manage alert rules and work the queue.")


# ------------------------------------------------------------------ rules
with tab_alerts:
    rules = read(lambda s: repository.get_alert_rules(s))
    st.markdown("<div class='np-sec'>Existing rules</div>", unsafe_allow_html=True)
    if rules:
        st.dataframe(pd.DataFrame([{
            "name": r.name, "metric": r.metric,
            "device": r.device_name or "(any)",
            "condition": r.condition, "threshold": r.threshold,
            "severity": r.severity, "enabled": r.enabled,
        } for r in rules]), hide_index=True, width="stretch")
        options = {r.name: r for r in rules}
        chosen = st.selectbox("Toggle a rule", list(options))
        if st.button("Enable" if not options[chosen].enabled else "Disable",
                     disabled=not can_operate):
            _write(lambda s: repository.set_alert_rule_enabled(
                s, options[chosen], not options[chosen].enabled))
            st.rerun()
    else:
        st.info("No alert rules yet - create one below.")

    st.divider()
    st.markdown("<div class='np-sec'>New rule</div>", unsafe_allow_html=True)
    metric_opts = sorted({m for (_d, m, _u, src) in avail if src == data_source})
    with st.form("new_rule"):
        name = st.text_input("Rule name")
        col1, col2 = st.columns(2)
        with col1:
            metric = st.selectbox("Metric", metric_opts or [""])
            device = st.selectbox("Device", ["(any)"] + devices)
        with col2:
            condition = st.selectbox("Condition", ["above", "below"])
            threshold = st.number_input("Threshold", value=100.0, format="%.4g")
        severity = st.selectbox("Severity", ALERT_SEVERITIES, index=1)
        submitted = st.form_submit_button("Create rule", type="primary")

    if submitted:
        if not can_operate:
            st.error("Operator role required to create rules.")
        elif not name or not metric:
            st.error("Rule name and metric are required.")
        else:
            try:
                _write(lambda s: repository.create_alert_rule(
                    s, name=name.strip(), metric=metric,
                    device_name=None if device == "(any)" else device,
                    condition=condition, threshold=float(threshold),
                    severity=severity))
                st.success(f"Rule '{name}' created")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    st.divider()
    if st.button("▶️ Run alert cycle", type="primary",
                 disabled=not rules or not can_operate):
        touched = _write(lambda s: alert_service.run_cycle(
            s, data_source=data_source))
        st.success(f"Cycle complete - {len(touched)} series in breach.")


# ------------------------------------------------------------------ events
with tab_events:
    events = read(lambda s: repository.get_alert_events(
        s, data_source=data_source))
    open_count = read(lambda s: repository.count_open_alerts(s))
    theme.kpi_row([
        ("Open alerts", open_count, "unresolved across all sources", "bad"),
        ("This source", len(events), f"events for '{data_source}'", "info"),
    ])
    st.caption("")
    if not events:
        st.info("No alerts fired for this source yet.")
    else:
        df = pd.DataFrame([{
            "id": e.id, "status": e.status, "severity": e.severity,
            "rule": e.rule_name, "device": e.device_name,
            "metric": e.metric, "value": e.value, "threshold": e.threshold,
            "times": e.occurrences, "last_fired": e.last_fired_at,
            "summary": e.summary,
        } for e in events])
        st.dataframe(df, hide_index=True, width="stretch")
        by_id = {e.id: e for e in events}
        targets = [i for i in by_id if by_id[i].status in ("open", "acknowledged")]
        if targets:
            sel = st.selectbox("Select alert", targets,
                               format_func=lambda i: f"#{i} · {by_id[i].rule_name} "
                                                     f"· {by_id[i].status}")
            a, b = st.columns(2)
            if a.button("👌 Acknowledge",
                        disabled=by_id[sel].status != "open" or not can_operate):
                _write(lambda s: repository.acknowledge_alert_event(
                    s, by_id[sel], repository.db_utcnow()))
                st.rerun()
            if b.button("✅ Resolve", disabled=not can_operate):
                _write(lambda s: repository.resolve_alert_event(
                    s, by_id[sel], repository.db_utcnow()))
                st.rerun()


# ------------------------------------------------------------------ graph
with tab_graph:
    g = read(lambda s: graph.build_from_session(s))
    if g.number_of_nodes() == 0:
        st.info("No topology registered yet - deploy the lab or enable demo mode.")
    else:
        summary = graph.connectivity_summary(g)
        spof = graph.single_points_of_failure(g)
        theme.kpi_row([
            ("Devices", summary["nodes"], "", "info"),
            ("Links", summary["edges"], "", "default"),
            ("Components", summary["components"],
             "1 = fully connected", "ok" if summary["components"] == 1 else "warn"),
            ("SPOF nodes", len(spof["articulation_nodes"]),
             "removal disconnects peers", "bad" if spof["articulation_nodes"] else "ok"),
            ("Bridge links", len(spof["bridge_links"]),
             "single path between groups", "warn" if spof["bridge_links"] else "ok"),
        ])
        st.caption("")
        if summary["isolated_nodes"]:
            st.warning("Isolated (no links): " + ", ".join(summary["isolated_nodes"]))

        st.markdown("<div class='np-sec'>Impact analysis</div>", unsafe_allow_html=True)
        node = st.selectbox("Blast radius if this device fails", sorted(g.nodes))
        impact = graph.blast_radius_node(g, node)
        reach = sorted(graph.reachable_from(g, node))
        st.markdown(
            f"**{node}** currently reaches {len(reach)} device(s): "
            f"{', '.join(reach) or 'none'}.")
        if impact["changed"]:
            st.error(
                f"Losing {node} disconnects {impact['lost_pairs']} reachable "
                f"pair(s). Affected: {', '.join(impact['affected'])}"
                + (f" · isolated: {', '.join(impact['isolated'])}"
                   if impact["isolated"] else ""))
        else:
            st.success(f"Losing {node} does not disconnect any reachable pair "
                       "(redundant paths exist).")

        if spof["articulation_nodes"]:
            st.markdown("<div class='np-sec'>Single points of failure</div>",
                        unsafe_allow_html=True)
            st.write(", ".join(spof["articulation_nodes"]))


# ------------------------------------------------------------------ insights
with tab_insights:
    from analytics import ml_anomaly, rca

    st.markdown("<div class='np-sec'>🧭 Root-cause correlation "
                "(open anomalies)</div>", unsafe_allow_html=True)
    st.caption("Groups anomalies that fire close in time on connected devices "
               "and ranks the most probable origin. Correlation only - the "
               "deterministic verifier and human review remain authoritative.")
    open_events = read(lambda s: repository.get_anomaly_events(
        s, data_source=data_source, status="open"))
    g = read(lambda s: graph.build_from_session(s))
    if len(open_events) >= 2:
        ev_dicts = [{"device_name": e.device_name, "metric": e.metric,
                     "id": e.id, "first_seen_at": e.first_seen_at}
                    for e in open_events]
        reports = rca.correlate_root_causes(ev_dicts, g)
        if reports:
            for rep in reports:
                st.markdown(
                    f"**Probable cause: {rep['probable_cause']}** · cluster "
                    f"{', '.join(rep['devices'])}")
                st.caption(rep["rationale"])
                st.dataframe(pd.DataFrame([
                    {"device": c["device"], "score": c["score"],
                     "earliest": c["earliest"].strftime("%H:%M:%S"),
                     "betweenness": round(c["centrality"], 3),
                     "metrics": ", ".join(c["metrics"]) or "-"}
                    for c in rep["candidates"]]),
                    hide_index=True, width="stretch")
        else:
            st.info("No connected, time-correlated anomaly cluster right now "
                    "(the open anomalies are independent).")
    else:
        st.info("Need at least 2 open anomalies for this source to correlate.")

    st.divider()
    st.markdown("<div class='np-sec'>🧠 Multivariate (correlated) check "
                "</div>", unsafe_allow_html=True)
    st.caption("Flags rows where several metrics deviate *together* even when "
               "no single metric crosses its own bound. Live/demo kept apart; "
               "missing values are dropped, never imputed.")
    per_dev: dict[str, set] = {}
    for (d, m, _u, src) in avail:
        if src == data_source:
            per_dev.setdefault(d, set()).add(m)
    mv_devices = sorted([d for d, ms in per_dev.items() if len(ms) >= 2])
    if not mv_devices:
        st.info("No device has two or more stored metrics for this source yet.")
    else:
        dev = st.selectbox("Device", mv_devices)
        if st.button("Run multivariate check"):
            frames = []
            for met in sorted(per_dev[dev]):
                rows = read(lambda s, _met=met: repository.get_metric_series(
                    s, dev, _met, data_source=data_source))
                if not rows:
                    continue
                f = pd.DataFrame(
                    {"collected_at": [r.collected_at for r in rows],
                     met: [r.value for r in rows]}).dropna()
                frames.append(f)
            if len(frames) < 2:
                st.info("Need at least two metrics with stored values to align.")
                st.stop()
            feat = frames[0]
            for f in frames[1:]:
                feat = feat.merge(f, on="collected_at", how="inner")
            feat = feat.sort_values("collected_at").reset_index(drop=True)
            X = feat.drop(columns=["collected_at"])
            ts = feat["collected_at"]
            if len(X) < 20:
                st.info(f"Only {len(X)} fully-aligned samples (need >= 20).")
            else:
                pts = ml_anomaly.detect_multivariate_isolation_forest(
                    X, ts, device_name=dev, data_source=data_source)
                if pts:
                    st.warning(
                        f"{len(pts)} jointly-unusual sample(s) flagged.")
                    st.dataframe(pd.DataFrame([
                        {"timestamp": p.timestamp, "detector": p.detector,
                         "severity": p.severity,
                         "anomaly_score": round(p.deviation, 4),
                         "explanation": p.explanation} for p in pts]),
                        hide_index=True, width="stretch")
                else:
                    st.success("No correlated deviation found in the aligned "
                               "window.")


# ------------------------------------------------------------------ change loop
with tab_change:
    st.caption("Approval-bound, lab-only configuration change workflow. This "
               "dashboard can PROPOSE and APPROVE changes and show the audit; "
               "actual application is disabled here (no executor is injected) "
               "and requires NETPILOT_CHANGE_LOOP_ENABLED plus a virtual-lab "
               "executor on a host that runs the lab. No production device is "
               "ever reachable from this path.")
    loop_on = get_settings().change_loop_enabled
    (st.success if loop_on else st.info)(
        "Change loop ENABLED on this host (apply still needs a lab executor)."
        if loop_on else
        "Change loop is OFF - apply will refuse by design. Set "
        "NETPILOT_CHANGE_LOOP_ENABLED on a lab host to enable application.")

    cu = current_user()
    principal = cu["username"] if cu else "anonymous"

    reqs = read(lambda s: repository.list_change_requests(
        s, data_source=data_source))
    by_uuid = {r.request_uuid: r for r in reqs}
    if reqs:
        st.markdown("<div class='np-sec'>Change requests</div>",
                    unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "id": r.request_uuid[:8], "device": r.device_name,
            "status": r.status, "risk": r.risk_level,
            "proposed_by": r.proposed_by or "-",
            "approved_by": r.approved_by or "-",
            "applied": ("yes" if r.applied_at else "no"),
            "updated (UTC)": r.updated_at,
        } for r in reqs]), hide_index=True, width="stretch")

        c_approve, c_apply, c_cancel = st.columns(3)
        with c_approve:
            validatable = [u for u, r in by_uuid.items() if r.status == "validated"]
            if validatable:
                sel_v = st.selectbox(
                    "Approve (validated)", validatable,
                    format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name}")
                if st.button("✅ Approve", disabled=not can_operate):
                    _req, msg = _write(
                        lambda s: change_service.approve(
                            s, sel_v, approved_by=principal))
                    st.success(msg) if msg == "approved" else st.warning(msg)
                    st.rerun()
            else:
                st.caption("Nothing awaiting approval.")
        with c_apply:
            approved = [u for u, r in by_uuid.items() if r.status == "approved"]
            if approved:
                sel_a = st.selectbox(
                    "Apply (approved)", approved,
                    format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name}")
                if st.button("🚀 Apply", disabled=not can_operate):
                    res = _write(lambda s: change_service.apply(
                        s, sel_a))  # no executor -> always refuses here
                    st.info(res["reason"])
            else:
                st.caption("Nothing approved and ready to apply.")
        with c_cancel:
            pending = [u for u, r in by_uuid.items()
                       if r.status not in ("applied", "failed", "cancelled")]
            if pending:
                sel_c = st.selectbox(
                    "Cancel", pending,
                    format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name}")
                if st.button("🚫 Cancel", disabled=not can_operate):
                    _write(lambda s: change_service.cancel(s, sel_c))
                    st.rerun()

        chosen = st.selectbox(
            "Inspect a request", list(by_uuid),
            format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name} · "
                                  f"{by_uuid[u].status}")
        req = by_uuid[chosen]
        if req.diff:
            with st.expander("Proposed diff"):
                st.code(req.diff, language="diff")
        if req.validation_json:
            with st.expander("Deterministic validation findings"):
                st.json(json.loads(req.validation_json))
    else:
        st.info("No change requests for this source yet.")

    st.divider()
    st.markdown("<div class='np-sec'>Propose a configuration change "
                "(validated only)</div>", unsafe_allow_html=True)
    with st.form("new_change"):
        dev = st.selectbox("Device", devices or [""])
        desc = st.text_input("Description")
        base = st.text_area("Current/base config (one line per row)",
                            value="hostname R1")
        prop = st.text_area("Proposed config (one line per row)",
                            value="hostname R1\ninterface eth0")
        risk = st.selectbox("Risk level", CHANGE_RISK_LEVELS, index=0)
        submit = st.form_submit_button("Validate & record proposal",
                                       type="primary")
    if submit:
        if not can_operate:
            st.error("Operator role required to propose changes.")
        elif not dev:
            st.error("Select a device first.")
        else:
            base_lines = [ln for ln in base.splitlines() if ln.strip()]
            prop_lines = [ln for ln in prop.splitlines() if ln.strip()]
            req = _write(lambda s: change_service.propose(
                s, device_name=dev, base_lines=base_lines,
                proposed_lines=prop_lines, data_source=data_source,
                description=desc or None, proposed_by=principal,
                risk_level=risk))
            (st.success if req.status == "validated" else st.error)(
                f"Proposal recorded as **{req.status}**.")


# --------------------------------------------------------------- access (admin)
# Shown only when authentication is enabled and the signed-in user is an
# admin; invisible in open/demo mode so the smoke suite is unaffected.
if get_settings().auth_enabled and has_role("admin"):
    st.divider()
    st.markdown("<div class='np-sec'>👥 Access control · signed in as "
                f"{current_user()['username']}</div>", unsafe_allow_html=True)
    users = read(lambda s: repository.list_users(s))
    st.dataframe(pd.DataFrame([
        {"username": u.username, "role": u.role, "active": u.is_active,
         "last_login": u.last_login}
        for u in users]), hide_index=True, width="stretch")

    with st.form("new_user"):
        c1, c2, c3 = st.columns(3)
        with c1:
            uname = st.text_input("New username")
        with c2:
            urole = st.selectbox("Role", USER_ROLES, index=0)
        with c3:
            upw = st.text_input("Password", type="password")
        create = st.form_submit_button("Create user", type="primary")
    if create:
        ok, msg = auth_service.password_policy(upw)
        if not uname:
            st.error("Username is required.")
        elif not ok:
            st.error(msg)
        else:
            try:
                _write(lambda s: repository.create_user(
                    s, username=uname.strip(),
                    password_hash=auth_service.hash_password(
                        upw, get_settings().pbkdf2_iterations),
                        role=urole))
                st.success(f"User '{uname}' created as {urole}.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception:
                st.error("Could not create user (name may already exist).")
