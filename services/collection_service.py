"""Background collection scheduler.

Design choice (spec 3.5): Streamlit reruns scripts per interaction, so
timers inside the UI are unstable. Instead a single daemon thread per
process owns the periodic schedule:

  * interval comes from configuration (NETPILOT_COLLECT_INTERVAL),
  * a non-reentrant lock guarantees collection jobs never overlap -
    if a job is still running when the next tick fires, the tick is
    skipped and logged,
  * stop_event gives a clean shutdown,
  * errors are captured per-cycle and logged without secrets; the last
    error is also exposed via status() for the System Status page.

The Streamlit app starts/controls this through get_scheduler() which is
stored in st.session_state so pages share one instance.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from config import get_settings

logger = logging.getLogger("netpilot.scheduler")


@dataclass
class SchedulerStatus:
    running: bool = False
    interval_seconds: int = 0
    next_run_in: float | None = None
    last_cycle_at: float | None = None
    last_cycle_status: str | None = None
    skipped_overlaps: int = 0
    last_error: str | None = None
    cycles_completed: int = 0


class CollectionScheduler:
    def __init__(self, collect_job: Callable[[], object],
                 interval_seconds: int | None = None) -> None:
        settings = get_settings()
        self._job = collect_job
        self._interval = interval_seconds or settings.collect_interval_seconds
        self._lock = threading.Lock()          # guards job overlap
        self._state_lock = threading.Lock()    # guards status fields
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = SchedulerStatus(interval_seconds=self._interval)

    @property
    def interval(self) -> int:
        return self._interval

    def start(self) -> bool:
        """Start the background loop. Returns False if already running."""
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._status.running = True
            self._thread = threading.Thread(
                target=self._loop, name="netpilot-collector", daemon=True)
            self._thread.start()
        logger.info("collection scheduler started (interval=%ss)", self._interval)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._state_lock:
            self._status.running = False

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_now(self) -> object | str:
        """Manual refresh: run one job synchronously (UI button).

        Overlap-safe: returns a notice if a scheduled cycle holds the lock.
        """
        if not self._lock.acquire(blocking=False):
            return "A collection cycle is already running; manual refresh skipped."
        try:
            return self._run_job_once()
        finally:
            self._lock.release()

    def status(self) -> SchedulerStatus:
        with self._state_lock:
            snap = SchedulerStatus(**self._status.__dict__)
        return snap

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            due = time.monotonic() + self._interval
            # Sleep until next tick, but wake early on stop.
            while time.monotonic() < due and not self._stop_event.is_set():
                time.sleep(min(1.0, due - time.monotonic()))
            if self._stop_event.is_set():
                break
            if not self._lock.acquire(blocking=False):
                with self._state_lock:
                    self._status.skipped_overlaps += 1
                logger.warning("scheduled cycle skipped: previous job still running")
                continue
            try:
                self._run_job_once()
            finally:
                self._lock.release()

    def _run_job_once(self) -> object | None:
        try:
            result = self._job()
            with self._state_lock:
                self._status.last_cycle_at = time.time()
                self._status.cycles_completed += 1
                status = getattr(result, "status", None)
                self._status.last_cycle_status = status or "ok"
                if status in ("partial", "error"):
                    errs = getattr(result, "errors", [])
                    self._status.last_error = "; ".join(errs)[:300] or None
                else:
                    self._status.last_error = None
            return result
        except Exception as exc:  # noqa: BLE001 - scheduler must survive
            # Log only type + short message; never secrets, never full
            # command output (the collector itself already sanitises).
            logger.exception("collection job failed")
            with self._state_lock:
                self._status.last_cycle_status = "error"
                self._status.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            return None


# ----------------------------------------------------------------------
# Streamlit-friendly singleton helpers
# ----------------------------------------------------------------------
_scheduler: CollectionScheduler | None = None
_singleton_lock = threading.Lock()


def get_scheduler() -> CollectionScheduler:
    """One shared scheduler per process (Streamlit sessions included)."""
    global _scheduler
    with _singleton_lock:
        if _scheduler is None:
            from network.metrics_collector import collect_once
            _scheduler = CollectionScheduler(collect_job=collect_once)
        return _scheduler


def manual_refresh() -> object | str:
    return get_scheduler().run_now()
