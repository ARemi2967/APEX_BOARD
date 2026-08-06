"""API layer tests — synchronous FastAPI TestClient + temp SQLite file.

Uses TestClient (which runs its own event loop) rather than httpx.AsyncClient +
ASGITransport, to avoid the anyio/pytest-asyncio task-tracking conflict. DB
seeding is done with a plain sync engine on the same temp file the app reads.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.config import Settings
from backend.db.base import Base
from backend.db.models import Player, Snapshot
from backend.main import create_app

SAMPLE_BRIDGE: dict[str, Any] = {
    "global": {
        "name": "TestPlayer",
        "uid": "100",
        "level": 474,
        "rank": {
            "rankName": "Diamond",
            "rankDiv": 4,
            "rankScore": 12474,
            "rankImg": "http://x/diamond.png",
        },
        "battlepass": {"level": 110},
    },
    "total": {
        "kills": {"value": 22},
        "damage": {"value": 50000},
        "wins": {"value": 10},
    },
    "legends": {
        "selected": {"Wraith": {"data": {}}},
        "all": {
            "Wraith": {"data": [{"key": "specialEvent_kills", "value": 22},
                                {"key": "specialEvent_damage", "value": 50000}]},
            "Bangalore": {},
        },
    },
}


class FakeClient:
    def __init__(self, data: dict = SAMPLE_BRIDGE) -> None:
        self.data = data
        self.calls = 0

    async def get_bridge_by_uid(self, uid: str, platform: str) -> dict:
        self.calls += 1
        return self.data

    async def close(self) -> None:
        pass


def _db_path(tmp_path) -> str:
    return str(tmp_path / "api.db")


def _bootstrap(
    tmp_path,
    *,
    fake: FakeClient | None = None,
    seed_players: list[Player] | None = None,
    seed_snapshots: list[Snapshot] | None = None,
):
    db_path = _db_path(tmp_path)
    settings = Settings(apex_api_key="test_key", db_path=db_path)
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    if seed_players or seed_snapshots:
        with Session(eng) as session:
            for p in seed_players or []:
                session.add(p)
            session.flush()
            for s in seed_snapshots or []:
                session.add(s)
            session.commit()
    eng.dispose()
    return create_app(
        start_scheduler=False, apex_client=fake or FakeClient(), settings=settings
    )


def _query_snapshots(db_path) -> list[Snapshot]:
    eng = create_engine(f"sqlite:///{db_path}")
    with Session(eng) as session:
        snaps = session.query(Snapshot).all()
        # detach
        for s in snaps:
            _ = s.level, s.kills
    eng.dispose()
    return snaps


def test_health(tmp_path) -> None:
    app = _bootstrap(tmp_path)
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_list_players(tmp_path) -> None:
    app = _bootstrap(
        tmp_path, seed_players=[Player(uid="100", platform="PC", username="Test")]
    )
    with TestClient(app) as client:
        r = client.get("/api/players")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["uid"] == "100"
    assert data[0]["platform"] == "PC"


def test_create_player_triggers_snapshot(tmp_path) -> None:
    fake = FakeClient()
    app = _bootstrap(tmp_path, fake=fake)
    with TestClient(app) as client:
        r = client.post("/api/players", json={"uid": "100", "platform": "PC"})
    assert r.status_code == 201
    assert r.json()["uid"] == "100"
    assert fake.calls == 1  # initial snapshot fired

    snaps = _query_snapshots(_db_path(tmp_path))
    assert len(snaps) == 1
    assert snaps[0].level == 474


def test_create_player_idempotent_returns_existing(tmp_path) -> None:
    fake = FakeClient()
    app = _bootstrap(tmp_path, fake=fake)
    with TestClient(app) as client:
        r1 = client.post("/api/players", json={"uid": "100", "platform": "PC"})
        r2 = client.post("/api/players", json={"uid": "100", "platform": "PC"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


def test_create_player_invalid_platform_rejected(tmp_path) -> None:
    app = _bootstrap(tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/players", json={"uid": "100", "platform": "Android"})
    assert r.status_code == 422


def test_untrack_player_soft_deletes(tmp_path) -> None:
    app = _bootstrap(tmp_path, seed_players=[Player(uid="100", platform="PC")])
    with TestClient(app) as client:
        r = client.delete("/api/players/1")
    assert r.status_code == 200
    assert r.json()["tracked"] is False


def test_update_player_display_name(tmp_path) -> None:
    app = _bootstrap(tmp_path, seed_players=[Player(uid="100", platform="PC")])
    with TestClient(app) as client:
        r = client.patch("/api/players/1", json={"display_name": "ARemi"})
    assert r.status_code == 200
    assert r.json()["display_name"] == "ARemi"


def test_get_current_returns_parsed_stats(tmp_path) -> None:
    app = _bootstrap(tmp_path, seed_players=[Player(uid="100", platform="PC")])
    with TestClient(app) as client:
        r = client.get("/api/players/1/current")
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == 474
    assert body["rank_name"] == "Diamond"
    assert body["rank_img"] == "http://x/diamond.png"
    assert body["kills"] == 22
    assert body["fetched_at"]


def test_get_current_cache_hits_within_ttl(tmp_path) -> None:
    fake = FakeClient()
    app = _bootstrap(
        tmp_path, fake=fake, seed_players=[Player(uid="100", platform="PC")]
    )
    with TestClient(app) as client:
        client.get("/api/players/1/current")
        client.get("/api/players/1/current")
    assert fake.calls == 1  # second call served from cache


def test_get_history_returns_points(tmp_path) -> None:
    app = _bootstrap(
        tmp_path,
        seed_players=[Player(uid="100", platform="PC")],
        seed_snapshots=[
            Snapshot(player_id=1, raw_json="{}", damage=100, level=10),
            Snapshot(player_id=1, raw_json="{}", damage=200, level=11),
        ],
    )
    with TestClient(app) as client:
        r = client.get("/api/players/1/history?metric=damage&days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["metric"] == "damage"
    assert len(body["points"]) == 2
    assert body["points"][0]["value"] == 100
    assert body["points"][1]["value"] == 200


def test_get_history_invalid_metric(tmp_path) -> None:
    app = _bootstrap(tmp_path, seed_players=[Player(uid="100", platform="PC")])
    with TestClient(app) as client:
        r = client.get("/api/players/1/history?metric=winrate")
    assert r.status_code == 400


def test_get_legends(tmp_path) -> None:
    app = _bootstrap(
        tmp_path,
        seed_players=[Player(uid="100", platform="PC")],
        seed_snapshots=[Snapshot(player_id=1, raw_json=json.dumps(SAMPLE_BRIDGE))],
    )
    with TestClient(app) as client:
        r = client.get("/api/players/1/legends")
    assert r.status_code == 200
    body = r.json()
    assert body["selected"] == "Wraith"
    assert "Wraith" in body["legends"]


def test_get_deltas(tmp_path) -> None:
    app = _bootstrap(
        tmp_path,
        seed_players=[Player(uid="100", platform="PC")],
        seed_snapshots=[
            Snapshot(player_id=1, raw_json="{}", kills=100, damage=1000, level=10, rank_score=2000),
            Snapshot(player_id=1, raw_json="{}", kills=130, damage=1200, level=10, rank_score=2100),
        ],
    )
    with TestClient(app) as client:
        r = client.get("/api/players/1/deltas?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["deltas"]["kills"] == 30
    assert body["deltas"]["damage"] == 200
    assert body["deltas"]["rank_score"] == 100
    assert "wins" not in body["deltas"]  # wins removed from the dashboard


def test_get_breakdown(tmp_path) -> None:
    raw = json.dumps({
        "total": {
            "kills": {"name": "BR Kills", "value": 100},
            "specialEvent_kills": {"name": "BR Kills", "value": 100},
            "smg_kills": {"name": "SMG Kills", "value": 50},
            "mastery_alternator_kills": {"name": "Alternator Kills", "value": 30},
            "kills_season_21": {"name": "S21 kills", "value": 5},
        },
        "legends": {"all": {
            "Octane": {"data": [
                {"name": "BR Kills", "key": "specialEvent_kills", "value": 100,
                 "rank": {"rankPos": 1000, "topPercent": 5.0},
                 "rankPlatformSpecific": {"rankPos": 500, "topPercent": 2.5}},
            ]},
            "Global": {"data": [
                {"name": "Alternator SMG Kills", "key": "mastery_alternator_kills", "value": 773,
                 "rank": {"rankPos": 1, "topPercent": 21.5},
                 "rankPlatformSpecific": {"rankPos": 1, "topPercent": 21.5}},
            ]},
        }},
    })
    app = _bootstrap(
        tmp_path,
        seed_players=[Player(uid="100", platform="PC")],
        seed_snapshots=[Snapshot(player_id=1, raw_json=raw)],
    )
    with TestClient(app) as client:
        r = client.get("/api/players/1/breakdown")
    assert r.status_code == 200
    body = r.json()
    weapon_names = [w["name"] for w in body["weapons"]]
    assert "Alternator Kills" in weapon_names  # account-wide mastery
    assert "SMG Kills" not in weapon_names      # per-legend class tracker, excluded
    assert "BR Kills" not in weapon_names       # aggregate excluded
    assert "S21 kills" not in weapon_names      # seasonal excluded
    assert len(body["trackers"]) == 1
    assert body["trackers"][0]["legend"] == "Octane"
    assert body["trackers"][0]["platform_top_pct"] == 2.5
    assert body["trackers"][0]["key"] == "specialEvent_kills"
    # 'Global' is the account-aggregate bucket, not a legend — must be excluded
    assert all(t["legend"] != "Global" for t in body["trackers"])
    assert all(l["legend"] != "Global" for l in body["legends"])
    # legend rollup → featured/main legend
    assert body["legends"][0]["legend"] == "Octane"
    assert body["legends"][0]["kills"] == 100
    assert body["legends"][0]["best_platform_pct"] == 2.5


def test_player_not_found_returns_404_error_shape(tmp_path) -> None:
    app = _bootstrap(tmp_path)
    with TestClient(app) as client:
        r = client.get("/api/players/9999/current")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == 404
