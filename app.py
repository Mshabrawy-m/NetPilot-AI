"""NetPilot AI entry point and grouped workflow navigation."""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="NetPilot AI", page_icon="🛰️", layout="wide")


def _overview() -> None:
    """Evidence-backed landing page with the most useful network signals."""
    from config import get_settings
    from services.app_state import (
        bootstrap,
        demo_banner_if_active,
        read,
        sidebar_status,
    )
    from ui import theme

    bootstrap()
    sidebar_status()
    theme.page_header(
        "Network overview",
        "Current inventory, observed network health, and collection freshness",
        icon="📊",
    )
    demo_banner_if_active()

    from database import repository, source_scope
    from services import topology_service
    from ui import source_controls

    settings = get_settings()

    from database.models import db_utcnow
    from ui import presentation

    sources = read(lambda session: source_scope.available_sources(session))
    source = source_controls.selector(
        sources, key="np_overview_source", demo_mode=settings.demo_mode)

    devices = read(lambda session: source_scope.filter_devices(
        list(repository.get_devices(session)), source))
    status = topology_service.compute_network_status(devices)
    data_source_value = None if source == source_scope.DEMO else str(source)
    last_run = read(lambda session: repository.get_last_successful_run(
        session, data_source=data_source_value))
    latest_sample = read(lambda session: repository.get_latest_sample_time(
        session, data_source=data_source_value))
    open_anomalies = read(lambda session: repository.get_anomaly_events(
        session, data_source=data_source_value, status="open", limit=50))
    recent_alerts = []
    try:
        from services import alert_service
        recent_alerts = read(lambda session: alert_service.list_alert_events(
            session, data_source=data_source_value, limit=50))
    except Exception:  # noqa: BLE001 - read-only UI guard for optional alert data
        recent_alerts = []

    # --- What needs attention now (read-only, existing data) ---
    findings: list[dict[str, str]] = []
    severity = "OK"
    degraded_devices = sum(1 for d in devices if str(getattr(d, "status", "")).lower() == "degraded")
    if status.down > 0:
        severity = "Problem"
        findings.append({
            "text": f"{status.down} device{'s are' if status.down != 1 else ' is'} down.",
            "page": "app_pages/3_Devices.py", "label": "View devices"})
    if degraded_devices > 0 and severity != "Problem":
        severity = "Watch"
        findings.append({
            "text": f"{degraded_devices} device{'s are' if degraded_devices != 1 else ' is'} degraded.",
            "page": "app_pages/3_Devices.py", "label": "View devices"})
    if open_anomalies:
        severity = "Problem" if len(open_anomalies) >= 3 else ("Watch" if severity == "OK" else severity)
        findings.append({
            "text": f"{len(open_anomalies)} open anomaly event{'s' if len(open_anomalies) != 1 else ''} need review.",
            "page": "app_pages/8_Anomalies.py", "label": "Review anomalies"})
    active_alerts = [a for a in recent_alerts if str(getattr(a, "status", "")).lower() in {"open", "active", "firing", "new"}]
    if active_alerts:
        severity = "Problem"
        findings.append({
            "text": f"{len(active_alerts)} active alert{'s' if len(active_alerts) != 1 else ''} need attention.",
            "page": "app_pages/11_Ops_Center.py", "label": "Open Operations Center"})
    if latest_sample is not None:
        age_seconds = max(0.0, (db_utcnow() - latest_sample).total_seconds())
        if age_seconds > 15 * 60 and severity == "OK":
            severity = "Watch"
            findings.append({
                "text": f"Live data is getting old (last sample {presentation.humanize_age(age_seconds)}).",
                "page": "app_pages/6_System_Status.py", "label": "Check system status"})
    if not findings:
        last_seen = presentation.format_utc(latest_sample) if latest_sample else "no samples yet"
        findings.append({
            "text": f"No issues detected right now. Latest sample: {last_seen}.",
            "page": "app_pages/5_Metrics_History.py", "label": "View metrics"})

    sev_tone = {"OK": "ok", "Watch": "warn", "Problem": "bad"}.get(severity, "muted")
    st.subheader("What needs attention now")
    st.markdown(theme.status_pill(severity, sev_tone), unsafe_allow_html=True)
    for f in findings[:3]:
        cols = st.columns([4, 1])
        cols[0].write(f"- {f['text']}")
        if cols[1].button(f["label"], key=f"attn_{f['label']}_{f['page']}_{f['text'][:20]}"):
            st.switch_page(f["page"])

    tone = {
        "healthy": "ok", "degraded": "warn", "down": "bad", "unknown": "muted",
    }
    label = {
        "healthy": "Healthy", "degraded": "Degraded", "down": "Down", "unknown": "Unknown",
    }
    theme.kpi_row([
        ("Network status", label[status.overall],
         "Based on current device status", tone[status.overall]),
        ("Devices up", f"{status.up} / {status.total}",
         "Current inventory status", "ok" if status.up else "muted"),
        ("Devices down", status.down, "Current inventory status",
         "bad" if status.down else "muted"),
    ])

    if last_run is not None:
        st.caption(
            f"Latest successful collection: {presentation.format_utc(last_run.finished_at, with_seconds=True)} · "
            f"{last_run.devices_up}/{last_run.devices_total} devices up · "
            f"Source: {presentation.source_label(last_run.data_source, demo_mode=settings.demo_mode)}"
        )
    else:
        presentation.empty_state(
            "No collection has been recorded yet",
            "NetPilot has not stored a successful collection for this view.",
            "Open System status to check collection, or use Demo data to explore the dashboard.",
            technical="Developer command: `python tools/live_collector.py --once`",
        )
        if st.button("Open System status", key="ov_sys_status"):
            st.switch_page("app_pages/6_System_Status.py")

    if status.total:
        st.subheader("Device inventory")
        st.dataframe(
            [{"Device": device.name, "Type": device.type, "Status": presentation.friendly_status(device.status),
              "Lab IP": device.lab_ip or "—", "Management IP": device.mgmt_ip or "—"}
             for device in devices],
            hide_index=True, width="stretch",
        )
    else:
        presentation.empty_state(
            "No devices found for this source",
            "There are no device records to show for the selected data source.",
            "Run a collection or load sample data, then refresh this page.",
            technical="Developer command: `python tools/seed_simulated.py`",
        )
        if st.button("Open System status", key="ov_empty_sys"):
            st.switch_page("app_pages/6_System_Status.py")

    if not settings.demo_mode:
        has_rows = read(lambda session: source_scope.has_rows(session, source))
        if not has_rows:
            if source == source_scope.LIVE:
                presentation.empty_state(
                    "No live measurements stored yet",
                    "Live data appears after the collector stores its first successful sample.",
                    "Check System status or start a collection. You can also explore Simulated data.",
                    technical="Developer commands: `python tools/live_collector.py --once` or `python tools/seed_simulated.py`",
                )
            else:
                presentation.empty_state(
                    "No simulated measurements stored yet",
                    "Simulated data is separate from live data and must be loaded before it appears here.",
                    "Load simulated data from System status or ask a developer to seed it.",
                    technical="Developer command: `python tools/seed_simulated.py`",
                )


def _db_diagnostic() -> None:
    """Credential-safe database target diagnostic for hosted deployments.

    Community Cloud redacts exception text from non-owner viewers, which makes a
    live connection failure impossible to debug without dashboard access. This
    page is opt-in (``?diag=1``) and prints only what is safe to publish: the
    resolved target (host/port/database/user/sslmode/pooler/fingerprint), the
    driver name, and a sanitised root-cause chain. It never prints a password,
    a DSN, or any userinfo fragment.
    """
    import os
    import re
    from urllib.parse import quote, quote_plus

    from sqlalchemy.engine import make_url

    from config import get_settings
    from database import connection, roles
    from tools.diagnose_db_target import parse_target

    st.subheader("Database diagnostic")
    settings = get_settings()
    env_var = roles.ROLE_URL_ENV[roles.ROLE_APP]
    st.code(
        f"demo_mode={settings.demo_mode}\n"
        f"manage_schema={settings.manage_schema}\n"
        f"{env_var} present={bool((os.environ.get(env_var) or '').strip())}"
    )

    try:
        url = connection._resolve_engine_url()
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        st.error(f"URL resolution failed: {type(exc).__name__}: {exc}")
        st.stop()

    try:
        st.code(parse_target(url).redacted())
        st.code(f"driver={make_url(url).drivername}")
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        st.error(f"URL parse failed: {type(exc).__name__}")

    password = ""
    try:
        password = make_url(url).password or ""
    except Exception:  # noqa: BLE001 - best-effort redaction only
        password = ""

    def _clean(text: str) -> str:
        if password:
            for variant in {password, quote(password, safe=""), quote_plus(password)}:
                text = text.replace(variant, "***")
        text = re.sub(r"(?i)([a-z][a-z0-9+.\-]*://)[^\s'\"]+", r"\1<redacted>", text)
        text = re.sub(r"(?i)(password\s*[=:]\s*)\S+", r"\1***", text)
        return text[:1500]

    try:
        from services.app_state import ensure_ready_for_mode

        connection.set_database_role(roles.ROLE_APP)
        ensure_ready_for_mode()
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        st.error("Database check failed. Root cause chain:")
        node: BaseException | None = exc
        seen = 0
        while node is not None and seen < 6:
            st.code(f"{type(node).__name__}: {_clean(str(node))}")
            node = node.__cause__ or node.__context__
            seen += 1
    else:
        st.success("Database is reachable and the schema is ready.")

    st.stop()


if st.query_params.get("diag") == "1":
    _db_diagnostic()

pages = st.navigation(
    {
        "Overview": [
            st.Page(_overview, title="Network overview", icon="📊", default=True),
        ],
        "Network operations": [
            st.Page("app_pages/2_Topology.py", title="Topology", icon="🕸️"),
            st.Page("app_pages/3_Devices.py", title="Devices & interfaces", icon="🖥️"),
            st.Page("app_pages/4_Connectivity_Tests.py", title="Connectivity tests", icon="📡"),
            st.Page("app_pages/5_Metrics_History.py", title="Metrics & history", icon="📈"),
            st.Page("app_pages/8_Anomalies.py", title="Anomalies", icon="🚨"),
            st.Page("app_pages/11_Ops_Center.py", title="Operations center", icon="🛎️"),
        ],
        "Analytics": [
            st.Page("app_pages/7_Data_Analysis.py", title="Data Analysis", icon="🔬"),
        ],
        "Agentic Operations": [
            st.Page("app_pages/10_AI_Network_Assistant.py", title="Network assistant & tools", icon="🤖"),
            st.Page("app_pages/13_Evaluation.py", title="Evaluation · safety", icon="🧾"),
        ],
        "System status": [
            st.Page("app_pages/6_System_Status.py", title="System status", icon="🩺"),
        ],
    },
    position="sidebar",
    expanded=True,
)
pages.run()
