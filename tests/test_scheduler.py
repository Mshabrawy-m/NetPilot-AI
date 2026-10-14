"""Collection scheduler tests: overlap prevention, error capture, lifecycle.

These run the real threading machinery with short intervals - no lab and
no network involved (the job is a stub)."""
from __future__ import annotations

import threading
import time

from services.collection_service import CollectionScheduler


def test_run_now_executes_job_and_records_success():
    calls = []

    def job():
        calls.append(1)
        return type("R", (), {"status": "ok", "errors": []})()

    sched = CollectionScheduler(job, interval_seconds=999)
    sched.run_now()
    assert len(calls) == 1
    status = sched.status()
    assert status.last_cycle_status == "ok"
    assert status.cycles_completed == 1
    assert status.last_error is None


def test_overlapping_jobs_are_prevented():
    started = threading.Event()
    release = threading.Event()

    def slow_job():
        started.set()
        release.wait(timeout=5)
        return type("R", (), {"status": "ok", "errors": []})()

    sched = CollectionScheduler(slow_job, interval_seconds=999)
    t = threading.Thread(target=sched.run_now)
    t.start()
    assert started.wait(2)
    # While a job holds the lock, a second run_now must skip, not overlap.
    result = sched.run_now()
    assert isinstance(result, str) and "already running" in result
    release.set()
    t.join(5)
    assert not t.is_alive()


def test_job_exception_is_captured_not_raised():
    def bad_job():
        raise RuntimeError("simulated collector failure")

    sched = CollectionScheduler(bad_job, interval_seconds=999)
    result = sched.run_now()          # must not raise
    assert result is None
    status = sched.status()
    assert status.last_cycle_status == "error"
    assert "RuntimeError" in status.last_error


def test_background_loop_runs_and_stops_cleanly():
    calls = []

    def job():
        calls.append(1)
        return type("R", (), {"status": "ok", "errors": []})()

    sched = CollectionScheduler(job, interval_seconds=1)
    assert sched.start() is True
    assert sched.start() is False      # already running -> no double thread
    time.sleep(2.5)
    sched.stop()
    assert len(calls) >= 2
    assert not sched.is_running()
    assert sched.status().running is False


def test_loop_skips_when_previous_cycle_still_running():
    """Directly verifies the non-blocking acquire path used by the loop."""
    sched = CollectionScheduler(lambda: None, interval_seconds=1)
    sched._lock.acquire()              # simulate long-running job
    try:
        notice = sched.run_now()
        assert isinstance(notice, str)
    finally:
        sched._lock.release()
