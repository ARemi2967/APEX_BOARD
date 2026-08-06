"""Snapshot service: fetch a player's bridge profile and persist a Snapshot.

Parsing is deliberately tolerant — apexlegendsapi.com's `total` tracker set is
frequently partially unpopulated (missing keys, -1 sentinels, empty names).
See the apexlegendsapi.com quirks memory.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.db.models import Player, Snapshot
from backend.integrations.apex_api import ApexClient

logger = logging.getLogger(__name__)

# wins/matches come from the `total` aggregate. kills/damage are summed across
# legends separately (see _sum_legend_metric) — total.specialEvent_* is only ONE
# legend's value, not the account total.
_TOTAL_CANDIDATES = {
    "wins": ["specialEvent_wins", "wins"],
    "matches_played": ["matches", "matches_played"],
}

# Lifetime counters are monotonically non-decreasing. The upstream tracker
# read is flaky — a fetch that omits a tracker (None) or returns a stale lower
# value should carry forward the last known good value, so the trend doesn't
# show fake cliffs. rank_score is excluded (it goes up AND down with play).
_MONOTONIC_COLUMNS = ("kills", "damage", "wins", "matches_played", "level")


def _smooth_monotonic(parsed: dict[str, Any], prev: Snapshot | None) -> None:
    """In-place: carry forward previous values for monotonic counters that
    regressed (None or lower) in the current fetch."""
    if prev is None:
        return
    for col in _MONOTONIC_COLUMNS:
        new = parsed.get(col)
        old = getattr(prev, col, None)
        if old is not None and (new is None or (new is not None and new < old)):
            parsed[col] = old


def parse_bridge(data: dict[str, Any]) -> dict[str, Any]:
    """Extract the columns we snapshot from a raw bridge response.

    Returns a dict of column→value suitable for `Snapshot(**parsed)`. Missing
    or unpopulated fields become None rather than raising.
    """
    g = data.get("global") or {}
    rank = g.get("rank") or {}
    bp = g.get("battlepass") or {}
    total = data.get("total") or {}

    parsed: dict[str, Any] = {
        "level": _as_int(g.get("level")),
        "rank_name": rank.get("rankName"),
        "rank_div": _as_int(rank.get("rankDiv")),
        "rank_score": _as_int(rank.get("rankScore")),
        "ranked_season": rank.get("rankedSeason"),
        "bp_level": _as_int(bp.get("level")),
    }
    # kills/damage = sum across all real legends (account total)
    parsed["kills"] = _sum_legend_metric(data, "specialEvent_kills", "kills")
    parsed["damage"] = _sum_legend_metric(data, "specialEvent_damage", "damage")
    for column, candidates in _TOTAL_CANDIDATES.items():
        parsed[column] = _first_present(total, candidates)
    return parsed


def _tracker_value(trackers: Any, key: str) -> int | None:
    """Find a tracker's value by key; `data` may be a list or a dict of trackers."""
    if isinstance(trackers, dict):
        trackers = list(trackers.values())
    for t in trackers or []:
        if isinstance(t, dict) and t.get("key") == key:
            return _as_int(t.get("value"))
    return None


def _sum_legend_metric(data: dict[str, Any], primary_key: str, fallback_key: str) -> int | None:
    """Sum a metric across all real legends (prefer primary key, fall back).
    'Global' (the account-aggregate bucket) is excluded — it's not a legend."""
    total_sum = 0
    found = False
    for name, info in ((data.get("legends") or {}).get("all") or {}).items():
        if name == "Global" or not isinstance(info, dict):
            continue
        value = _tracker_value(info.get("data"), primary_key)
        if value is None:
            value = _tracker_value(info.get("data"), fallback_key)
        if value is not None:
            total_sum += value
            found = True
    return total_sum if found else None


def _first_present(total: Any, candidates: list[str]) -> int | None:
    """Return the first non-null counter value among candidate `total` keys."""
    for key in candidates:
        value = _total_value(total, key)
        if value is not None:
            return value
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _total_value(total: Any, key: str) -> int | None:
    """Read `total[key]["value"]`, coercing the -1 missing-data sentinel to None."""
    entry = total.get(key) if isinstance(total, dict) else None
    if not isinstance(entry, dict):
        return None
    value = _as_int(entry.get("value"))
    if value is None or value < 0:
        return None
    return value


async def capture_snapshot(
    client: ApexClient,
    session_factory: async_sessionmaker,
    player: Player,
) -> Snapshot:
    """Fetch one player's profile by UID and insert a Snapshot row.

    Also backfills `player.username` if the upstream now has a name and we
    didn't (common for Steam-linked accounts with an empty indexed name).

    Raises ApexError if the upstream fetch fails; the caller decides whether
    to skip or abort.
    """
    data = await client.get_bridge_by_uid(player.uid, player.platform)
    parsed = parse_bridge(data)
    name = (data.get("global") or {}).get("name") or None

    async with session_factory() as session:
        prev = (
            await session.execute(
                select(Snapshot)
                .where(Snapshot.player_id == player.id)
                .order_by(Snapshot.captured_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        _smooth_monotonic(parsed, prev)

        snap = Snapshot(
            player_id=player.id,
            raw_json=json.dumps(data),
            **parsed,
        )
        session.add(snap)
        if name:
            db_player = await session.get(Player, player.id)
            if db_player is not None and not db_player.username:
                db_player.username = name
        await session.commit()
        await session.refresh(snap)

    logger.info(
        "snapshot captured player_id=%s uid=%s level=%s rank=%s",
        player.id, player.uid, parsed["level"], parsed["rank_name"],
    )
    return snap
