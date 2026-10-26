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

from services.app_state import (bootstrap, current_user, demo_banner_if_active,
                                flash, has_role, read, render_flash,
                                sidebar_status)
from ui import theme

bootstrap()
sidebar_status()
theme.page_header(
    "Ops Center",
    "Proactive alerting & topology risk intelligence (v2 · Slice 1)",
    icon="🛰️")
demo_banner_if_active()
render_flash()

from analytics import graph
from config import get_settings
from database import repository
from database.models import ALERT_SEVERITIES
from database.models import CHANGE_RISK_LEVELS
from database.models import USER_ROLES
from services import digital_twin, impact_service
from services import ops_center_actions as ops_actions


def _render_impact_report(report: dict) -> None:
    """Render the read-only B2 impact report before the approval controls."""
    if report.get("topology_is_demo"):
        st.warning("Topology source: " + report["topology_source_label"])
    else:
        st.caption("Topology source: " + report["topology_source_label"])

    status = report.get("analysis_status")
    notes = "; ".join(report.get("analysis_notes") or [])
    if status == impact_service.ANALYSIS_COMPLETE:
        st.success(f"Impact analysis: COMPLETE. {notes}")
    elif status == impact_service.ANALYSIS_PARTIAL:
        st.warning(f"Impact analysis: PARTIAL. {notes}")
    elif status == impact_service.ANALYSIS_INSUFFICIENT:
        st.warning(f"Impact analysis: INSUFFICIENT EVIDENCE. {notes}")
    else:
        st.error(f"Impact analysis: ERROR. {notes}")

    scope = report.get("scope") or {}
    st.markdown(
        f"**What is changing:** `{report.get('device') or '-'}` · "
        f"request `{(report.get('request_uuid') or '-')[:8]}` · "
        f"status `{report.get('status')}` · "
        f"declared risk `{report.get('declared_risk')}` · "
        f"request data source `{report.get('data_source')}`")
    if scope.get("interfaces"):
        st.write("Target interfaces: " + ", ".join(scope["interfaces"]))
    else:
        st.caption("No interface context in the proposed configuration "
                   "(device-level scope).")
    st.caption(
        f"Proposed lines: {scope.get('proposed_line_count', 0)} · "
        f"base lines: {scope.get('base_line_count', 0)} · "
        f"documented dependencies: {scope.get('reachable_device_count', 0)} · "
        f"direct neighbors: {scope.get('direct_neighbor_count', 0)} · "
        "declared risk is human-assigned, not a measured score.")
    if report.get("description"):
        st.caption("Description: " + report["description"])
    if report.get("diff"):
        with st.expander("Proposed diff (what is changing)"):
            st.code(report["diff"], language="diff")

    targets = report.get("direct_targets") or []
    if targets:
        st.markdown("**Directly targeted (confirmed)**")
        st.dataframe(pd.DataFrame([{
            "element": t.get("element"), "kind": t.get("kind"),
            "classification": t.get("classification"),
            "why": t.get("explanation"), "evidence": t.get("evidence"),
            "uncertainty": "; ".join(t.get("uncertainty") or []) or "-",
        } for t in targets]), hide_index=True, width="stretch")
    else:
        st.info("No direct target could be identified from the request.")

    deps = report.get("dependencies") or []
    if deps:
        st.markdown("**Dependent elements (potential / unknown)**")
        st.dataframe(pd.DataFrame([{
            "element": d.get("element"),
            "classification": d.get("classification"),
            "hops": d.get("hop"),
            "path": " → ".join(d.get("path") or []),
            "why": d.get("explanation"),
            "evidence": d.get("evidence"),
            "uncertainty": "; ".join(d.get("uncertainty") or []) or "-",
            "telemetry": (d.get("telemetry") or {}).get("state", "-"),
        } for d in deps]), hide_index=True, width="stretch")
    else:
        st.info("No dependent device is supported by the stored topology.")

    counts = report.get("classification_counts") or {}
    st.caption(
        f"Findings: confirmed={counts.get('confirmed', 0)} · "
        f"potential={counts.get('potential', 0)} · "
        f"unknown={counts.get('unknown', 0)}. Evidence classes: confirmed = "
        "explicitly targeted by the request; potential = documented topology "
        "dependency without an observed failure; unknown = insufficient or "
        "blocked evidence. These are not risk scores.")

    unknowns = report.get("unknown_findings") or []
    if unknowns:
        st.markdown("**Unknown impact / insufficient evidence**")
        for finding in unknowns:
            st.write(
                f"- **{finding.get('element')}** ({finding.get('kind')}): "
                f"{finding.get('explanation')} "
                f"Evidence: {finding.get('evidence')}.")

    telemetry = report.get("telemetry_summary") or {}
    st.caption(
        "Telemetry evidence for this request source: "
        f"{telemetry.get('fresh', 0)} fresh, {telemetry.get('stale', 0)} "
        f"stale, {telemetry.get('missing', 0)} missing, "
        f"{telemetry.get('uncertain', 0)} uncertain. Telemetry never upgrades "
        "a dependency to confirmed impact.")

    if report.get("affected_subnets"):
        st.caption("Documented subnets on direct links: " +
                   ", ".join(report["affected_subnets"]) +
                   " (link subnets only; not a service inventory).")

    with st.expander("Redundancy / alternate paths (structural only)"):
        redundancy = report.get("redundancy") or {}
        st.write(
            "Target is a documented single point of failure: "
            + ("yes" if redundancy.get("target_is_articulation_point")
               else "no"))
        alternates = redundancy.get("alternate_paths") or []
        if alternates:
            st.dataframe(pd.DataFrame([{
                "neighbor": item.get("neighbor"),
                "parallel links": item.get("parallel_links"),
                "alternate path": ("yes" if item.get("alternate_path")
                                   else "no"),
                "basis": item.get("basis"),
                "link statuses": ", ".join(item.get("link_statuses") or []),
                "note": item.get("note"),
            } for item in alternates]), hide_index=True, width="stretch")
        else:
            st.caption("No direct link is available for redundancy analysis.")
        st.caption(redundancy.get("note") or "")

    with st.expander("Missing information, assumptions & limitations"):
        st.markdown("**Missing information**")
        missing = report.get("missing_information") or []
        if missing:
            for item in missing:
                st.write(f"- {item}")
        else:
            st.caption("No missing information was reported by this analysis.")
        st.markdown("**Assumptions**")
        for item in report.get("assumptions") or []:
            st.write(f"- {item}")
        st.markdown("**Routes & services**")
        st.write((report.get("routes_and_services") or {}).get("note", "-"))


def _render_digital_twin_report(report: dict) -> None:
    """Render the read-only D1 simulation result.

    This panel is deliberately informational: it never approves, applies,
    verifies or rolls back a change, and it says so.
    """
    st.markdown("<div class='np-sec'>🧪 Digital Twin simulation (read-only)</div>",
                unsafe_allow_html=True)
    st.caption(
        "Deterministic, isolated simulation over the stored topology. It is "
        "NOT approval, execution, post-change verification, rollback, or a "
        "real-network success.")
    status = report.get("status")
    reasons = "; ".join(report.get("status_reasons") or [])
    if status == digital_twin.STATUS_SIMULATED:
        st.info(
            "Digital Twin: SIMULATED (modeled topology only; not a "
            f"real-network success). {reasons}")
    elif status == digital_twin.STATUS_PARTIAL:
        st.warning(f"Digital Twin: PARTIAL. {reasons}")
    elif status == digital_twin.STATUS_INSUFFICIENT:
        st.warning(f"Digital Twin: INSUFFICIENT EVIDENCE. {reasons}")
    elif status == digital_twin.STATUS_INVALID:
        st.error(f"Digital Twin: INVALID INPUT. {reasons}")
    elif status == digital_twin.STATUS_ERROR:
        st.error(f"Digital Twin: ERROR. {reasons}")
    else:
        st.info(f"Digital Twin: NOT SIMULATED. {reasons}")

    change = report.get("change_ref") or {}
    st.caption(
        f"Topology snapshot: `{report.get('topology_ref') or '-'}` · "
        f"fingerprint `{(report.get('topology_fingerprint') or '-')[:23]}` · "
        f"change `{(change.get('fingerprint') or '-')[:23]}` · "
        f"source `{report.get('topology_source')}` · "
        "structural reachability only.")

    before = report.get("before") or {}
    after = report.get("after") or {}
    st.markdown(
        f"**Before:** {before.get('node_count', 0)} node(s) · "
        f"{before.get('available_edge_count', 0)} available link(s) · "
        f"{len(before.get('reachable_from_target') or [])} reachable from the "
        "target.")
    st.markdown(
        f"**After (simulated):** {after.get('node_count', 0)} node(s) · "
        f"{after.get('available_edge_count', 0)} available link(s) · "
        f"{len(after.get('reachable_from_target') or [])} reachable from the "
        "target.")

    affected = report.get("affected_components") or {}
    lost = affected.get("lost_reachability_from_target") or []
    isolated = affected.get("newly_isolated_devices") or []
    if lost:
        st.error("Predicted loss of documented reachability from the target: "
                 + ", ".join(lost))
    elif status == digital_twin.STATUS_SIMULATED and \
            report.get("topology_effect") != "none":
        st.info("No documented reachability loss was modeled (structural "
                "only; not a safety guarantee).")
    elif status == digital_twin.STATUS_SIMULATED:
        st.info("No topology-affecting operation was modeled.")
    if isolated:
        st.warning("Newly isolated in the simulated topology: "
                   + ", ".join(isolated))
    if affected.get("lost_pairs"):
        st.caption(f"Lost reachable pair(s): {affected['lost_pairs']}.")

    operations = report.get("operations") or []
    if operations:
        with st.expander("Simulated operations"):
            st.dataframe(pd.DataFrame([{
                "line": op.get("line"),
                "operation": op.get("operation"),
                "interface": op.get("interface") or "-",
                "supported": op.get("supported"),
                "effect": op.get("effect"),
                "reason": op.get("reason") or "-",
            } for op in operations]), hide_index=True, width="stretch")
    unsupported = report.get("unsupported_operations") or []
    if unsupported:
        st.warning(
            "Unsupported operations (reported as unknown, never approximated): "
            + ", ".join(str(op.get("line")) for op in unsupported))

    with st.expander("Assumptions, limitations & conflicts"):
        st.markdown("**Assumptions**")
        for item in report.get("assumptions") or []:
            st.write(f"- {item}")
        st.markdown("**Limitations**")
        for item in report.get("limitations") or []:
            st.write(f"- {item}")
        conflicts = report.get("conflicts") or []
        if conflicts:
            st.markdown("**Conflicts / unknown evidence**")
            for item in conflicts:
                st.write(f"- **{item.get('element')}** "
                         f"({item.get('kind')}): {item.get('detail')}")

    st.caption(
        "A simulated result is not proof that a real change is safe. It never "
        "authorizes, approves, applies, verifies or rolls back a change.")


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
            outcome = ops_actions.toggle_alert_rule(options[chosen].id)
            flash(outcome.message, level=outcome.level)
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
            flash("Operator role required to create rules.", level="error")
        else:
            outcome = ops_actions.create_alert_rule(
                name=name, metric=metric,
                device_name=None if device == "(any)" else device,
                condition=condition, threshold=float(threshold),
                severity=severity)
            flash(outcome.message, level=outcome.level)
        st.rerun()

    st.divider()
    if st.button("▶️ Run alert cycle", type="primary",
                 disabled=not rules or not can_operate):
        outcome = ops_actions.run_alert_cycle(data_source)
        flash(outcome.message, level=outcome.level)
        st.rerun()


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
                outcome = ops_actions.acknowledge_alert(by_id[sel].id)
                flash(outcome.message, level=outcome.level)
                st.rerun()
            if b.button("✅ Resolve", disabled=not can_operate):
                outcome = ops_actions.resolve_alert(by_id[sel].id)
                flash(outcome.message, level=outcome.level)
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
            try:
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
                    st.info("Need at least two metrics with stored values to "
                            "align.")
                else:
                    feat = frames[0]
                    for f in frames[1:]:
                        feat = feat.merge(f, on="collected_at", how="inner")
                    feat = feat.sort_values(
                        "collected_at").reset_index(drop=True)
                    X = feat.drop(columns=["collected_at"])
                    ts = feat["collected_at"]
                    if len(X) < 20:
                        st.info(f"Only {len(X)} fully-aligned samples "
                                "(need >= 20).")
                    else:
                        pts = ml_anomaly.detect_multivariate_isolation_forest(
                            X, ts, device_name=dev,
                            data_source=data_source)
                        if pts:
                            st.warning(
                                f"{len(pts)} jointly-unusual sample(s) "
                                "flagged.")
                            st.dataframe(pd.DataFrame([
                                {"timestamp": p.timestamp,
                                 "detector": p.detector,
                                 "severity": p.severity,
                                 "anomaly_score": round(p.deviation, 4),
                                 "explanation": p.explanation} for p in pts]),
                                hide_index=True, width="stretch")
                        else:
                            st.success("No correlated deviation found in the "
                                       "aligned window.")
            except Exception as exc:  # noqa: BLE001 - read-only UI action
                st.error("Multivariate check failed: "
                         f"{type(exc).__name__}: {str(exc)[:200]}")


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
    authenticated = cu is not None
    principal = cu["username"] if cu else None
    if not authenticated:
        st.info(
            "Open mode: no signed-in identity. A proposal is recorded without "
            "a requester identity, and approval is fail-closed because the app "
            "cannot prove the approver is a different person from the "
            "requester. Enable authentication (NETPILOT_AUTH_ENABLED) and sign "
            "in as a distinct operator to approve. Rejection and cancellation "
            "remain available; their actor is recorded only when authenticated.")

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
            "expires (UTC)": (str(r.expires_at) if r.expires_at else "-"),
            "applied": ("yes" if r.applied_at else "no"),
            "updated (UTC)": r.updated_at,
        } for r in reqs]), hide_index=True, width="stretch")

        # B2: the read-only impact report is rendered BEFORE the approval
        # controls so the decision-maker can review it in the same run.
        st.markdown("<div class='np-sec'>🧭 Topology impact analysis</div>",
                    unsafe_allow_html=True)
        st.caption("Read-only analysis of the stored topology. It does not "
                   "connect to devices or apply configuration. Review it "
                   "before approving a change.")
        impact_choices = [u for u, r in by_uuid.items()
                          if r.status in ("validated", "approved")]
        impact_selected = None
        if impact_choices:
            sel_i = st.selectbox(
                "Impact analysis request", impact_choices,
                format_func=lambda u: (
                    f"{u[:8]} · {by_uuid[u].device_name} · "
                    f"{by_uuid[u].status}"),
                key="change_impact_request")
            impact_selected = sel_i
            req_i = by_uuid[sel_i]
            source_label = impact_service.topology_source_label(
                get_settings().demo_mode)
            with st.spinner("Analyzing stored topology impact..."):
                report = read(lambda s: impact_service.analyze_request(
                    s, req_i, topology_source=source_label))
            _render_impact_report(report)
            with st.spinner("Simulating the proposed change..."):
                twin_report = read(lambda s: digital_twin.simulate_request(
                    s, req_i, topology_source=source_label))
            _render_digital_twin_report(twin_report)
        else:
            st.info("No validated or approved request is available for "
                    "impact analysis yet.")

        c_approve, c_reject, c_apply, c_cancel = st.columns(4)
        with c_approve:
            validatable = [u for u, r in by_uuid.items()
                           if r.status == "validated"]
            if validatable:
                sel_v = st.selectbox(
                    "Approve (validated)", validatable,
                    format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name}")
                approval_reason = st.text_input(
                    "Approval reason (optional)", key="change_approval_reason")
                st.caption(
                    f"Acting on: {sel_v[:8]} · {by_uuid[sel_v].device_name} · "
                    f"{by_uuid[sel_v].status}")
                mismatch_v = (impact_selected is not None
                              and impact_selected != sel_v)
                if mismatch_v:
                    st.warning(
                        "The impact report above is for a different request. "
                        "Select this request in 'Impact analysis request' "
                        "before approving.")
                if st.button("✅ Approve",
                             disabled=not can_operate or mismatch_v):
                    outcome = ops_actions.approve_request(
                        sel_v, approved_by=principal,
                        authenticated=authenticated,
                        reason=approval_reason or None)
                    flash(outcome.message, level=outcome.level)
                    st.rerun()
            else:
                st.caption("Nothing awaiting approval.")
        with c_reject:
            rejectable = [u for u, r in by_uuid.items()
                          if r.status == "validated"]
            if rejectable:
                sel_r = st.selectbox(
                    "Reject (validated)", rejectable,
                    format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name}")
                rejection_reason = st.text_input(
                    "Rejection reason (required)",
                    key="change_rejection_reason")
                st.caption(
                    f"Acting on: {sel_r[:8]} · {by_uuid[sel_r].device_name} · "
                    f"{by_uuid[sel_r].status}")
                mismatch_r = (impact_selected is not None
                              and impact_selected != sel_r)
                if mismatch_r:
                    st.warning(
                        "The impact report above is for a different request. "
                        "Select this request in 'Impact analysis request' "
                        "before rejecting.")
                if st.button("❌ Reject",
                             disabled=not can_operate or mismatch_r):
                    outcome = ops_actions.reject_request(
                        sel_r, rejected_by=principal,
                        authenticated=authenticated,
                        reason=rejection_reason or None)
                    flash(outcome.message, level=outcome.level)
                    st.rerun()
            else:
                st.caption("Nothing awaiting rejection.")
        with c_apply:
            approved = [u for u, r in by_uuid.items()
                        if r.status == "approved"]
            if approved:
                sel_a = st.selectbox(
                    "Apply (approved)", approved,
                    format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name}")
                st.caption(
                    f"Acting on: {sel_a[:8]} · {by_uuid[sel_a].device_name} · "
                    f"{by_uuid[sel_a].status}")
                mismatch_a = (impact_selected is not None
                              and impact_selected != sel_a)
                if mismatch_a:
                    st.warning(
                        "The impact report above is for a different request. "
                        "Select this request in 'Impact analysis request' "
                        "before applying.")
                if st.button("🚀 Apply",
                             disabled=not can_operate or mismatch_a):
                    # No executor is injected: this path must refuse.
                    outcome = ops_actions.apply_request(sel_a)
                    flash(outcome.message, level=outcome.level)
                    st.rerun()
            else:
                st.caption("Nothing approved and ready to apply.")
        with c_cancel:
            pending = [u for u, r in by_uuid.items()
                       if r.status in ("draft", "proposed", "validated",
                                       "approved")]
            if pending:
                sel_c = st.selectbox(
                    "Cancel", pending,
                    format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name}")
                cancel_reason = st.text_input(
                    "Cancellation reason (optional)",
                    key="change_cancel_reason")
                st.caption(
                    f"Acting on: {sel_c[:8]} · {by_uuid[sel_c].device_name} · "
                    f"{by_uuid[sel_c].status}")
                if st.button("🚫 Cancel", disabled=not can_operate):
                    outcome = ops_actions.cancel_request(
                        sel_c, cancelled_by=principal,
                        authenticated=authenticated,
                        reason=cancel_reason or None)
                    flash(outcome.message, level=outcome.level)
                    st.rerun()
            else:
                st.caption("Nothing pending that can be cancelled.")

        chosen = st.selectbox(
            "Inspect a request", list(by_uuid),
            format_func=lambda u: f"{u[:8]} · {by_uuid[u].device_name} · "
                                  f"{by_uuid[u].status}")
        req = by_uuid[chosen]
        meta_a, meta_b, meta_c = st.columns(3)
        meta_a.caption(f"Expires (UTC): {req.expires_at or 'never'}")
        meta_b.caption(f"Approval reason: {req.approval_reason or '-'}")
        meta_c.caption(f"Rejection reason: {req.rejection_reason or '-'}")
        if req.cancelled_at or req.cancellation_reason:
            st.caption(
                f"Cancelled by {req.cancelled_by or 'unauthenticated actor'} "
                f"at {req.cancelled_at or '-'} (UTC): "
                f"{req.cancellation_reason or '-'}")
        if req.expired_at:
            st.caption(f"Expired at {req.expired_at} (UTC).")
        if req.diff:
            with st.expander("Proposed diff"):
                st.code(req.diff, language="diff")
        if req.validation_json:
            with st.expander("Deterministic validation findings"):
                parsed, error = ops_actions.parse_stored_json(
                    req.validation_json)
                if error:
                    st.warning(error)
                    st.code(str(req.validation_json)[:4000])
                else:
                    st.json(parsed)
        with st.expander("Audit trail"):
            events = read(lambda s: repository.get_change_audit_events(
                s, req.id))
            if events:
                st.dataframe(pd.DataFrame([{
                    "at (UTC)": e.at, "action": e.action,
                    "actor": e.actor or "-",
                    "authenticated": "yes" if e.actor_authenticated else "no",
                    "from": e.from_status or "-",
                    "to": e.to_status or "-",
                    "reason": e.reason or "-",
                } for e in events]), hide_index=True, width="stretch")
            else:
                st.caption("No audit events recorded for this request.")
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
            flash("Operator role required to propose changes.", level="error")
        else:
            base_lines = [ln for ln in base.splitlines() if ln.strip()]
            prop_lines = [ln for ln in prop.splitlines() if ln.strip()]
            outcome = ops_actions.propose_change(
                device_name=dev, base_lines=base_lines,
                proposed_lines=prop_lines, data_source=data_source,
                description=desc or None, proposed_by=principal,
                proposer_authenticated=authenticated, risk_level=risk)
            flash(outcome.message, level=outcome.level)
        st.rerun()


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
        outcome = ops_actions.create_user(
            username=uname, password=upw, role=urole,
            iterations=get_settings().pbkdf2_iterations)
        flash(outcome.message, level=outcome.level)
        st.rerun()
