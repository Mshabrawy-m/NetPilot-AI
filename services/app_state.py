"""Shared helpers for all Streamlit pages (imported by app.py and app_pages/*).

Keeps UI glue in one place: DB initialisation, demo-mode labelling,
scheduler controls and cached reads of lab data. Business logic stays in
services/ and data access in database/repository.
"""
from __future__ import annotations

import threading

import pandas as pd
import streamlit as st

from config import demo_isolation_violation, get_settings
from database import connection, repository, roles
from database.connection import get_session, init_db, require_schema
from database.models import db_utcnow
from services import auth, collection_service, demo_data
from ui import theme

# Demo seeding is guarded process-wide: ``st.session_state`` is per browser
# session, so relying on it alone let every session (and Streamlit's prerun)
# seed independently, and two concurrent seeds fight over SQLite's single
# writer. See ``_ensure_demo_seeded``.
_demo_seed_lock = threading.Lock()
_demo_seed_done = False

# A message rendered in the same script run as ``st.rerun()`` is discarded by
# the rerun before the browser ever paints it. Mutating actions therefore
# queue their confirmation here and the page renders it on the next run.
_FLASH_KEY = "_np_flash_messages"
_FLASH_LEVELS = ("success", "info", "warning", "error")


def flash(message: str, *, level: str = "success") -> None:
    """Queue a message that survives the caller's ``st.rerun()``."""
    if level not in _FLASH_LEVELS:
        raise ValueError(f"unsupported flash level: {level!r}")
    st.session_state.setdefault(_FLASH_KEY, []).append(
        {"level": level, "message": message})


def render_flash() -> None:
    """Render (and clear) messages queued by actions in the previous run."""
    for item in st.session_state.pop(_FLASH_KEY, []):
        getattr(st, item["level"])(item["message"])


def ensure_ready_for_mode() -> None:
    """Run before any database access: isolation guard, role, then schema control.

    Order is the whole point (Stage A3 P1/P2, extended by Stage A6b-1 M4):

    1. ``demo_isolation_violation`` is checked first, so a demo configuration
       aimed at a shared external database raises *before* ``init_db``/engine
       connection or a read-only reflect - no connection is ever opened.
    2. The process then declares itself the ``netpilot_app`` reader role so its
       connections resolve to the application's own URL (never the collector's
       or the administrator's). In demo mode this is a no-op: the isolated
       SQLite store is used unchanged.
    3. ``schema_management_allowed`` decides whether this process may run DDL at
       all. It is False when ``NETPILOT_MANAGE_SCHEMA`` is false, and also False
       for a runtime role once role separation is adopted, so the hosted
       read-only reader only ever verifies the schema (SELECT), never creating
       or migrating it.
    """
    settings = get_settings()
    violation = demo_isolation_violation(settings)
    if violation:
        raise RuntimeError(violation)
    connection.set_database_role(roles.ROLE_APP)
    if connection.schema_management_allowed():
        init_db()
    else:
        require_schema()


def admin_actions_available() -> bool:
    """Whether admin-only destructive DB actions may run in this process.

    ``netpilot_app`` (and any runtime role under separation) holds no DELETE or
    schema privilege, so the reset/purge actions are hidden from it. Demo mode
    keeps them because its SQLite store is a disposable sandbox.
    """
    return connection.admin_actions_available()


def bootstrap() -> None:
    """Run at the top of every page: schema, scheduler, demo seeding."""
    ensure_ready_for_mode()
    settings = get_settings()
    if settings.demo_mode:
        _ensure_demo_seeded()
    st.session_state.setdefault("_scheduler", collection_service.get_scheduler())
    # Apply the shared "Mission Control" styling to every page from one place.
    theme.inject_css()
    theme.apply_plotly_theme()
    # Auth is opt-in (settings.auth_enabled). When disabled this is a no-op,
    # preserving the open Phase 1-3 behaviour and the test/smoke suite.
    if get_settings().auth_enabled:
        _ensure_bootstrap_admin()
        if not current_user():
            _login_gate()


def _ensure_demo_seeded() -> None:
    """Seed the demo store exactly once per process, under a lock.

    The old guard used only ``st.session_state``, which is per browser
    session: every new session (and Streamlit's prerun) re-seeded, and two
    concurrent seeds contend for SQLite's single writer and can fail with
    "database is locked" (or duplicate/clobber rows, since seeding purges
    first). A process-wide lock plus flag guarantees a single seeding; the
    per-session flag is kept as a cheap short-circuit.
    """
    global _demo_seed_done
    if _demo_seed_done or st.session_state.get("_demo_seeded"):
        st.session_state["_demo_seeded"] = True
        return
    with _demo_seed_lock:
        if not _demo_seed_done:
            demo_data.seed_demo_data()
            _demo_seed_done = True
    st.session_state["_demo_seeded"] = True


# --------------------------------------------------------------------------
# Authentication gate (active only when settings.auth_enabled is True)
# --------------------------------------------------------------------------
def current_user():
    """The signed-in principal as a small dict, or None."""
    return st.session_state.get("_np_user")


def has_role(minimum: str) -> bool:
    """Role check for action gating. Open mode (auth off) allows everything."""
    if not get_settings().auth_enabled:
        return True
    user = current_user()
    if user is None:
        return False
    return auth.role_at_least(user["role"], minimum)


def _ensure_bootstrap_admin() -> None:
    """Create the configured admin on first run (no plaintext logged)."""
    settings = get_settings()
    if not (settings.auth_bootstrap_admin_user and
            settings.auth_bootstrap_admin_password):
        return
    session = get_session()
    try:
        if repository.count_users(session) == 0:
            ok, _msg = auth.password_policy(settings.auth_bootstrap_admin_password)
            if ok:
                repository.create_user(
                    session,
                    username=settings.auth_bootstrap_admin_user,
                    password_hash=auth.hash_password(
                        settings.auth_bootstrap_admin_password,
                        settings.pbkdf2_iterations),
                    role="admin")
                session.commit()
    finally:
        session.close()


def _login_gate() -> None:
    """Render the sidebar sign-in form and block page content until authed."""
    with st.sidebar.form("np_login", clear_on_submit=True):
        st.markdown("#### 🔐 Sign in to NetPilot")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        session = get_session()
        try:
            user = auth.authenticate(session, username, password)
            principal = ({"username": user.username, "role": user.role}
                         if user is not None else None)
            if principal:
                session.commit()
        finally:
            session.close()
        if principal:
            st.session_state["_np_user"] = principal
            st.rerun()
        else:
            st.sidebar.error("Invalid username or password.")
    st.stop()


def demo_banner_if_active() -> None:
    if get_settings().demo_mode:
        st.warning(demo_data.DEMO_BANNER, icon="⚠️")


def sidebar_status() -> None:
    """Sidebar: mode, lab/db connectivity and collection controls."""
    settings = get_settings()
    st.sidebar.markdown(
        "<div style='font-weight:750;font-size:1.05rem;color:#e5edf7'>"
        "<span style='color:#55b9e8'>◉</span>&nbsp; NetPilot AI</div>"
        "<div style='font-size:.72rem;color:#9aabc1;margin-top:2px'>"
        "Network operations &amp; analytics</div>",
        unsafe_allow_html=True)
    mode = "🧪 Demo mode" if settings.demo_mode else "🔌 Live lab mode"
    st.sidebar.caption(mode)

    # Signed-in identity + logout (only meaningful when auth is enabled).
    if settings.auth_enabled and current_user():
        user = current_user()
        st.sidebar.caption(
            f"👤 {user['username']} · role: **{user['role']}**")
        if st.sidebar.button("🚪 Sign out", width="stretch"):
            st.session_state.pop("_np_user", None)
            st.rerun()

    from database.connection import check_connection
    from network import containerlab_manager
    db_ok, db_info = check_connection()
    avail = containerlab_manager.availability()
    st.sidebar.caption(
        f"Database: {'🟢' if db_ok else '🔴'} {db_info.split(':///')[0] if db_ok else 'unavailable'}"
    )
    lab_running = any(avail.values()) and avail.get("containerlab", False)
    st.sidebar.caption(
        "Lab (docker/containerlab): "
        f"{'🟢' if lab_running else '🔴'} "
        f"docker={avail['docker']}, containerlab={avail['containerlab']}"
    )

    scheduler: collection_service.CollectionScheduler = st.session_state["_scheduler"]
    if settings.demo_mode:
        # Demo history is seeded data (services/demo_data.py); running the
        # collector would probe real lab hardware from a demo screen.
        st.sidebar.caption("🧪 Demo mode: collector disabled — no live probes.")
    elif not connection.app_collection_allowed() or not lab_running:
        # Two independent reasons the in-app collector cannot write here:
        #   * the app role is SELECT-only (netpilot_app under role separation),
        #     so INSERT into collection_runs would fail with InsufficientPrivilege;
        #   * there is no reachable Containerlab lab to probe.
        # In both cases the external native collector owns collection; this app
        # only reads what it stored. Never start the scheduler or offer a button
        # that would produce a failing write.
        if scheduler.is_running():
            scheduler.stop()
        st.sidebar.info(
            "Live data is collected by the external native collector "
            "(tools/live_collector.py); this app is read-only.")
        try:
            latest_sample = read(lambda s: repository.get_latest_sample_time(
                s, data_source=settings.data_source))
            n_samples = read(lambda s: repository.count_metric_samples(
                s, data_source=settings.data_source))
            last_run = read(lambda s: repository.get_last_successful_run(
                s, data_source=settings.data_source))
        except Exception as exc:  # noqa: BLE001 - status must never break a page
            st.sidebar.caption(f"Collection status unavailable: {type(exc).__name__}")
        else:
            # Freshness comes from the data itself, never from collection_runs:
            # a later run row must not make old samples look current.
            if latest_sample is not None:
                age_seconds = max(
                    0.0, (db_utcnow() - latest_sample).total_seconds())
                age_minutes = int(age_seconds // 60)
                st.sidebar.caption(
                    "Latest live sample: "
                    f"{latest_sample:%Y-%m-%d %H:%M} UTC "
                    f"({age_minutes:,} min ago) · "
                    f"{n_samples:,} sample(s) stored")
                if age_seconds > 15 * 60:
                    st.sidebar.warning(
                        "Live data is stale: the newest stored sample is "
                        f"{age_minutes:,} min old (warning after 15 min).")
            else:
                st.sidebar.caption(
                    f"No live sample stored yet · "
                    f"{n_samples:,} sample(s) stored")
            # The run table is kept as a secondary audit line only - it says
            # what the collector recorded, not how fresh the telemetry is.
            if last_run is not None and last_run.finished_at is not None:
                st.sidebar.caption(
                    "Last recorded run: "
                    f"{last_run.finished_at:%Y-%m-%d %H:%M} UTC "
                    f"(status {last_run.status})")
            else:
                st.sidebar.caption("No collection run recorded yet")
    else:
        col_a, col_b = st.sidebar.columns(2)
        if col_a.button("▶️ Start collector", width="stretch"):
            scheduler.start()
            st.sidebar.caption(f"Collecting every {scheduler.interval}s")
        if col_b.button("⏹ Stop", width="stretch"):
            scheduler.stop()
        if st.sidebar.button("🔄 Collect now", width="stretch"):
            result = scheduler.run_now()
            if isinstance(result, str):
                st.sidebar.warning(result)
            elif result is not None and getattr(result, "status", None) == "error":
                st.sidebar.error(
                    "Collection ran but the lab was unreachable: "
                    + "; ".join(getattr(result, "errors", []) or [])[:200])
            else:
                st.sidebar.success("Collection cycle finished")
        status = scheduler.status()
        st.sidebar.caption(
            f"Scheduler: {'running' if status.running else 'idle'} | "
            f"cycles={status.cycles_completed} | skipped overlaps={status.skipped_overlaps}")


def read(fn):
    """Run a repository query inside a fresh session (UI-side helper)."""
    session = get_session()
    try:
        return fn(session)
    finally:
        session.close()


def samples_to_frame(samples) -> pd.DataFrame:
    rows = [{
        "collected_at": s.collected_at, "device": s.device_name,
        "interface": s.interface_name, "metric": s.metric,
        "value": s.value, "unit": s.unit, "source": s.data_source,
    } for s in samples]
    return pd.DataFrame(rows)
