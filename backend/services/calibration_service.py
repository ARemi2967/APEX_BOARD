"""Legend & weapon stat calibration: reconcile EA export ground truth vs
stale trackers.

Why: the bridge API's per-legend kills/damage are the currently-equipped
trackers' readings, and its weapon mastery (`mastery_<weapon>_*`) only exposes
a flaky subset of weapons. For rarely-played legends those readings lag
(sometimes by months), so the site undercounts. The EA export's
``gameDataTable`` carries the OFFICIAL lifetime career counters — the very
numbers the in-game trackers read — so reconciliation is a direct comparison:

    missing    = max(0, EA官方值 − 追踪器值)     per legend/weapon, per metric
    calibrated = max(追踪器值, EA官方值)

A healthy tracker matches EA exactly (missing = 0 — verified on real data);
a stale one gets topped up to the official number. Matches played AFTER the
export date aren't in EA's numbers yet, but they are in the (live) tracker —
``max()`` keeps whichever is ahead, so the calibration never rolls a fresh
tracker back. Re-importing a newer export refreshes the EA baseline.

EA anonymizes some character ids (``unknown``); those counters can't be
attributed to a legend and are surfaced separately as ``unattributed``.
Weapon mastery trackers only ever show some weapons — EA-only weapons simply
appear as new rows (the big win: Mastiff/Flatline/etc. the site never saw).

Results are computed on read (never stored), cached against the latest
snapshot/import ids so per-request cost stays low.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.db.models import EaLegendStat, EaWeaponStat, Snapshot
from backend.services.ea_export_service import (
    ea_character_to_bridge,
    ea_weapon_display,
    latest_import_id,
)

logger = logging.getLogger(__name__)


@dataclass
class LegendCalibration:
    legend: str
    tracker_kills: int | None = None
    tracker_damage: int | None = None
    ea_kills: int | None = None
    ea_damage: int | None = None
    ea_games_played: int | None = None
    missing_kills: int = 0
    missing_damage: int = 0
    calibrated_kills: int | None = None
    calibrated_damage: int | None = None
    has_tracker: bool = False

    @property
    def is_calibrated(self) -> bool:
        return self.missing_kills > 0 or self.missing_damage > 0


@dataclass
class CalibrationReport:
    player_id: int
    ea_as_of: datetime | None = None
    total_missing_kills: int = 0
    total_missing_damage: int = 0
    # EA counters whose character id was anonymized — can't be attributed.
    unattributed_kills: int = 0
    unattributed_damage: int = 0
    legends: list[LegendCalibration] = field(default_factory=list)


def _legend_kd(data: dict, name: str) -> tuple[int | None, int | None]:
    """kills/damage for one legend from a snapshot's raw bridge payload
    (same preference order as everywhere: specialEvent_* first)."""
    info = ((data.get("legends") or {}).get("all") or {}).get(name) or {}
    trackers = info.get("data") or []
    by_key: dict = {
        t.get("key"): t.get("value") for t in trackers if isinstance(t, dict)
    }

    def read(primary: str, fallback: str) -> int | None:
        for key in (primary, fallback):
            value = by_key.get(key)
            if value is None:
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
        return None

    return read("specialEvent_kills", "kills"), read("specialEvent_damage", "damage")


# Per-DB cache: key = (db identity, player_id) → (snapshot id, import id,
# report). The db discriminator matters because tests run many temp databases
# in one process — player ids collide across them. In production there is a
# single engine, so the extra key element is a no-op there.
_cache: dict[tuple, tuple[int | None, int | None, CalibrationReport]] = {}


def _cache_key(session_factory: async_sessionmaker, player_id: int) -> tuple:
    bind = getattr(session_factory, "kw", {}).get("bind")
    db = getattr(getattr(bind, "url", None), "database", None)
    return (str(db) if db else "<unknown-db>", player_id)


async def compute_calibration(
    session_factory: async_sessionmaker, player_id: int, use_cache: bool = True
) -> CalibrationReport | None:
    """Reconciliation report for one player, or None when there are no
    snapshots to reconcile against."""
    async with session_factory() as session:
        latest_snap = (
            await session.execute(
                select(Snapshot.id, Snapshot.raw_json)
                .where(Snapshot.player_id == player_id)
                .order_by(Snapshot.captured_at.desc())
                .limit(1)
            )
        ).first()
        if latest_snap is None:
            return None

    last_import = await latest_import_id(session_factory, player_id)
    key = _cache_key(session_factory, player_id)
    if use_cache:
        cached = _cache.get(key)
        if cached is not None and cached[0] == latest_snap.id and cached[1] == last_import:
            return cached[2]

    report = CalibrationReport(player_id=player_id)

    # latest tracker readings (the site's current view)
    latest_data = json.loads(latest_snap.raw_json)
    latest_all = (latest_data.get("legends") or {}).get("all") or {}

    if last_import is not None:
        async with session_factory() as session:
            stat_rows = (
                await session.execute(
                    select(EaLegendStat).where(EaLegendStat.import_id == last_import)
                )
            ).scalars().all()

        ea: dict[str, EaLegendStat] = {}
        for row in stat_rows:
            name = ea_character_to_bridge(row.legend)
            if name is None:  # anonymized character — count, don't attribute
                report.unattributed_kills += row.kills or 0
                report.unattributed_damage += row.damage or 0
                if row.as_of is not None and report.ea_as_of is None:
                    report.ea_as_of = row.as_of
                continue
            if name in ea:
                continue  # first row per legend wins (one per import anyway)
            ea[name] = row
            if report.ea_as_of is None:
                report.ea_as_of = row.as_of

        for name in sorted(set(ea) | {n for n in latest_all if n != "Global"}):
            stat = ea.get(name)
            tk, td = _legend_kd(latest_data, name)
            if stat is None and tk is None and td is None:
                continue  # nothing to reconcile
            row = LegendCalibration(
                legend=name,
                tracker_kills=tk,
                tracker_damage=td,
                ea_kills=stat.kills if stat is not None else None,
                ea_damage=stat.damage if stat is not None else None,
                ea_games_played=stat.games_played if stat is not None else None,
                has_tracker=(tk is not None or td is not None),
            )
            row.missing_kills = max(0, (row.ea_kills or 0) - (tk or 0)) if row.ea_kills is not None else 0
            row.missing_damage = max(0, (row.ea_damage or 0) - (td or 0)) if row.ea_damage is not None else 0
            row.calibrated_kills = max(tk or 0, row.ea_kills or 0) if (tk is not None or row.ea_kills is not None) else None
            row.calibrated_damage = max(td or 0, row.ea_damage or 0) if (td is not None or row.ea_damage is not None) else None
            if row.missing_kills or row.missing_damage:
                report.total_missing_kills += row.missing_kills
                report.total_missing_damage += row.missing_damage
            elif stat is None:
                continue  # tracker-only legend, EA has nothing to say — noise
            report.legends.append(row)

        report.legends.sort(
            key=lambda r: (r.missing_damage + r.missing_kills * 300, r.ea_games_played or 0),
            reverse=True,
        )

    _cache[key] = (latest_snap.id, last_import, report)
    return report


def calibration_overlay(report: CalibrationReport | None) -> dict[str, dict[str, int]]:
    """Legend→{calibrated_kills, calibrated_damage, missing_*} map used by the
    /legends endpoint so the dashboard can render calibrated values in place."""
    if report is None:
        return {}
    out: dict[str, dict[str, int]] = {}
    for row in report.legends:
        if not row.is_calibrated:
            continue  # nothing to fix — omit so the payload stays small
        out[row.legend] = {
            "calibrated_kills": row.calibrated_kills or 0,
            "calibrated_damage": row.calibrated_damage or 0,
            "missing_kills": row.missing_kills,
            "missing_damage": row.missing_damage,
        }
    return out


# --- weapon calibration --------------------------------------------------------
#
# Same direct-comparison idea, joined on the mastery tracker short id
# (`mastery_<short>_kills`). EA-only weapons (the site's trackers never expose
# them) appear as new rows; headshots/shots/hits ride along for accuracy.

# how many recent snapshots to scan for the (flaky) mastery values
_MASTERY_SNAPSHOT_SCAN = 20


def _short_id_display_map() -> dict[str, str]:
    from backend.services.ea_export_service import WEAPON_ID_MAP

    return {short: display for short, display in WEAPON_ID_MAP.values()}


_SHORT_ID_TO_DISPLAY: dict[str, str] = _short_id_display_map()


@dataclass
class WeaponCalibration:
    weapon: str                      # canonical display name (WEAPON_CN key)
    short_id: str                    # mastery tracker join id
    tracker_kills: int | None = None
    tracker_damage: int | None = None
    ea_kills: int | None = None
    ea_damage: int | None = None
    headshots: int | None = None
    shots: int | None = None
    hits: int | None = None
    missing_kills: int = 0
    missing_damage: int = 0
    calibrated_kills: int | None = None
    calibrated_damage: int | None = None

    @property
    def is_calibrated(self) -> bool:
        return self.missing_kills > 0 or self.missing_damage > 0


@dataclass
class WeaponCalibrationReport:
    player_id: int
    ea_as_of: datetime | None = None
    weapons: list[WeaponCalibration] = field(default_factory=list)


def _merge_tracker_mastery(raw_jsons: list[str]) -> dict[str, dict]:
    """Max (monotonic) mastery value per weapon short id across snapshots,
    split by metric — same strategy as the breakdown endpoint's merge. Keeps
    the tracker's display name (e.g. "Alternator SMG Kills") for continuity."""
    merged: dict[str, dict] = {}
    for raw in raw_jsons:
        try:
            total = (json.loads(raw).get("total") or {})
        except json.JSONDecodeError:
            continue
        for key, entry in total.items():
            if not isinstance(entry, dict) or not key.startswith("mastery_"):
                continue
            if key.endswith("_kills"):
                short, metric = key[len("mastery_"):-len("_kills")], "k"
            elif key.endswith("_damage_done"):
                short, metric = key[len("mastery_"):-len("_damage_done")], "d"
            else:
                continue
            value = entry.get("value")
            if not isinstance(value, int) or value <= 0:
                continue
            slot = merged.setdefault(short, {"k": 0, "d": 0, "name": None})
            if value > slot[metric]:
                slot[metric] = value
            if slot["name"] is None and isinstance(entry.get("name"), str):
                slot["name"] = entry["name"]
    return merged


_weapon_cache: dict[tuple, tuple[int | None, int | None, WeaponCalibrationReport]] = {}


async def compute_weapon_calibration(
    session_factory: async_sessionmaker, player_id: int, use_cache: bool = True
) -> WeaponCalibrationReport:
    """Weapon reconciliation rows (tracker-only + EA-only + both), kills desc."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Snapshot.id, Snapshot.raw_json)
                .where(Snapshot.player_id == player_id)
                .order_by(Snapshot.captured_at.desc())
                .limit(_MASTERY_SNAPSHOT_SCAN)
            )
        ).all()
    latest_snap_id = rows[0].id if rows else None
    raw_jsons = [r.raw_json for r in rows]

    last_import = await latest_import_id(session_factory, player_id)
    key = _cache_key(session_factory, player_id)
    if use_cache:
        cached = _weapon_cache.get(key)
        if cached is not None and cached[0] == latest_snap_id and cached[1] == last_import:
            return cached[2]

    report = WeaponCalibrationReport(player_id=player_id)
    tracker = _merge_tracker_mastery(raw_jsons) if raw_jsons else {}

    ea_by_short: dict[str, EaWeaponStat] = {}
    if last_import is not None:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(EaWeaponStat).where(EaWeaponStat.import_id == last_import)
                )
            ).scalars().all()
        for row in rows:
            display = ea_weapon_display(row.weapon)
            if display is None:
                continue  # unmapped EA weapon id — can't attribute yet
            short_id, _name = display
            if short_id not in ea_by_short:  # first row per weapon wins
                ea_by_short[short_id] = row
            if report.ea_as_of is None:
                report.ea_as_of = row.as_of

    by_short: dict[str, WeaponCalibration] = {}
    for short_id, values in tracker.items():
        # tracker-known weapons keep their upstream display name; canonical
        # names are only used for weapons the site has never seen
        display = values.get("name") or _SHORT_ID_TO_DISPLAY.get(short_id, short_id)
        by_short[short_id] = WeaponCalibration(
            weapon=display, short_id=short_id,
            tracker_kills=values["k"] or None,
            tracker_damage=values["d"] or None,
        )
    for short_id, row in ea_by_short.items():
        entry = by_short.get(short_id)
        if entry is None:
            entry = by_short[short_id] = WeaponCalibration(
                weapon=_SHORT_ID_TO_DISPLAY.get(short_id, short_id), short_id=short_id
            )
        entry.ea_kills, entry.ea_damage = row.kills, row.damage
        entry.headshots, entry.shots, entry.hits = row.headshots, row.shots, row.hits

    for entry in by_short.values():
        tk, td = entry.tracker_kills, entry.tracker_damage
        ek, ed = entry.ea_kills, entry.ea_damage
        entry.missing_kills = max(0, (ek or 0) - (tk or 0)) if ek is not None else 0
        entry.missing_damage = max(0, (ed or 0) - (td or 0)) if ed is not None else 0
        entry.calibrated_kills = max(tk or 0, ek or 0) if (tk is not None or ek is not None) else None
        entry.calibrated_damage = max(td or 0, ed or 0) if (td is not None or ed is not None) else None

    report.weapons = sorted(
        by_short.values(), key=lambda r: (r.calibrated_kills or 0), reverse=True
    )
    _weapon_cache[key] = (latest_snap_id, last_import, report)
    return report
