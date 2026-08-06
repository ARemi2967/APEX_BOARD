"""Async SQLAlchemy engine + session factory for SQLite (WAL mode).

Usage:
    engine = make_engine("data/apex.db")
    Session = make_session_factory(engine)
    async with Session() as session:
        ...

Every new connection gets `PRAGMA journal_mode=WAL` and
`PRAGMA foreign_keys=ON` attached via a connect-time listener, so cascade
deletes and concurrent read/write work without further setup.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(db_path: str) -> AsyncEngine:
    """Build an async SQLite engine for `db_path` with WAL + FK pragmas."""
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url)
    _attach_sqlite_pragmas(engine)
    return engine


def _attach_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def make_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to `engine`.

    `expire_on_commit=False` so returned ORM objects stay usable after commit
    (important for the API layer returning model instances).
    """
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
