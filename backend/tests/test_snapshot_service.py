"""Tests for snapshot parsing, capture, and scheduler failure isolation."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from backend.db.base import Base
from backend.db.models import Player, Snapshot
from backend.db.session import make_engine, make_session_factory
from backend.integrations.exceptions import PlayerNotFoundError
from backend.scheduler.jobs import build_scheduler, capture_all_tracked
from backend.services.snapshot_service import capture_snapshot, parse_bridge

# A representative bridge response (trimmed to the fields we parse).
SAMPLE_BRIDGE: dict[str, Any] = {
    "global": {
        "name": "TestPlayer",
        "uid": "100",
        "level": 474,
        "rank": {"rankName": "Diamond", "rankDiv": 4, "rankScore": 12474},
        "battlepass": {"level": 110},
    },
    "total": {
        "kills": {"name": "Kills", "value": 22},
        "damage": {"name": "Damage", "value": 50000},
        "wins": {"name": "Wins", "value": 10},
        "kd": {"name": "KD", "value": -1},  # unpopulated sentinel
    },
    "legends": {"all": {
        "Wraith": {"data": [{"key": "specialEvent_kills", "value": 100},
                            {"key": "specialEvent_damage", "value": 5000}]},
        "Global": {"data": [{"key": "mastery_alternator_kills", "value": 773}]},
    }},
}


def test_parse_bridge_extracts_all_fields() -> None:
    parsed = parse_bridge(SAMPLE_BRIDGE)
    assert parsed["level"] == 474
    assert parsed["rank_name"] == "Diamond"
    assert parsed["rank_div"] == 4
    assert parsed["rank_score"] == 12474
    assert parsed["bp_level"] == 110
    assert parsed["kills"] == 100      # summed from Wraith legend (total ignored)
    assert parsed["damage"] == 5000    # summed from Wraith legend
    assert parsed["wins"] == 10
    assert parsed["matches_played"] is None  # key absent → None


def test_parse_bridge_tolerates_empty_and_sentinels() -> None:
    parsed = parse_bridge({"global": {}, "total": {}})
    assert parsed["level"] is None
    assert parsed["rank_name"] is None
    assert parsed["kills"] is None
    assert parsed["damage"] is None

    # -1 sentinel on a counter → None (not -1).
    parsed2 = parse_bridge({"total": {"kills": {"value": -1}}})
    assert parsed2["kills"] is None


def test_parse_bridge_sums_kills_damage_across_legends() -> None:
    # account kills/damage = sum across all real legends (specialEvent preferred,
    # plain fallback); 'Global' excluded; the `total` aggregate is ignored.
    parsed = parse_bridge(
        {
            "total": {"specialEvent_kills": {"value": 999}},  # ignored (single-legend)
            "legends": {"all": {
                "Octane": {"data": [{"key": "specialEvent_kills", "value": 100},
                                    {"key": "specialEvent_damage", "value": 5000}]},
                "Bloodhound": {"data": [{"key": "kills", "value": 22},
                                        {"key": "damage", "value": 1000}]},
                "Global": {"data": [{"key": "mastery_x_kills", "value": 50}]},
            }},
        }
    )
    assert parsed["kills"] == 122    # 100 (specialEvent) + 22 (fallback)
    assert parsed["damage"] == 6000  # 5000 + 1000


class _FakeClient:
    """Duck-typed stand-in for ApexClient."""

    def __init__(self, data: dict, fail_uid: str | None = None) -> None:
        self.data = data
        self.fail_uid = fail_uid

    async def get_bridge_by_uid(self, uid: str, platform: str) -> dict:
        if self.fail_uid is not None and uid == self.fail_uid:
            raise PlayerNotFoundError(f"not found: {uid}")
        return self.data


async def _make_db(tmp_path):
    engine = make_engine(str(tmp_path / "t.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, make_session_factory(engine)


async def test_capture_snapshot_inserts_row_and_backfills_username(tmp_path) -> None:
    engine, Session = await _make_db(tmp_path)

    async with Session() as session:
        player = Player(uid="100", platform="PC")  # no username
        session.add(player)
        await session.commit()
        await session.refresh(player)
        player_id = player.id

    snap = await capture_snapshot(_FakeClient(SAMPLE_BRIDGE), Session, player)

    assert snap.id is not None
    assert snap.player_id == player_id
    assert snap.level == 474
    assert snap.kills == 100     # summed from Wraith legend
    assert snap.rank_name == "Diamond"

    async with Session() as session:
        db_player = await session.get(Player, player_id)
        assert db_player.username == "TestPlayer"  # backfilled
        snaps = (await session.execute(select(Snapshot))).scalars().all()
    assert len(snaps) == 1
    assert json.loads(snaps[0].raw_json)["global"]["name"] == "TestPlayer"

    await engine.dispose()


async def test_capture_snapshot_keeps_existing_username(tmp_path) -> None:
    engine, Session = await _make_db(tmp_path)

    async with Session() as session:
        player = Player(uid="100", platform="PC", username="ManualName")
        session.add(player)
        await session.commit()
        await session.refresh(player)

    await capture_snapshot(_FakeClient(SAMPLE_BRIDGE), Session, player)

    async with Session() as session:
        db_player = await session.get(Player, player.id)
    assert db_player.username == "ManualName"  # not overwritten

    await engine.dispose()


async def test_capture_all_tracked_isolates_failure(tmp_path) -> None:
    engine, Session = await _make_db(tmp_path)

    async with Session() as session:
        session.add(Player(uid="ok", platform="PC"))
        session.add(Player(uid="bad", platform="PC"))
        await session.commit()

    # Client fails for "bad"; "ok" must still be captured.
    count = await capture_all_tracked(
        _FakeClient(SAMPLE_BRIDGE, fail_uid="bad"), Session
    )
    assert count == 1

    async with Session() as session:
        snaps = (await session.execute(select(Snapshot))).scalars().all()
    assert len(snaps) == 1

    await engine.dispose()


async def test_capture_all_tracked_no_players(tmp_path) -> None:
    engine, Session = await _make_db(tmp_path)
    count = await capture_all_tracked(_FakeClient(SAMPLE_BRIDGE), Session)
    assert count == 0
    await engine.dispose()


async def test_capture_snapshot_smooths_flaky_regression(tmp_path) -> None:
    # Upstream flakiness: a good baseline (4858/1.72M) then a fetch that
    # returns the stale plain kills=22 and no damage. Smoothing must keep
    # the last known good values.
    engine, Session = await _make_db(tmp_path)
    async with Session() as session:
        player = Player(uid="100", platform="PC")
        session.add(player)
        await session.commit()
        await session.refresh(player)
        session.add(Snapshot(
            player_id=player.id, raw_json="{}", kills=4858, damage=1722620, level=474,
        ))
        await session.commit()

    flaky = {
        "global": {"name": "T", "uid": "100", "level": 474},
        "total": {"kills": {"value": 22}},  # stale plain; no specialEvent, no damage
    }
    snap = await capture_snapshot(_FakeClient(flaky), Session, player)

    assert snap.kills == 4858       # smoothed
    assert snap.damage == 1722620   # smoothed (carried forward)
    assert snap.level == 474
    await engine.dispose()


async def test_capture_snapshot_keeps_genuine_increase(tmp_path) -> None:
    engine, Session = await _make_db(tmp_path)
    async with Session() as session:
        player = Player(uid="100", platform="PC")
        session.add(player)
        await session.commit()
        await session.refresh(player)
        session.add(Snapshot(player_id=player.id, raw_json="{}", kills=4858, level=474))
        await session.commit()

    grew = {
        "global": {"name": "T", "uid": "100", "level": 475},
        "legends": {"all": {"Octane": {"data": [{"key": "specialEvent_kills", "value": 4900}]}}},
    }
    snap = await capture_snapshot(_FakeClient(grew), Session, player)
    assert snap.kills == 4900   # real increase, not smoothed away
    assert snap.level == 475
    await engine.dispose()


def test_build_scheduler_creates_interval_job() -> None:
    scheduler = build_scheduler(
        client=_FakeClient(SAMPLE_BRIDGE),
        session_factory=object(),
        interval_minutes=5,
    )
    job = scheduler.get_job("snapshot_cycle")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 300  # 5 min
    assert job.max_instances == 1
