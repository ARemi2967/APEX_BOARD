"""Admin API tests: token gate + EA export upload/report endpoints.

Follows test_api.py conventions: sync TestClient over a temp-file DB seeded
with a plain sync engine; raw-body POST for the zip upload. The fixture zip
mirrors the verified real EA export format (gameDataTable).
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.config import Settings
from backend.db.base import Base
from backend.db.models import Player, Snapshot
from backend.main import create_app

TOKEN = "sekrit-admin-token"

SAMPLE_BRIDGE: dict[str, Any] = {
    "global": {"name": "TestPlayer", "uid": "100", "level": 100, "rank": {}, "battlepass": {}},
    "total": {},
    "legends": {
        "selected": {"Octane": {"data": {}}},
        "all": {
            "Octane": {"data": [
                {"key": "specialEvent_kills", "value": 4998},
                {"key": "specialEvent_damage", "value": 1792007},
            ]},
            "Lifeline": {"data": [
                {"key": "specialEvent_kills", "value": 244},
                {"key": "specialEvent_damage", "value": 109858},
            ]},
        },
    },
}


class FakeClient:
    async def get_bridge_by_uid(self, uid: str, platform: str) -> dict:
        return SAMPLE_BRIDGE

    async def close(self) -> None:
        pass


def _export_zip() -> bytes:
    doc = {
        "requestId": "Request-1",
        "date": "31-Aug-2026",
        "userData": [{
            "product": "Apex Legends",
            "subscribers": [{
                "subscriber": "prod-respawn-entertainment",
                "userData": {
                    "gameDataTable": [
                        {"name": "stats.characters[character_octane].kills", "value": "5337"},
                        {"name": "stats.characters[character_octane].damage_done", "value": "1962210"},
                        {"name": "stats.characters[character_octane].games_played", "value": "3753"},
                        {"name": "stats.characters[character_lifeline].kills", "value": "244"},
                        {"name": "stats.characters[character_lifeline].damage_done", "value": "109858"},
                    ],
                    "antiCheatSessions": [
                        {"ip": "1.2.3.4", "time_start": "2026-08-30T10:00:00.000Z",
                         "time_end": "2026-08-30T10:20:00.000Z"},
                    ],
                },
            }],
        }],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Request-1-ApexLegends.json", json.dumps(doc))
    return buf.getvalue()


def _bootstrap(tmp_path, *, admin_token: str = TOKEN) -> TestClient:
    db_path = str(tmp_path / "admin.db")
    settings = Settings(apex_api_key="k", db_path=db_path, admin_token=admin_token)
    eng = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(eng)
    with Session(eng) as session:
        player = Player(uid="100", platform="PC", username="Alice")
        session.add(player)
        session.flush()
        session.add(Snapshot(
            player_id=player.id, captured_at=datetime(2026, 8, 1),
            raw_json=json.dumps(SAMPLE_BRIDGE), kills=5242,
        ))
        session.commit()
    eng.dispose()
    app = create_app(start_scheduler=False, apex_client=FakeClient(), settings=settings)
    return TestClient(app)


def _auth(client: TestClient, token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_admin_disabled_without_token(tmp_path) -> None:
    client = _bootstrap(tmp_path, admin_token="")
    assert client.post("/api/admin/ea-import?player_id=1").status_code == 404
    assert client.get("/api/admin/ea-imports").status_code == 404


def test_admin_rejects_bad_token(tmp_path) -> None:
    client = _bootstrap(tmp_path)
    assert client.post("/api/admin/ea-import?player_id=1").status_code == 401
    assert client.get(
        "/api/admin/ea-imports", headers=_auth(client, "wrong")
    ).status_code == 401


def test_upload_import_report_and_legends_overlay(tmp_path) -> None:
    client = _bootstrap(tmp_path)

    # upload (raw body)
    resp = client.post(
        "/api/admin/ea-import?player_id=1&filename=export.zip",
        content=_export_zip(), headers=_auth(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["legend_count"] == 2
    assert body["data_end"].startswith("2026-08-30")

    # re-upload the same file → duplicate, no double import
    resp2 = client.post(
        "/api/admin/ea-import?player_id=1&filename=export.zip",
        content=_export_zip(), headers=_auth(client),
    )
    assert resp2.status_code == 200
    assert resp2.json()["duplicate"] is True

    # import history
    history = client.get("/api/admin/ea-imports", headers=_auth(client)).json()
    assert len(history) == 1
    assert history[0]["file_name"] == "export.zip"
    assert history[0]["legend_count"] == 2

    # garbage upload → 400 with a helpful message
    bad = client.post(
        "/api/admin/ea-import?player_id=1&filename=bad.zip",
        content=b"not a zip", headers=_auth(client),
    )
    assert bad.status_code == 400
    assert "zip" in bad.json()["detail"].lower()

    # reconciliation: Octane's tracker is 339 kills behind EA; Lifeline healthy
    report = client.get("/api/admin/ea-report/1", headers=_auth(client)).json()
    by_legend = {r["legend"]: r for r in report["legends"]}
    assert by_legend["Octane"]["missing_kills"] == 339
    assert by_legend["Octane"]["calibrated_kills"] == 5337
    assert by_legend["Octane"]["missing_damage"] == 170203
    assert by_legend["Lifeline"]["missing_kills"] == 0
    assert report["total_missing_kills"] == 339
    assert report["ea_as_of"].startswith("2026-08-31")

    # the public legends endpoint carries the overlay for the dashboard
    legends = client.get("/api/players/1/legends").json()
    assert legends["calibration"]["Octane"]["calibrated_kills"] == 5337
    assert "Lifeline" not in legends["calibration"]

    # unknown player → clean 404
    assert client.get("/api/admin/ea-report/99", headers=_auth(client)).status_code == 404
