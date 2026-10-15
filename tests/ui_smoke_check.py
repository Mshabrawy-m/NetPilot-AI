"""Headless UI smoke test using Streamlit's official AppTest harness.

Executes app.py and each page script the same way the Streamlit runtime
would, failing on any uncaught exception (rendered red box in the
browser), then exercises the Phase 2 action buttons (run detection,
run forecast comparison) against the isolated demo database.

It also guards two bugs that only surfaced in the hosted app:

* page 8 - running detection with a DEVICE-LEVEL metric. ``options`` then
  contains ``None`` (the "(device-level)" scope), so the default-index
  lookup used to raise ``KeyError`` the first time the button was pressed.
* page 10 - the SAME AI run visible in the "New request" and "Run history"
  tabs at once. ``st.tabs`` renders every panel in a single run, so the
  shared widget key raised ``StreamlitDuplicateElementKey``.

Run: .venv/Scripts/python tests/ui_smoke_check.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Demo mode with an isolated throwaway database so the smoke test never
# touches live lab data.
os.environ["NETPILOT_DEMO_MODE"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{(ROOT / 'data' / 'uitest.db').as_posix()}"

from streamlit.testing.v1 import AppTest  # noqa: E402

TARGETS = ["app.py"] + sorted(str(p.relative_to(ROOT))
                              for p in (ROOT / "pages").glob("*.py"))

failed = False
for target in TARGETS:
    at = AppTest.from_file(str(ROOT / target), default_timeout=120)
    at.run()
    exceptions = [e.value for e in at.exception]
    if exceptions:
        failed = True
        print(f"[FAIL] {target}")
        for exc in exceptions:
            print("       ", str(exc)[:400])
    else:
        print(f"[ OK ] {target} "
              f"(warnings={len(at.warning)}, infos={len(at.info)})")

# ------------------------------------------------- Phase 2 action buttons


def press(page: str, label: str, *, select_metric: str | None = None) -> bool:
    """Open a page, optionally pick a metric, click `label`, rerun, check."""
    at = AppTest.from_file(str(ROOT / page), default_timeout=180)
    at.run()
    if select_metric is not None:
        picked = False
        for box in at.selectbox:
            if select_metric in list(box.options or []):
                box.select(select_metric)
                picked = True
                break
        if not picked:
            print(f"[FAIL] {page}: no selectbox offered metric "
                  f"'{select_metric}'")
            return False
        at.run()
    btn = next((b for b in at.button if label in (b.label or "")), None)
    if btn is None:
        print(f"[FAIL] {page}: button '{label}' not rendered")
        return False
    btn.click()
    at.run()
    if at.exception:
        print(f"[FAIL] {page}: after clicking '{label}'")
        for exc in at.exception:
            print("       ", str(exc)[:400])
        return False
    print(f"[ OK ] {page}: '{label}' executed without exceptions")
    return True


def page10_same_run_in_two_tabs() -> bool:
    """Regression: one run rendered in the 'New request' AND 'Run history'
    tabs in the same script run must not collide on a widget key."""
    from database import repository
    from database.connection import get_session, init_db

    init_db()
    run_uuid = "smoke-dupkey-run"
    session = get_session()
    try:
        if repository.get_ai_run_by_uuid(session, run_uuid) is None:
            repository.create_ai_run(
                session, run_uuid=run_uuid, request_text="smoke regression",
                data_source="demo", model="smoke", prompt_version="smoke")
            session.commit()
    finally:
        session.close()

    at = AppTest.from_file(
        str(ROOT / "pages/10_AI_Network_Assistant.py"), default_timeout=180)
    at.session_state["last_ai_run"] = run_uuid
    at.run()
    if at.exception:
        print("[FAIL] pages/10_AI_Network_Assistant.py: same run in two tabs "
              "(duplicate element key)")
        for exc in at.exception:
            print("       ", str(exc)[:400])
        return False
    print("[ OK ] pages/10_AI_Network_Assistant.py: same run in two tabs")
    return True


page8_ok = press("pages/8_Anomalies.py", "Run detection",
                 select_metric="ping.loss_pct")
page10_ok = page10_same_run_in_two_tabs()

actions_ok = (
    page8_ok
    and page10_ok
    and press("pages/9_Forecasting.py", "Run forecast comparison")
    # Regression guard: submitting a change proposal used to raise sqlalchemy
    # DetachedInstanceError, because the page read an ORM attribute after the
    # committing session had closed. See pages/11_Ops_Center.py.
    and press("pages/11_Ops_Center.py", "Validate & record proposal")
)

# Demo banner must be present on the landing page (spec: clearly labelled).
at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
at.run()
banner = any("DEMO MODE" in w.value for w in at.warning)
print("demo banner shown:", "OK" if banner else "MISSING")
sys.exit(1 if (failed or not banner or not actions_ok) else 0)
