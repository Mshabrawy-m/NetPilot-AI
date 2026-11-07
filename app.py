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

    sources = read(lambda session: source_scope.available_sources(session))
    source = source_controls.selector(
        sources, key="np_overview_source", demo_mode=settings.demo_mode)

    devices = read(lambda session: source_scope.filter_devices(
        list(repository.get_devices(session)), source))
    status = topology_service.compute_network_status(devices)
    # Ensure data_source is properly typed: None for demo mode, string otherwise
    data_source_value = None if source == source_scope.DEMO else str(source)
    last_run = read(lambda session: repository.get_last_successful_run(
        session, data_source=data_source_value))

    tone = {
        "healthy": "ok", "degraded": "warn", "down": "bad", "unknown": "muted",
    }
    label = {
        "healthy": "Healthy", "degraded": "Degraded", "down": "Down", "unknown": "Unknown",
    }
    theme.kpi_row([
        ("Network status", label[status.overall],
         "Derived from registered device status", tone[status.overall]),
        ("Devices up", f"{status.up} / {status.total}",
         "Current inventory status", "ok" if status.up else "muted"),
        ("Devices down", status.down, "Current inventory status",
         "bad" if status.down else "muted"),
    ])

    if last_run is not None:
        st.caption(
            f"Latest successful collection · "
            f"{last_run.finished_at:%Y-%m-%d %H:%M:%S} UTC · "
            f"{last_run.devices_up}/{last_run.devices_total} devices up · "
            f"source: {last_run.data_source}"
        )
    else:
        st.info(
            "No successful collection is recorded yet. Start collection from "
            "the sidebar or explore clearly labelled demo data."
        )

    if status.total:
        st.subheader("Device inventory")
        st.dataframe(
            [{"Device": device.name, "Type": device.type, "Status": device.status,
              "Lab IP": device.lab_ip or "—", "Management IP": device.mgmt_ip or "—"}
             for device in devices],
            hide_index=True, width="stretch",
        )
    else:
        st.info(
            "No rows exist yet for the selected source. Devices appear after "
            "the native collector writes a cycle, or after "
            "`python tools/seed_simulated.py` seeds the labelled simulated "
            "source."
        )

    if not settings.demo_mode:
        has_rows = read(lambda session: source_scope.has_rows(session, source))
        if not has_rows:
            if source == source_scope.LIVE:
                st.info(
                    "No live measurements are stored yet. Run the native "
                    "collector (`python tools/live_collector.py --once`) or "
                    "seed the labelled simulated source "
                    "(`python tools/seed_simulated.py`). Containerlab is not "
                    "required for Live Mode.", icon="ℹ️")
            else:
                st.info(
                    "No simulated rows are stored yet. Run "
                    "`python tools/seed_simulated.py` to populate this "
                    "clearly-labelled source.", icon="ℹ️")


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
        "AI lab": [
            st.Page("app_pages/12_AI_Lab.py", title="AI Lab · data & forecasting", icon="🧪"),
            st.Page("app_pages/7_Data_Analysis.py", title="Data Analysis", icon="🔬"),
            st.Page("app_pages/9_Forecasting.py", title="Forecasting", icon="🔮"),
        ],
        "Agentic Operations": [
            st.Page("app_pages/10_AI_Network_Assistant.py", title="Network assistant & tools", icon="🤖"),
        ],
        "System status": [
            st.Page("app_pages/6_System_Status.py", title="System status", icon="🩺"),
        ],
    },
    position="sidebar",
    expanded=True,
)
pages.run()
