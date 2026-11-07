"""Regression test for get_last_successful_run data_source parameter.

This test verifies that the fix for the TypeError when calling
get_last_successful_run with data_source parameter works correctly.
"""
from __future__ import annotations

import pytest

from database import connection, repository, source_scope
from services.app_state import ensure_ready_for_mode


@pytest.fixture(autouse=True)
def ensure_schema():
    """Ensure schema exists before each test."""
    ensure_ready_for_mode()


@pytest.fixture
def session():
    """Create a fresh session for each test."""
    with connection.session_scope() as s:
        yield s


def test_get_last_successful_run_with_none_source(session):
    """Test that get_last_successful_run works with data_source=None."""
    result = repository.get_last_successful_run(session, data_source=None)
    # Should not raise TypeError
    assert result is None or isinstance(result, repository.CollectionRun)


def test_get_last_successful_run_with_live_source(session):
    """Test that get_last_successful_run works with data_source='live'."""
    result = repository.get_last_successful_run(session, data_source="live")
    # Should not raise TypeError
    assert result is None or isinstance(result, repository.CollectionRun)


def test_get_last_successful_run_with_demo_source(session):
    """Test that get_last_successful_run works with data_source='demo'."""
    result = repository.get_last_successful_run(session, data_source="demo")
    # Should not raise TypeError
    assert result is None or isinstance(result, repository.CollectionRun)


def test_get_last_successful_run_with_simulated_source(session):
    """Test that get_last_successful_run works with data_source='simulated'."""
    result = repository.get_last_successful_run(session, data_source="simulated")
    # Should not raise TypeError
    assert result is None or isinstance(result, repository.CollectionRun)


def test_source_scope_comparison():
    """Test that source_scope constants are strings and comparable."""
    assert isinstance(source_scope.DEMO, str)
    assert isinstance(source_scope.LIVE, str)
    assert isinstance(source_scope.SIMULATED, str)

    # Test comparisons
    assert source_scope.DEMO == "demo"
    assert source_scope.LIVE == "live"
    assert source_scope.SIMULATED == "simulated"

    # Test inequality
    assert source_scope.DEMO != source_scope.LIVE
    assert source_scope.LIVE != source_scope.SIMULATED


def test_ternary_expression_with_source_scope():
    """Test that the ternary expression used in the fix works correctly."""
    # Simulate the expression: None if source == source_scope.DEMO else str(source)

    # Test with DEMO
    source = source_scope.DEMO
    data_source_value = None if source == source_scope.DEMO else str(source)
    assert data_source_value is None

    # Test with LIVE
    source = source_scope.LIVE
    data_source_value = None if source == source_scope.DEMO else str(source)
    assert data_source_value == "live"

    # Test with SIMULATED
    source = source_scope.SIMULATED
    data_source_value = None if source == source_scope.DEMO else str(source)
    assert data_source_value == "simulated"

    # Test with None (edge case)
    source = None
    data_source_value = None if source == source_scope.DEMO else str(source)
    assert data_source_value == "None"  # str(None) == "None"

    # Test with integer (edge case)
    source = 123
    data_source_value = None if source == source_scope.DEMO else str(source)
    assert data_source_value == "123"


def test_get_last_successful_run_with_seeded_data(session):
    """Test get_last_successful_run with actual seeded data."""
    from database.models import CollectionRun, db_utcnow

    # Create test runs with different data_source values
    now = db_utcnow()
    run_live = CollectionRun(
        started_at=now,
        finished_at=now,
        status="ok",
        devices_total=1,
        devices_up=1,
        data_source="live"
    )
    run_sim = CollectionRun(
        started_at=now,
        finished_at=now,
        status="ok",
        devices_total=1,
        devices_up=1,
        data_source="simulated"
    )
    run_demo = CollectionRun(
        started_at=now,
        finished_at=now,
        status="ok",
        devices_total=1,
        devices_up=1,
        data_source="demo"
    )

    session.add_all([run_live, run_sim, run_demo])
    session.flush()

    # Test querying with data_source parameter
    result_live = repository.get_last_successful_run(session, data_source="live")
    assert result_live is not None
    assert result_live.data_source == "live"

    result_sim = repository.get_last_successful_run(session, data_source="simulated")
    assert result_sim is not None
    assert result_sim.data_source == "simulated"

    result_demo = repository.get_last_successful_run(session, data_source="demo")
    assert result_demo is not None
    assert result_demo.data_source == "demo"

    # Test querying without data_source parameter (should return any)
    result_any = repository.get_last_successful_run(session)
    assert result_any is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
