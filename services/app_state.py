"""Shared helpers for all Streamlit pages (imported by app.py and pages/*).

Keeps UI glue in one place: DB initialisation, demo-mode labelling,
scheduler controls and cached reads of lab data. Business logic stays in
services/ and data access in database/repository.
"""
from __future__ import annotations

import threading

import pandas as pd
import streamlit as st

from config import get_settings
from database.connection import get_session, init_db
from database import repository
from services import auth, collection_service, demo_data
from ui import theme

# Demo seeding is guarded process-wide: ``st.session_state`` is per browser
# session, so relying on it alone let every session (and Streamlit's prerun)
# seed independently, and two concurrent seeds fight over SQLite's single
# writer. See ``_ensure_demo_seeded``.
_demo_seed_lock = threading.Lock()
_demo_seed_done = False


def bootstrap() -> None:
    """Run at the top of every page: schema, scheduler, demo seeding."""
    st.set_page_config(page_title="NetPilot AI", page_icon="🛰️",
                       layout="wide")
    init_db()
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
        "<div style='font-family:Space Grotesk;font-weight:700;font-size:1.15rem;"
        "letter-spacing:2px;color:#7DD3FC'>NETPILOT</div>"
        "<div style='font-size:.68rem;letter-spacing:3px;color:#8FA1BF;"
        "text-transform:uppercase;margin-top:-4px'>Ops&nbsp;Console&nbsp;·&nbsp;Phases&nbsp;1–3</div>",
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
