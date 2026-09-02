"""Legend-stat calibration: reconcile EA export ground truth vs stale trackers.

Why: the bridge API's per-legend kills/damage are the currently-equipped
trackers' readings. For rarely-played legends those readings lag (sometimes by
months), so the site undercounts. The EA export's ``gameDataTable`` carries
the OFFICIAL per-legend lifetime career counters — the very numbers the
in-game trackers read — so reconciliation is a direct comparison:

    missing    = max(0, EA官方值 − 追踪器值)     per legend, per metric
    calibrated = max(追踪器值, EA官方值)

A healthy tracker matches EA exactly (missing = 0 — verified on real data);
a stale one gets topped up to the official number. Matches played AFTER the
export date aren't in EA's numbers yet, but they are in the (live) tracker —
``max()`` keeps whichever is ahead, so the calibration never rolls a fresh
tracker back. Re-importing a newer export refreshes the EA baseline.

EA anonymizes some character ids (``unknown``); those counters can't be
attributed to a legend and are surfaced separately as ``unattributed``.

Results are computed on read (never stored), cached against the latest
snapshot/import ids so per-request cost stays low.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.db.models import EaLegendStat, Snapshot
from backend.services.ea_export_service import ea_character_to_bridge, latest_import_id

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
