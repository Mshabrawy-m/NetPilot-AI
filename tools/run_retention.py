"""Standalone NetPilot telemetry-retention runner (Stage A6-1).

This is the one-shot, externally scheduled process that deletes *expired raw
live telemetry* from ``metric_samples`` so the shared database stays inside its
storage budget. It is deliberately separate from ``run_collector.py`` and from
the hosted Streamlit reader: it runs no collection, opens no long-lived pool,
and is meant to be driven by a cron/systemd timer on the Linux collector host.

Operational safety (Stage A6-1)
-------------------------------
Retention stays **DISABLED in production until the A6-2 daily-rollup job is
implemented, tested and approved** - deleting raw history with no rollup
replacement would destroy the long-horizon data forecasts need. This module
therefore:

* is **never** imported or invoked by the application, the collector or the
  scheduler - only an operator running it explicitly can delete anything;
* has **no force / bypass / enable flag**. There is no way to make it delete
  more than its bounded, scoped, age-based configuration allows;
* refuses to run at all unless ``NETPILOT_DEMO_MODE=false`` *and* a PostgreSQL
  ``DATABASE_URL`` are set, the change-loop and lab-execution safety flags are
  off, and ``NETPILOT_RETENTION_RAW_DAYS`` is a positive number.

The deletion itself is done by :func:`database.repository.delete_expired_live_samples`,
which hard-codes ``data_source = 'live'`` AND ``collected_at < cutoff`` and never
calls the unscoped ``purge_metric_samples``.

Subcommand
----------
    python tools/run_retention.py        # one bounded retention pass

Exit codes (mirroring run_collector.py)
---------------------------------------
    0  success (or a benign skip because another lock holder is active)
    1  retention run failed for a non-database reason
    2  configuration error (demo mode, non-PostgreSQL URL, safety flags on,
       or retention disabled via a non-positive NETPILOT_RETENTION_RAW_DAYS)
    3  database unavailable (OperationalError)

The DATABASE_URL and any credentials in it are never printed; database failures
report only the exception class.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import OperationalError

from config import (
    demo_isolation_violation,
    get_settings,
    is_external_database,
)
from database import connection, roles
from database.connection import get_session
from database.models import db_utcnow
from database.repository import delete_expired_live_samples

# Reuse the collector's battle-tested advisory lock so retention and collection
# share one locking implementation (fcntl on POSIX, msvcrt on Windows).
from tools.run_collector import InstanceLock

EXIT_OK = 0
EXIT_RETENTION_ERROR = 1
EXIT_CONFIG = 2
EXIT_DB = 3

logger = logging.getLogger("netpilot.retention.runner")


def default_lock_path() -> str:
    """Lock file location, overridable with NETPILOT_RETENTION_LOCK.

    Intentionally a *different* default from the collector lock, so a retention
    pass does not block on (or get blocked by) a concurrent collector cycle;
    PostgreSQL MVCC keeps the batched DELETE and the collector INSERT correct
    independently. Two overlapping retention passes, however, serialize on it.
    """
    env = os.getenv("NETPILOT_RETENTION_LOCK", "").strip()
    if env:
        return env
    return str(Path(tempfile.gettempdir()) / "netpilot-retention.lock")


def check_configuration(settings) -> list[str]:
    """Return human-readable reasons the retention pass must refuse to run.

    Empty list means the configuration is a valid, *enabled* live retention run.
    Stage A6b-1 M4 validates the retention role's **own** connection
    (``NETPILOT_DB_URL_RETENTION`` under separation, else ``DATABASE_URL``) and
    fails closed if it is missing. Only the URL scheme is ever echoed back -
    never the DSN or credentials.
    """
    errors: list[str] = []
    if settings.demo_mode:
        errors.append(
            "retention requires NETPILOT_DEMO_MODE=false (live mode); it must "
            "never delete from a demo store.")
    try:
        url = roles.resolve_role_url(roles.ROLE_RETENTION, settings=settings)
    except roles.RoleConfigError as exc:
        errors.append(str(exc))
        url = None
    if url is not None and not url.startswith("postgresql"):
        scheme = url.split("://", 1)[0] or "unset"
        errors.append(
            "retention requires a PostgreSQL connection for the "
            "netpilot_retention role (the shared external database); current "
            f"scheme is {scheme!r}.")
    if url is not None and is_external_database(url) and settings.demo_mode:
        errors.append(
            "DEMO/LIVE ISOLATION: demo mode must not target an external "
            "database.")
    if settings.change_loop_enabled:
        errors.append(
            "retention refuses to run with NETPILOT_CHANGE_LOOP_ENABLED=true.")
    if settings.lab_execution_enabled:
        errors.append(
            "retention refuses to run with NETPILOT_LAB_EXECUTION_ENABLED=true.")
    # No bypass: a non-positive window is a hard refusal, not a "delete nothing"
    # success, so an operator cannot silently mis-schedule an empty run and think
    # retention is active. There is deliberately no --force flag anywhere.
    if settings.retention_raw_days <= 0:
        errors.append(
            "retention is disabled (NETPILOT_RETENTION_RAW_DAYS must be a "
            "positive number of days). This is expected until the A6-2 daily "
            "rollups are approved.")
    if settings.retention_batch_size <= 0:
        errors.append(
            "retention requires a positive NETPILOT_RETENTION_BATCH_SIZE.")
    return errors


def perform_retention(
    lock_path: str,
    settings,
    *,
    session_factory: Callable[[], object] = get_session,
    delete_fn: Callable[..., int] = delete_expired_live_samples,
    now_fn: Callable[[], dt.datetime] = db_utcnow,
) -> int:
    """Run one bounded retention pass under the single-instance lock.

    Returns an exit code. A contended lock is a benign skip (EXIT_OK) that deletes
    nothing, so overlapping timer fires never race a second retention pass. The
    cutoff is a naive UTC value derived from ``now_fn`` minus the configured
    window, matching the ``collected_at`` convention. Any database error is
    reported by exception class only (never the DSN).
    """
    lock = InstanceLock(lock_path)
    if not lock.acquire():
        logger.warning("another retention pass holds the lock; skipping this run")
        return EXIT_OK
    try:
        cutoff = now_fn() - dt.timedelta(days=settings.retention_raw_days)
        session = session_factory()
        try:
            deleted = delete_fn(
                session,
                cutoff_utc=cutoff,
                batch_size=settings.retention_batch_size,
            )
        finally:
            session.close()
        logger.info("retention completed: %d expired live samples deleted",
                    deleted)
        return EXIT_OK
    except OperationalError as exc:
        logger.error("database unavailable (%s); retention aborted",
                     type(exc).__name__)
        return EXIT_DB
    except Exception as exc:  # noqa: BLE001 - never crash the timer blindly
        logger.error("retention failed: %s: %s",
                     type(exc).__name__, str(exc)[:200])
        return EXIT_RETENTION_ERROR
    finally:
        lock.release()


def _configure_logging() -> None:
    level = os.getenv("NETPILOT_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_retention",
        description="Bounded live-telemetry retention pass (external mode).")
    parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()

    # Deepest isolation guard runs first, before any session is opened, so a
    # demo configuration aimed at a shared DB cannot connect at all.
    violation = demo_isolation_violation(settings)
    if violation:
        logger.error("configuration rejected: %s", violation)
        return EXIT_CONFIG

    errors = check_configuration(settings)
    if errors:
        for message in errors:
            logger.error("configuration rejected: %s", message)
        return EXIT_CONFIG

    # Bind the connection layer to the retention role so the DELETE runs on the
    # netpilot_retention connection only (never a shared/administrator URL).
    previous_role = connection.current_role()
    connection.set_database_role(roles.ROLE_RETENTION)
    try:
        return perform_retention(default_lock_path(), settings)
    finally:
        connection.set_database_role(previous_role)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
