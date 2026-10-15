"""Headless UI smoke test using Streamlit's official AppTest harness.

Executes app.py and each page script the same way the Streamlit runtime
would, failing on any uncaught exception (rendered red box in the
browser), then exercises the Phase 2 action buttons (run detection,
run forecast comparison) against the isolated demo database.
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


def press(page: str, label: str) -> bool:
    """Open a page, click the button containing `label`, rerun, check."""
    at = AppTest.from_file(str(ROOT / page), default_timeout=180)
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


actions_ok = (
    press("pages/8_Anomalies.py", "Run detection")
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
