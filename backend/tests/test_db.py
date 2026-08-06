"""Tests for the storage layer — models, session factory, and SQLite pragmas.

Uses a temp file DB per test (tmp_path) so WAL + foreign-key pragmas behave
like the real database, not an in-memory shim.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.db.base import Base
from backend.db.models import Player, Snapshot
from backend.db.session import make_engine, make_session_factory


async def _make_db(tmp_path, filename: str = "t.db"):
    """Create tables on a temp file DB and return (engine, session_factory)."""
    engine = make_engine(str(tmp_path / filename))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, make_session_factory(engine)


async def test_insert_player_and_snapshot(tmp_path) -> None:
    engine, Session = await _make_db(tmp_path)

    async with Session() as session:
        player = Player(uid="100", platform="PC", username="Alice")
        session.add(player)
        await session.commit()
        await session.refresh(player)
        assert player.id is not None

        snap = Snapshot(player_id=player.id, raw_json="{}", level=100, kills=5)
        session.add(snap)
        await session.commit()

    async with Session() as session:
        snaps = (
            await session.execute(
                select(Snapshot).where(Snapshot.player_id == player.id)
            )
        ).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].level == 100
    assert snaps[0].kills == 5

    await engine.dispose()


async def test_uid_is_unique(tmp_path) -> None:
    engine, Session = await _make_db(tmp_path)

    async with Session() as session:
        session.add(Player(uid="200", platform="PC"))
        await session.commit()

    async with Session() as session:
        session.add(Player(uid="200", platform="PC"))
        with pytest.raises(IntegrityError):
            await session.commit()

    await engine.dispose()


async def test_username_can_be_null(tmp_path) -> None:
    # Steam-linked accounts may have an empty indexed name.
    engine, Session = await _make_db(tmp_path)

    async with Session() as session:
        player = Player(uid="300", platform="PC")  # no username
        session.add(player)
        await session.commit()
        await session.refresh(player)
        assert player.username is None

    await engine.dispose()


async def test_snapshot_parsed_fields_all_nullable(tmp_path) -> None:
    # Upstream trackers are often partially unpopulated; parsed fields must
    # accept NULL without error.
    engine, Session = await _make_db(tmp_path)

    async with Session() as session:
        player = Player(uid="400", platform="PC")
        session.add(player)
        await session.commit()
        await session.refresh(player)
        snap = Snapshot(player_id=player.id, raw_json="{}")  # all None
        session.add(snap)
        await session.commit()
        assert snap.kills is None
        assert snap.rank_name is None
        assert snap.damage is None

    await engine.dispose()


async def test_delete_player_cascades_to_snapshots(tmp_path) -> None:
    engine, Session = await _make_db(tmp_path)

    async with Session() as session:
        player = Player(uid="500", platform="PC")
        session.add(player)
        await session.commit()
        await session.refresh(player)
        session.add(Snapshot(player_id=player.id, raw_json="{}", level=1))
        session.add(Snapshot(player_id=player.id, raw_json="{}", level=2))
        await session.commit()

    async with Session() as session:
        obj = await session.get(Player, player.id)
        await session.delete(obj)
        await session.commit()

    async with Session() as session:
        remaining = (await session.execute(select(Snapshot))).scalars().all()
    assert remaining == []

    await engine.dispose()


async def test_wal_mode_enabled_on_file_db(tmp_path) -> None:
    engine, _ = await _make_db(tmp_path)
    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
    assert mode == "wal"
    await engine.dispose()
