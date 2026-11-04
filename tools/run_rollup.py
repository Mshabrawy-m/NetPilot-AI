"""Standalone NetPilot daily-rollup runner (Stage A6-2).

This one-shot, externally scheduled process aggregates *completed* UTC days of
raw live telemetry from ``metric_samples`` into ``metric_daily_rollups`` so that
long-horizon analytics survive once raw retention (A6-1) shrinks the detailed
history. It is separate from ``run_collector.py`` and ``run_retention.py`` and
from the hosted Streamlit reader: it runs no collection, performs **no DDL**
(schema creation stays the responsibility of ``run_collector.py init-schema``),
and is meant to be driven by a cron/systemd timer on the collector host.

Operational notes
-----------------
* The current, still-incomplete UTC day is **never** aggregated. Each run
  rebuilds the ``rollup_lookback_days`` most-recent *completed* days, which also
  re-derives older days so late-arriving telemetry is folded in.
* Rollups are non-destructive (an idempotent upsert into its own table); this
  command therefore does **not** run retention and does not enable/schedule it.
* Reuses the collector's advisory ``InstanceLock`` and the same configuration /
  demo-isolation guards as ``run_retention.py``.

Usage
-----
    python tools/run_rollup.py        # rebuild recent completed UTC days

Environment
-----------
    DATABASE_URL                   must be PostgreSQL (required, never printed)
    NETPILOT_DEMO_MODE             must be false (required)
    NETPILOT_ROLLUP_LOOKBACK_DAYS  completed UTC days to rebuild per run
                                   (default 1 = yesterday; 0 is a no-op)
    NETPILOT_ROLLUP_LOCK           lock-file path (default <tmp>/netpilot-rollup.lock)
    NETPILOT_LOG_LEVEL             DEBUG/INFO/WARNING (INFO default)

Exit codes (mirroring run_collector.py / run_retention.py)
---------------------------------------------------------
    0  success (or a benign skip because another lock holder is active)
    1  rollup run failed for a non-database reason
    2  configuration error (demo mode, non-PostgreSQL URL, safety flags on)
    3  database unavailable / rollup table missing (OperationalError)

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
from database.repository import rollup_uniqueness_strategy_info, upsert_daily_rollups

# Reuse the collector's battle-tested advisory lock so rollups, retention and
# collection share one locking implementation (fcntl on POSIX, msvcrt on Windows).
from tools.run_collector import InstanceLock

EXIT_OK = 0
EXIT_ROLLUP_ERROR = 1
EXIT_CONFIG = 2
EXIT_DB = 3

logger = logging.getLogger("netpilot.rollup.runner")


def default_lock_path() -> str:
    """Lock file location, overridable with NETPILOT_ROLLUP_LOCK.

    A *distinct* default from the collector and retention locks, so a rollup
    pass neither blocks nor is blocked by them; two overlapping rollup passes
    serialize on this file.
    """
    env = os.getenv("NETPILOT_ROLLUP_LOCK", "").strip()
    if env:
        return env
    return str(Path(tempfile.gettempdir()) / "netpilot-rollup.lock")


def check_configuration(settings) -> list[str]:
    """Return human-readable reasons the rollup pass must refuse to run.

    Empty list means the configuration is a valid live rollup run. Stage
    A6b-1 M4 validates the rollup role's **own** connection
    (``NETPILOT_DB_URL_ROLLUP`` under separation, else ``DATABASE_URL``) and
    fails closed if it is missing. Only the URL scheme is ever echoed back -
    never the DSN or credentials.
    """
    errors: list[str] = []
    if settings.demo_mode:
        errors.append(
            "rollups require NETPILOT_DEMO_MODE=false (live mode); demo history "
            "is never aggregated.")
    try:
        url = roles.resolve_role_url(roles.ROLE_ROLLUP, settings=settings)
    except roles.RoleConfigError as exc:
        errors.append(str(exc))
        url = None
    if url is not None and not url.startswith("postgresql"):
        scheme = url.split("://", 1)[0] or "unset"
        errors.append(
            "rollups require a PostgreSQL connection for the netpilot_rollup "
            f"role (the shared external database); current scheme is {scheme!r}.")
    if url is not None and is_external_database(url) and settings.demo_mode:
        errors.append(
            "DEMO/LIVE ISOLATION: demo mode must not target an external "
            "database.")
    if settings.change_loop_enabled:
        errors.append(
            "rollups refuse to run with NETPILOT_CHANGE_LOOP_ENABLED=true.")
    if settings.lab_execution_enabled:
        errors.append(
            "rollups refuse to run with NETPILOT_LAB_EXECUTION_ENABLED=true.")
    return errors


def completed_day_windows(today_utc: dt.date, lookback_days: int) -> list[tuple[dt.datetime, dt.datetime]]:
    """Return ``[day_start, day_end)`` naive-UTC windows for completed days.

    ``today_utc`` (the current, incomplete day) is always excluded: the highest
    window ends at ``today 00:00``. ``lookback_days`` completed days are returned,
    newest first. A non-positive lookback yields no windows.
    """
    windows: list[tuple[dt.datetime, dt.datetime]] = []
    for offset in range(1, max(0, lookback_days) + 1):
        day_end = dt.datetime.combine(
            today_utc - dt.timedelta(days=offset - 1), dt.time(0, 0))
        day_start = day_end - dt.timedelta(days=1)
        windows.append((day_start, day_end))
    return windows


def perform_rollup(
    lock_path: str,
    settings,
    *,
    session_factory: Callable[[], object] = get_session,
    upsert_fn: Callable[..., int] = upsert_daily_rollups,
    now_fn: Callable[[], dt.datetime] = db_utcnow,
) -> int:
    """Rebuild the recent completed UTC days under the single-instance lock.

    Each day is a separate atomic ``upsert_daily_rollups`` call (its own
    transaction). A contended lock is a benign skip (EXIT_OK) that writes
    nothing. Any database error is reported by exception class only (never the
    DSN).
    """
    lock = InstanceLock(lock_path)
    if not lock.acquire():
        logger.warning("another rollup pass holds the lock; skipping this run")
        return EXIT_OK
    try:
        today_utc = now_fn().date()
        windows = completed_day_windows(today_utc, settings.rollup_lookback_days)
        if not windows:
            logger.info("rollup lookback is 0 days; nothing to do")
            return EXIT_OK
        # Read-only provenance for the NULL-safe uniqueness decision: it names the
        # configured server major so an operator can see that the surrogate key was
        # chosen deliberately, not because the server was assumed to be new. Never
        # fatal - the constraint itself is version-independent.
        probe = session_factory()
        try:
            logger.info("rollup uniqueness strategy: %s",
                        rollup_uniqueness_strategy_info(probe))
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            logger.debug("rollup uniqueness inspection unavailable (%s)",
                         type(exc).__name__)
        finally:
            probe.close()
        total = 0
        for day_start, day_end in windows:
            session = session_factory()
            try:
                written = upsert_fn(
                    session, day_start=day_start, day_end=day_end)
            finally:
                session.close()
            total += written
            logger.info(
                "rebuilt rollups for %s..%s: %d row(s)",
                day_start.isoformat(), day_end.isoformat(), written)
        logger.info("rollup completed: %d total row(s) written", total)
        return EXIT_OK
    except OperationalError as exc:
        logger.error("database unavailable (%s); rollup aborted",
                     type(exc).__name__)
        return EXIT_DB
    except Exception as exc:  # noqa: BLE001 - never crash the timer blindly
        logger.error("rollup failed: %s: %s",
                     type(exc).__name__, str(exc)[:200])
        return EXIT_ROLLUP_ERROR
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
        prog="run_rollup",
        description="Rebuild recent completed UTC-day telemetry rollups.")
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

    # Bind the connection layer to the rollup role so the aggregation runs on
    # the netpilot_rollup connection only (never a shared/administrator URL).
    previous_role = connection.current_role()
    connection.set_database_role(roles.ROLE_ROLLUP)
    try:
        return perform_rollup(default_lock_path(), settings)
    finally:
        connection.set_database_role(previous_role)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
