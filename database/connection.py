"""Database connection management (replaceable storage layer).

The project uses SQLAlchemy 2.0 so the same code runs against:
  * SQLite     - default, zero-config local development
  * PostgreSQL - set DATABASE_URL=postgresql+psycopg2://user:pass@host/db

The connection URL comes only from configuration/environment variables.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings
from database.models import Base

_lock = threading.Lock()
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_engine_url: str | None = None


def _build_engine(url: str) -> Engine:
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        # Make sure the parent directory of a file-based SQLite DB exists.
        path_part = url.split("sqlite:///")[-1]
        if path_part and not path_part.startswith(":memory:"):
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        # SQLite uses a single writer; WAL improves concurrent read/write
        # behaviour for the background collector + Streamlit sessions.
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - glue
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine, _session_factory, _engine_url
    url = get_settings().database_url
    with _lock:
        if _engine is None or _engine_url != url:
            if _engine is not None:
                _engine.dispose()
            _engine = _build_engine(url)
            _session_factory = sessionmaker(bind=_engine, future=True)
            _engine_url = url
        assert _engine is not None
        return _engine


def get_session() -> Session:
    """Return a new ORM session (caller is responsible for closing)."""
    get_engine()  # ensures the factory matches the currently configured URL
    factory = _session_factory
    assert factory is not None
    return factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables if they do not exist (safe to call repeatedly)."""
    Base.metadata.create_all(get_engine())


def check_connection() -> tuple[bool, str]:
    """Lightweight connectivity probe used by the System Status page."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, get_engine().url.render_as_string(hide_password=True)
    except Exception as exc:  # pragma: no cover - depends on environment
        return False, f"{type(exc).__name__}: {exc}"


def reset_lab_data() -> int:
    """Delete all disposable lab data (every table). Returns table count.

    This only ever touches NetPilot's own database (SQLite file or the
    configured PostgreSQL schema) - never a production system.
    """
    engine = get_engine()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    return len(Base.metadata.tables)
