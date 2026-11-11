"""Live mode with a SELECT-only app role must never start the collector.

The hosted Streamlit app connects as ``netpilot_app`` once role separation is
enabled. That role may SELECT but holds no INSERT grant, so an in-app
collection cycle can only fail with ``InsufficientPrivilege`` on
``collection_runs``. This test reproduces the hosted shape with the real
service logic:

* role separation is active and the runtime role is ``netpilot_app``;
* the database itself refuses INSERT into ``collection_runs`` (a SQLite
  trigger mirrors the missing PostgreSQL grant);
* Containerlab is reported as available, so the role - not the missing lab -
  is the deciding gate.

The page must render without an exception, must not offer "Start collector"
or "Collect now", must say that collection is external and read-only, and
must report the last successful run plus the stored sample count from
read-only queries.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from database import connection, roles

ROOT = Path(__file__).resolve().parents[1]

READ_ONLY_MESSAGE = (
    "Live data is collected by the external native collector "
    "(tools/live_collector.py); this app is read-only.")


def _texts(at) -> list[str]:
    values: list[str] = []
    for name in ("markdown", "warning", "info", "error", "success",
                 "caption", "title", "header", "subheader", "text"):
        for element in getattr(at, name, []):
            value = getattr(element, "value", None)
            if value is not None:
                values.append(str(value))
    return values


def _seed_and_block_writes(db_url: str) -> int:
    """Seed a successful run + samples, then make INSERT fail like the grant."""
    from database.connection import session_scope
    from database.models import CollectionRun, MetricSample, db_utcnow

    now = db_utcnow()
    with session_scope() as session:
        session.add(CollectionRun(
            started_at=now - dt.timedelta(minutes=5),
            finished_at=now - dt.timedelta(minutes=4),
            status="ok", devices_total=3, devices_up=3,
            data_source="live"))
        session.add_all([
            MetricSample(device_name="R1", metric="ping.avg_rtt_ms",
                         value=1.0 + i, unit="ms", data_source="live",
                         collected_at=now - dt.timedelta(minutes=i))
            for i in range(5)
        ])

    # The trigger is the SQLite analogue of the app role's missing INSERT
    # grant: any attempt to write a collection run aborts immediately.
    with connection.get_engine().begin() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER block_collection_runs "
            "BEFORE INSERT ON collection_runs "
            "BEGIN SELECT RAISE(ABORT, "
            "'permission denied for table collection_runs'); END")
    return 5


def test_select_only_live_mode_never_starts_collection(db_url, monkeypatch):
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    monkeypatch.setenv("NETPILOT_CHANGE_LOOP_ENABLED", "false")
    monkeypatch.setenv("NETPILOT_LAB_EXECUTION_ENABLED", "false")
    # Same database, but now the app role owns the connection and role
    # separation is active. The role is the only reason collection is gated.
    monkeypatch.setenv("NETPILOT_DB_URL_APP", db_url)

    from network import containerlab_manager
    monkeypatch.setattr(containerlab_manager, "availability",
                        lambda: {"docker": True, "containerlab": True})

    n_samples = _seed_and_block_writes(db_url)

    assert roles.role_separation_enabled() is True
    assert connection.active_role() == roles.ROLE_APP
    assert connection.app_collection_allowed() is False

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
    at.run()

    exceptions = [str(e.value) for e in at.exception]
    assert not exceptions, exceptions[0][:500]

    labels = [str(getattr(button, "label", "")) for button in at.button]
    assert not any("Collect now" in label for label in labels)
    assert not any("Start collector" in label for label in labels)

    joined = "\n".join(_texts(at))
    assert READ_ONLY_MESSAGE in joined
    assert "Last successful collection:" in joined
    assert f"{n_samples} sample(s) stored" in joined

    scheduler = at.session_state["_scheduler"]
    assert scheduler.is_running() is False
    assert scheduler.status().cycles_completed == 0
