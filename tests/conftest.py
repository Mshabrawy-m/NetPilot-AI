"""Shared pytest fixtures.

Every test gets a fresh temporary SQLite database, so tests never touch
live data. Demo-mode tests set NETPILOT_DEMO_MODE themselves.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("NETPILOT_DEMO_MODE", "false")
    from database import connection
    connection.init_db()
    return url


@pytest.fixture()
def session(db_url):
    from database.connection import get_session
    sess = get_session()
    yield sess
    sess.close()
