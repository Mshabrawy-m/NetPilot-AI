"""Standalone NetPilot collector runner (Stage A2 - external live collection).

This lets the existing, unmodified ``network.metrics_collector.collect_once()``
run as a one-shot process on a Linux lab host that has Docker/Containerlab and
LAN reachability to the monitored devices, writing to a *shared* PostgreSQL
database that the hosted Streamlit app later reads. Streamlit Community Cloud
never runs this; it only reads.

The module deliberately imports **no** Streamlit or FastAPI: the whole write
path (config, database, network) is Streamlit-free, so this is safe to drive
from a systemd timer or cron. It also never performs any network *configuration*
change - ``collect_once`` is read-only probing - and it refuses to start when
the change-loop or lab-execution safety flags are enabled.

Subcommands
-----------
    python tools/run_collector.py init-schema   # one-time DDL (schema admin)
    python tools/run_collector.py collect        # one live collection cycle

Design choices
--------------
* **One-shot, externally scheduled.** Prefer a systemd timer over an internal
  daemon: the process opens a DB connection, does one cycle, and exits, so a
  long-lived connection pool (and its ``pool_recycle`` concerns) is never
  needed. Idle-timeout managed Postgres tiers stay happy.
* **Separated schema vs collection.** ``init-schema`` needs DDL privileges and
  is run once by a schema administrator. ``collect`` needs only
  SELECT/INSERT/UPDATE and is what the timer repeats, so the collector role is
  never granted DDL.
* **Explicit live configuration required.** ``collect`` refuses unless
  ``NETPILOT_DEMO_MODE=false`` *and* a PostgreSQL ``DATABASE_URL`` are set, and
  refuses when either safety flag is on. There is no silent default that could
  collect into a demo store or reach a config-changing path.
* **Single-instance lock (this host only).** ``collect`` takes an advisory OS
  file lock so a second concurrent invocation on the same host skips instead of
  double-collecting or racing the inventory upserts. This is a local lock; it
  does NOT coordinate a second collector on a different host - run exactly one
  collector per lab across your fleet.

Exit codes
----------
    0  success (or a benign skip because another collector holds the lock)
    1  collection cycle reported status="error"
    2  configuration error (demo mode, non-PostgreSQL URL, safety flags on)
    3  database unavailable / schema not ready (OperationalError)

The DATABASE_URL and any credentials in it are never printed; failures report
only the exception class.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import OperationalError

from config import get_settings, is_external_database
from database import connection, roles
from database.connection import init_db
from network.metrics_collector import collect_once

EXIT_OK = 0
EXIT_COLLECTION_ERROR = 1
EXIT_CONFIG = 2
EXIT_DB = 3

logger = logging.getLogger("netpilot.collector.runner")

# fcntl on POSIX, msvcrt on Windows. Either present is enough for the
# single-instance lock; if neither exists we degrade to no local lock (never
# happens on the Linux target or a Windows dev box).
try:  # pragma: no cover - platform dependent
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None
try:  # pragma: no cover - platform dependent
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover
    _msvcrt = None


def default_lock_path() -> str:
    """Lock file location, overridable with NETPILOT_COLLECTOR_LOCK."""
    env = os.getenv("NETPILOT_COLLECTOR_LOCK", "").strip()
    if env:
        return env
    return str(Path(tempfile.gettempdir()) / "netpilot-collector.lock")


class InstanceLock:
    """Advisory, non-blocking, per-host exclusive lock on a lock file.

    POSIX uses ``flock``; Windows uses ``msvcrt.locking``. ``acquire()`` returns
    False immediately if another holder has it, and it is always released in a
    ``finally`` so a crashed cycle cannot wedge the schedule (an OS lock dies
    with the process anyway).
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+b")
        try:
            if _fcntl is not None:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            elif _msvcrt is not None:
                fh.seek(0)
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)
            # else: no locking primitive on this platform -> best-effort.
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                fh.seek(0)
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - release is best-effort
            pass
        finally:
            fh.close()


def check_configuration(settings) -> list[str]:
    """Return human-readable reasons ``collect`` must refuse to run.

    Empty list means the live configuration is valid. Stage A6b-1 M4 validates
    the collector's **own** role connection (``NETPILOT_DB_URL_COLLECTOR`` when
    role separation is adopted, otherwise ``DATABASE_URL``) rather than any
    shared/administrator URL, and fails closed if that role URL is missing.
    Only the URL scheme (never the DSN or credentials) is ever echoed back.
    """
    errors: list[str] = []
    if settings.demo_mode:
        errors.append(
            "collect requires NETPILOT_DEMO_MODE=false (live mode); the "
            "collector must never write to or read a demo store.")
    try:
        url = roles.resolve_role_url(roles.ROLE_COLLECTOR, settings=settings)
    except roles.RoleConfigError as exc:
        errors.append(str(exc))
        url = None
    if url is not None and not url.startswith("postgresql"):
        scheme = url.split("://", 1)[0] or "unset"
        errors.append(
            "collect requires a PostgreSQL connection for the "
            "netpilot_collector role (the shared external database); current "
            f"scheme is {scheme!r}.")
    if url is not None and is_external_database(url) and settings.demo_mode:
        errors.append(
            "DEMO/LIVE ISOLATION: demo mode must not target an external "
            "database.")
    if settings.change_loop_enabled:
        errors.append(
            "collect refuses to run with NETPILOT_CHANGE_LOOP_ENABLED=true; "
            "the standalone collector is read-only.")
    if settings.lab_execution_enabled:
        errors.append(
            "collect refuses to run with NETPILOT_LAB_EXECUTION_ENABLED=true; "
            "the standalone collector performs no device configuration.")
    return errors


def perform_collect(lock_path: str, *,
                    run: Callable[[], object] = collect_once) -> int:
    """Run one collection cycle under the single-instance lock.

    Returns an exit code. A contended lock is a benign skip (EXIT_OK) that runs
    no collection, so overlapping timer fires never double-write or race the
    inventory sync. Any database error is reported by exception class only.
    """
    lock = InstanceLock(lock_path)
    if not lock.acquire():
        logger.warning("another collector holds the lock; skipping this cycle")
        return EXIT_OK
    try:
        try:
            summary = run()
        except OperationalError as exc:
            logger.error("database unavailable (%s); cycle aborted",
                         type(exc).__name__)
            return EXIT_DB
        except Exception as exc:  # noqa: BLE001 - never crash the timer blindly
            logger.error("collection failed: %s: %s",
                         type(exc).__name__, str(exc)[:200])
            return EXIT_COLLECTION_ERROR
        status = getattr(summary, "status", None)
        if status == "error":
            logger.error("collection reported status=error")
            return EXIT_COLLECTION_ERROR
        if status == "partial":
            logger.warning("collection completed with status=partial "
                           "(some sources unavailable)")
            return EXIT_OK
        logger.info("collection completed successfully")
        return EXIT_OK
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
        prog="run_collector",
        description="Standalone NetPilot collector (external live mode).")
    parser.add_argument(
        "command", choices=("init-schema", "collect"),
        help="init-schema: one-time DDL setup. collect: one live cycle.")
    args = parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()

    if args.command == "init-schema":
        # Schema administration. Kept separate so the routine collector role
        # needs no DDL grant. Runs as the explicit administrator role
        # (Stage A6b-1 M4): it uses NETPILOT_DB_URL_ADMIN when role separation
        # is adopted, and never the collector's own connection. Isolation still
        # applies: never init a shared DB as a demo run.
        if settings.demo_mode and is_external_database(settings.database_url):
            logger.error("refusing to initialize an external database in "
                         "demo mode (DEMO/LIVE isolation)")
            return EXIT_CONFIG
        previous_role = connection.current_role()
        connection.set_database_role(roles.ROLE_ADMIN)
        try:
            init_db()
        except roles.RoleConfigError as exc:
            logger.error("configuration rejected: %s", exc)
            return EXIT_CONFIG
        except OperationalError as exc:
            logger.error("schema initialization failed (%s)", type(exc).__name__)
            return EXIT_DB
        finally:
            connection.set_database_role(previous_role)
        logger.info("database schema initialized")
        return EXIT_OK

    # command == "collect"
    errors = check_configuration(settings)
    if errors:
        for message in errors:
            logger.error("configuration rejected: %s", message)
        return EXIT_CONFIG
    # Bind the connection layer to the collector's own role for this cycle so
    # collect_once can only ever use the collector URL (no admin fallback).
    previous_role = connection.current_role()
    connection.set_database_role(roles.ROLE_COLLECTOR)
    try:
        return perform_collect(default_lock_path())
    finally:
        connection.set_database_role(previous_role)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
