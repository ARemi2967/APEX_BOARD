"""Tests for EA export ingestion + legend calibration.

Fixture zips mirror the verified real-world export format (2026-09):
``Request-<id>-ApexLegends.json`` with userData → subscribers → gameDataTable
(flat ``stats.characters[<char>].<stat>`` rows) + antiCheatSessions. A
match-history style zip (bare match list) is also covered for future-proofing.

Calibration tests use use_cache=False — the module cache is per-process.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime

import pytest
from sqlalchemy import select

from backend.db.base import Base
from backend.db.models import EaLegendStat, EaMatch, EaImport, Player, Snapshot
from backend.db.session import make_engine, make_session_factory
from backend.services.calibration_service import (
    calibration_overlay,
    compute_calibration,
)
from backend.services.ea_export_service import (
    EaExportError,
    ea_character_to_bridge,
    import_ea_export,
    normalize_legend_name,
    parse_export_zip,
    parse_match_entry,
    parse_timestamp,
)


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _real_format_export(stats_rows: list[dict], sessions: list[dict] | None = None,
                        date: str = "31-Aug-2026") -> bytes:
    """Zip mimicking the actual EA export layout."""
    doc = {
        "requestId": "Request-1",
        "date": date,
        "userData": [{
            "product": "Apex Legends",
            "platform": "PC",
            "subscribers": [{
                "subscriber": "prod-respawn-entertainment",
                "userData": {
                    "gameDataTableHeaders": ["name", "value"],
                    "gameDataTable": stats_rows,
                    "antiCheatSessionsHeaders": ["ip", "time_end", "time_start"],
                    "antiCheatSessions": sessions or [],
                },
            }],
        }],
    }
    return _zip_bytes({
        "Request-1-EA-Account-Info.json": json.dumps({"persona": {"email": "x@y.z"}}),
        "Request-1-ApexLegends.json": json.dumps(doc),
    })


def _char_stat(char: str, stat: str, value) -> dict:
    return {"name": f"stats.characters[{char}].{stat}", "value": str(value)}


OCTANE_ROWS = [
    _char_stat("character_octane", "kills", 5337),
    _char_stat("character_octane", "damage_done", 1962210),
    _char_stat("character_octane", "games_played", 3753),
    _char_stat("character_lifeline", "kills", 244),
    _char_stat("character_lifeline", "damage_done", 109858),
    _char_stat("character_unknown", "kills", 829),       # anonymized bucket
    _char_stat("character_unknown", "damage_done", 300273),
    # decoys: deeper nesting / other tables must NOT become legend stats
    {"name": "stats.characters[character_octane].weaponcategories[smg].kills", "value": "2419"},
    {"name": "stats.characters[character_octane].kills_max_single_game", "value": "19"},
    {"name": "stats.seasons[unknown].characters[character_octane].kills", "value": "300"},
    {"name": "stats.kills", "value": "572"},
]

SESSIONS = [
    {"ip": "1.2.3.4", "time_start": "2025-09-05T10:00:00.000Z", "time_end": "2025-09-05T10:25:00.000Z"},
    {"ip": "1.2.3.4", "time_start": "2026-08-31T11:00:00.000Z", "time_end": "2026-08-31T11:23:36.269Z"},
]


def _match_zip() -> bytes:
    """Hypothetical match-history format (kept for forward compatibility)."""
    return _zip_bytes({
        "Apex Legends/match histories/match_history_0.json": json.dumps({
            "matches": [
                {"sessionId": "s1", "completionDate": "2026-06-01T12:00:00Z",
                 "character": "wraith", "stats": {"kills": 3, "damageDealt": 900}},
                {"sessionId": "s2", "completionDate": "2026-06-02T12:00:00Z",
                 "character": "madmaggie", "stats": {"kills": 7, "damageDealt": 1234.6}},
            ]
        }),
    })


# --- pure parsing -------------------------------------------------------------


def test_parse_timestamp_variants() -> None:
    assert parse_timestamp("2026-06-01T12:00:00.000Z") == datetime(2026, 6, 1, 12)
    assert parse_timestamp(1_780_000_000) == datetime.utcfromtimestamp(1_780_000_000)
    assert parse_timestamp("1780000000000") == datetime.utcfromtimestamp(1_780_000_000)
    assert parse_timestamp("garbage") is None
    assert parse_timestamp(None) is None


def test_legend_name_normalization() -> None:
    assert normalize_legend_name("wraith") == "Wraith"
    assert normalize_legend_name("madmaggie") == "Mad Maggie"
    assert ea_character_to_bridge("character_octane") == "Octane"
    assert ea_character_to_bridge("character_immortal") == "Revenant"  # internal codename
    assert ea_character_to_bridge("character_madmaggie") == "Mad Maggie"
    assert ea_character_to_bridge("character_unknown") is None  # anonymized
    assert ea_character_to_bridge("unknown") is None
    assert ea_character_to_bridge("character_sparrow") == "Sparrow"


def test_parse_real_format_export() -> None:
    payload = parse_export_zip(_real_format_export(OCTANE_ROWS, SESSIONS))

    by_key = {s.raw_key: s for s in payload.legend_stats}
    assert by_key["character_octane"].kills == 5337
    assert by_key["character_octane"].damage == 1962210
    assert by_key["character_octane"].games_played == 3753
    assert by_key["character_lifeline"].kills == 244
    assert by_key["character_unknown"].kills == 829
    assert len(payload.legend_stats) == 3  # decoy keys excluded
    assert payload.matches == []

    assert payload.as_of == datetime(2026, 8, 31)          # from `date`
    assert payload.session_start == datetime(2025, 9, 5, 10)
    assert payload.session_end == datetime(2026, 8, 31, 11, 23, 36, 269000)
    assert payload.used_files == ["Request-1-ApexLegends.json"]


def test_parse_match_format_still_works() -> None:
    payload = parse_export_zip(_match_zip())
    assert len(payload.matches) == 2
    first = payload.matches[0]
    assert first.match_key == "s1"
    assert first.legend == "Wraith"
    assert first.damage == 900
    second = payload.matches[1]
    assert second.legend == "Mad Maggie"
    assert second.damage == 1235  # rounded float


def test_parse_garbage_raises() -> None:
    with pytest.raises(EaExportError):
        parse_export_zip(b"not a zip at all")
    with pytest.raises(EaExportError):
        parse_export_zip(_zip_bytes({"readme.txt": "hello"}))


# --- import (idempotency) ------------------------------------------------------


async def _make_db(tmp_path, filename: str = "ea.db"):
    engine = make_engine(str(tmp_path / filename))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    async with sf() as session:
        player = Player(uid="100", platform="PC", username="Alice")
        session.add(player)
        await session.commit()
        await session.refresh(player)
    return engine, sf, player


async def test_import_is_idempotent_by_hash(tmp_path) -> None:
    engine, sf, player = await _make_db(tmp_path)
    data = _real_format_export(OCTANE_ROWS, SESSIONS)

    first = await import_ea_export(sf, player.id, "export.zip", data)
    assert first["duplicate"] is False
    assert first["legend_count"] == 3
    assert first["match_count"] == 0
    assert first["data_start"] == datetime(2025, 9, 5, 10)

    again = await import_ea_export(sf, player.id, "export.zip", data)
    assert again["duplicate"] is True
    assert again["import_id"] == first["import_id"]

    async with sf() as session:
        assert len((await session.execute(select(EaLegendStat))).scalars().all()) == 3
        assert len((await session.execute(select(EaImport))).scalars().all()) == 1
    await engine.dispose()


async def test_overlapping_import_upserts_not_duplicates(tmp_path) -> None:
    engine, sf, player = await _make_db(tmp_path, "ea2.db")
    await import_ea_export(sf, player.id, "old.zip", _match_zip())
    await import_ea_export(sf, player.id, "new.zip", _match_zip())  # same matches, new file

    async with sf() as session:
        keys = sorted(m.match_key for m in (await session.execute(select(EaMatch))).scalars().all())
    assert keys == ["s1", "s2"]  # union, no double-count
    await engine.dispose()


# --- calibration (direct lifetime comparison) ----------------------------------


def _snap_raw(trackers: dict[str, tuple[int | None, int | None]]) -> str:
    all_legends = {
        name: {"data": (
            [{"key": "specialEvent_kills", "value": k}] if k is not None else []) +
            ([{"key": "specialEvent_damage", "value": d}] if d is not None else [])
        }
        for name, (k, d) in trackers.items()
    }
    return json.dumps({"legends": {"all": all_legends}})


async def _seed(tmp_path, trackers, legend_stat_rows):
    engine = make_engine(str(tmp_path / "cal.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    async with sf() as session:
        player = Player(uid="100", platform="PC")
        session.add(player)
        await session.flush()
        session.add(Snapshot(player_id=player.id, captured_at=datetime(2026, 8, 10),
                             raw_json=_snap_raw(trackers)))
        imp = EaImport(player_id=player.id, file_name="x.zip", file_hash="h1",
                       match_count=0, legend_count=len(legend_stat_rows))
        session.add(imp)
        await session.flush()
        for raw_key, k, d, g in legend_stat_rows:
            session.add(EaLegendStat(import_id=imp.id, player_id=player.id,
                                     legend=raw_key, kills=k, damage=d,
                                     games_played=g, as_of=datetime(2026, 8, 31)))
        await session.commit()
        await session.refresh(player)
    return engine, sf, player


# Octane's tracker is stale (site 4998 vs EA 5337); Lifeline healthy; Crypto has
# no tracker at all; the anonymized bucket must not be attributed.
_SEED_TRACKERS = {
    "Octane": (4998, 1792007),
    "Lifeline": (244, 109858),
    "Crypto": (None, None),
}
_SEED_EA = [
    ("character_octane", 5337, 1962210, 3753),
    ("character_lifeline", 244, 109858, 450),
    ("character_crypto", 15, 3000, 20),
    ("character_unknown", 829, 300273, 721),
]


async def test_calibration_direct_comparison(tmp_path) -> None:
    engine, sf, player = await _seed(tmp_path, _SEED_TRACKERS, _SEED_EA)
    report = await compute_calibration(sf, player.id, use_cache=False)

    assert report.ea_as_of == datetime(2026, 8, 31)
    by = {r.legend: r for r in report.legends}

    stale = by["Octane"]
    assert stale.missing_kills == 339            # 5337 − 4998
    assert stale.missing_damage == 170203        # 1962210 − 1792007
    assert stale.calibrated_kills == 5337
    assert stale.calibrated_damage == 1962210
    assert stale.is_calibrated

    healthy = by["Lifeline"]
    assert healthy.missing_kills == 0 and healthy.missing_damage == 0
    assert healthy.calibrated_kills == 244
    assert not healthy.is_calibrated

    invisible = by["Crypto"]                     # site has no tracker numbers
    assert invisible.has_tracker is False
    assert invisible.calibrated_kills == 15
    assert invisible.calibrated_damage == 3000

    assert report.total_missing_kills == 339 + 15
    assert report.total_missing_damage == 170203 + 3000
    # anonymized bucket is surfaced, never attributed
    assert report.unattributed_kills == 829
    assert report.unattributed_damage == 300273
    assert "unknown" not in by

    overlay = calibration_overlay(report)
    assert overlay["Octane"]["calibrated_kills"] == 5337
    assert overlay["Crypto"]["calibrated_kills"] == 15
    assert "Lifeline" not in overlay             # healthy → omitted
    await engine.dispose()


async def test_tracker_ahead_of_export_keeps_tracker(tmp_path) -> None:
    """Matches played after the export date live in the tracker only — the
    calibration must keep the higher (fresher) value, not roll it back."""
    trackers = {"Octane": (5500, 2000000)}       # tracker moved past EA
    ea = [("character_octane", 5337, 1962210, 3753)]
    engine, sf, player = await _seed(tmp_path, trackers, ea)

    report = await compute_calibration(sf, player.id, use_cache=False)
    row = report.legends[0]
    assert row.missing_kills == 0
    assert row.calibrated_kills == 5500          # max(tracker, EA)
    assert calibration_overlay(report) == {}
    await engine.dispose()


async def test_calibration_empty_without_import(tmp_path) -> None:
    engine, sf, player = await _seed(tmp_path, {"Octane": (10, 100)}, [])
    report = await compute_calibration(sf, player.id, use_cache=False)
    assert report.legends == []
    assert calibration_overlay(report) == {}
    await engine.dispose()
