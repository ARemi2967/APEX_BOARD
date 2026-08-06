"""HTTP routes for the dashboard API.

All endpoints live under `/api`. State (session factory, Apex client, current-
stats cache) is read from `app.state` via Depends, so tests can swap in a temp
DB + fake client by building the app with `create_app(...)`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.db.models import Player, Snapshot
from backend.integrations.apex_api import ApexClient
from backend.integrations.exceptions import ApexError, PlayerNotFoundError
from backend.services.snapshot_service import capture_snapshot, parse_bridge

from .schemas import (
    BreakdownOut,
    CurrentStats,
    DayStat,
    DeltasOut,
    HistoryOut,
    HistoryPoint,
    LegendActivity,
    LegendDaily,
    LegendRollup,
    LegendsOut,
    PlayerCreate,
    PlayerOut,
    PlayerUpdate,
    TrackerPercentile,
    WeaponStat,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# metric query param -> Snapshot column (for the /history endpoint).
_METRIC_COLUMNS = {
    "kills": Snapshot.kills,
    "damage": Snapshot.damage,
    "wins": Snapshot.wins,
    "matches_played": Snapshot.matches_played,
    "rank_score": Snapshot.rank_score,
    "level": Snapshot.level,
}


class TTLCache:
    """Tiny async-safe TTL cache for the /current live proxy."""

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._store: dict[Any, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: Any) -> Any:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts < self._ttl:
                return value
            return None

    async def set(self, key: Any, value: Any) -> None:
        async with self._lock:
            self._store[key] = (time.monotonic(), value)


# --- dependencies (read app.state, populated by create_app) ---

def get_session_factory(request: Request) -> async_sessionmaker:
    return request.app.state.session_factory


def get_apex_client(request: Request) -> ApexClient | None:
    return request.app.state.apex_client


def get_current_cache(request: Request) -> TTLCache:
    return request.app.state.current_cache


def get_display_name_override(request: Request) -> str | None:
    return request.app.state.display_name_override


def _require_client(client: ApexClient | None) -> ApexClient:
    if client is None:
        raise HTTPException(status_code=503, detail="APEX_API_KEY not configured")
    return client


# --- routes ---

@router.get("/players", response_model=list[PlayerOut])
async def list_players(
    sf: async_sessionmaker = Depends(get_session_factory),
) -> list[Player]:
    async with sf() as session:
        result = await session.execute(select(Player).order_by(Player.id))
        return list(result.scalars().all())


@router.post("/players", response_model=PlayerOut, status_code=201)
async def create_player(
    body: PlayerCreate,
    sf: async_sessionmaker = Depends(get_session_factory),
    client: ApexClient | None = Depends(get_apex_client),
) -> Player:
    async with sf() as session:
        existing = (
            await session.execute(
                select(Player).where(
                    Player.uid == body.uid, Player.platform == body.platform
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if not existing.tracked:  # re-track a soft-deleted player
                existing.tracked = True
                await session.commit()
                await session.refresh(existing)
            return existing
        player = Player(
            uid=body.uid, platform=body.platform, username=body.username,
            display_name=body.display_name,
        )
        session.add(player)
        await session.commit()
        await session.refresh(player)

    # Best-effort first snapshot; failure must not fail player creation.
    if client is not None:
        try:
            await capture_snapshot(client, sf, player)
        except ApexError as exc:
            logger.warning("initial snapshot failed for uid=%s: %s", body.uid, exc)
    return player


@router.delete("/players/{player_id}", response_model=PlayerOut)
async def untrack_player(
    player_id: int,
    sf: async_sessionmaker = Depends(get_session_factory),
) -> Player:
    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        player.tracked = False  # soft delete — keep historical snapshots
        await session.commit()
        await session.refresh(player)
        return player


@router.patch("/players/{player_id}", response_model=PlayerOut)
async def update_player(
    player_id: int,
    body: PlayerUpdate,
    sf: async_sessionmaker = Depends(get_session_factory),
) -> Player:
    """Update editable player fields (display name, username, tracked)."""
    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        if body.display_name is not None:
            player.display_name = body.display_name
        if body.username is not None:
            player.username = body.username
        if body.tracked is not None:
            player.tracked = body.tracked
        await session.commit()
        await session.refresh(player)
        return player


@router.get("/players/{player_id}/current", response_model=CurrentStats)
async def get_current(
    player_id: int,
    sf: async_sessionmaker = Depends(get_session_factory),
    client: ApexClient | None = Depends(get_apex_client),
    cache: TTLCache = Depends(get_current_cache),
    override: str | None = Depends(get_display_name_override),
) -> Any:
    client = _require_client(client)

    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        uid, platform, display_name = player.uid, player.platform, player.display_name

    cached = await cache.get(player_id)
    if cached is not None:
        return cached

    data = await client.get_bridge_by_uid(uid, platform)
    parsed = parse_bridge(data)
    g = data.get("global") or {}
    rank = g.get("rank") or {}
    rt = data.get("realtime") or {}
    stats = CurrentStats(
        uid=uid,
        name=(override or display_name or g.get("name") or None),
        level=parsed["level"],
        rank_name=parsed["rank_name"],
        rank_div=parsed["rank_div"],
        rank_score=parsed["rank_score"],
        rank_img=rank.get("rankImg") or None,
        bp_level=parsed["bp_level"],
        kills=parsed["kills"],
        damage=parsed["damage"],
        wins=parsed["wins"],
        is_online=bool(rt.get("isOnline")),
        is_in_game=bool(rt.get("isInGame")),
        selected_legend=rt.get("selectedLegend") or None,
        state_text=rt.get("currentStateAsText") or None,
        fetched_at=datetime.utcnow(),
    )
    await cache.set(player_id, stats)
    return stats


@router.get("/players/{player_id}/history", response_model=HistoryOut)
async def get_history(
    player_id: int,
    metric: str = Query("damage"),
    days: int = Query(7, ge=1, le=90),
    sf: async_sessionmaker = Depends(get_session_factory),
) -> HistoryOut:
    column = _METRIC_COLUMNS.get(metric)
    if column is None:
        raise HTTPException(
            status_code=400,
            detail=f"invalid metric {metric!r}; one of {sorted(_METRIC_COLUMNS)}",
        )
    since = _beijing_since(days)
    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        rows = (
            await session.execute(
                select(Snapshot.captured_at, column, Snapshot.ranked_season)
                .where(
                    Snapshot.player_id == player_id,
                    Snapshot.captured_at >= since,
                )
                .order_by(Snapshot.captured_at)
            )
        ).all()
    points = [
        HistoryPoint(captured_at=captured_at, value=value, ranked_season=season)
        for captured_at, value, season in rows
    ]
    points = _dedup_points(points)
    return HistoryOut(player_id=player_id, metric=metric, days=days, points=points)


@router.get("/players/{player_id}/legends", response_model=LegendsOut)
async def get_legends(
    player_id: int,
    sf: async_sessionmaker = Depends(get_session_factory),
) -> LegendsOut:
    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        snap = (
            await session.execute(
                select(Snapshot)
                .where(Snapshot.player_id == player_id)
                .order_by(Snapshot.captured_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots yet for this player")

    data = json.loads(snap.raw_json)
    legends = data.get("legends") or {}
    selected_dict = legends.get("selected") or {}
    selected_name = next(iter(selected_dict)) if isinstance(selected_dict, dict) and selected_dict else None
    all_legends = legends.get("all") or {}
    return LegendsOut(player_id=player_id, selected=selected_name, legends=all_legends)


_DELTA_METRICS = ("rank_score", "level", "kills", "damage")


def _beijing_since(days: int) -> datetime:
    """Beijing-midnight (UTC+8) anchored window start. days=1 → today midnight."""
    beijing_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(beijing_tz)
    midnight_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight_bj - timedelta(days=days - 1)).astimezone(timezone.utc).replace(tzinfo=None)


def _beijing_date_str(dt: datetime) -> str:
    """Beijing (UTC+8) date string from a naive UTC datetime."""
    return dt.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8))).date().isoformat()


def _rank_delta_within_season(rows: list) -> int | None:
    """RP change in the CURRENT season only. Baseline = first post-placement
    (RP>0) snapshot of the current season. Skips the season reset (cross-season)
    and the placement allocation (the 0→placement step)."""
    if not rows:
        return None
    last = rows[-1]
    cur_season = last.ranked_season
    baseline = None
    for r in rows:
        if r.ranked_season == cur_season and r.rank_score and r.rank_score > 0:
            baseline = r.rank_score
            break
    if baseline is None or last.rank_score is None:
        return None
    return last.rank_score - baseline


def _dedup_points(points: list) -> list:
    """Keep only turning points for chart rendering — reduces noise from flat
    runs of identical snapshots. Always keeps first + last; for the middle,
    keeps points where the value changes from the previous or to the next."""
    if len(points) <= 2:
        return points
    result = [points[0]]
    for i in range(1, len(points) - 1):
        if points[i].value != points[i - 1].value or points[i].value != points[i + 1].value:
            result.append(points[i])
    result.append(points[-1])
    return result


def _compute_deltas_from_rows(rows: list) -> dict[str, int | None]:
    """Compute per-metric delta from a list of snapshot rows."""
    if not rows:
        return {m: None for m in _DELTA_METRICS}
    first, last = rows[0], rows[-1]
    deltas: dict[str, int | None] = {}
    for metric in _DELTA_METRICS:
        if metric == "rank_score":
            deltas[metric] = _rank_delta_within_season(rows)
        else:
            a = getattr(first, metric)
            b = getattr(last, metric)
            deltas[metric] = (b - a) if (a is not None and b is not None) else None
    return deltas


@router.get("/players/{player_id}/deltas", response_model=DeltasOut)
async def get_deltas(
    player_id: int,
    days: int = Query(1, ge=1, le=90),
    sf: async_sessionmaker = Depends(get_session_factory),
) -> DeltasOut:
    """Per-metric change over the last `days` days, anchored to Beijing midnight
    (UTC+8). Also returns yesterday's deltas for the UI fallback."""
    beijing_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(beijing_tz)
    midnight_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    today_since = (midnight_bj - timedelta(days=days - 1)).astimezone(timezone.utc).replace(tzinfo=None)
    query_since = (midnight_bj - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)

    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        rows = (
            await session.execute(
                select(
                    Snapshot.captured_at, Snapshot.rank_score, Snapshot.ranked_season,
                    Snapshot.kills, Snapshot.damage, Snapshot.level,
                )
                .where(
                    Snapshot.player_id == player_id,
                    Snapshot.captured_at >= query_since,
                )
                .order_by(Snapshot.captured_at)
            )
        ).all()
    if not rows:
        return DeltasOut(
            player_id=player_id, days=days,
            deltas={m: None for m in _DELTA_METRICS},
        )

    # Split into today (Beijing-midnight anchored) and yesterday (for fallback)
    today_rows = [r for r in rows if r.captured_at >= today_since]
    yesterday_rows = [r for r in rows if r.captured_at < today_since]

    today_deltas = _compute_deltas_from_rows(today_rows)
    yesterday_deltas = _compute_deltas_from_rows(yesterday_rows) if len(yesterday_rows) >= 2 else None

    return DeltasOut(
        player_id=player_id, days=days,
        first_at=today_rows[0].captured_at if today_rows else None,
        last_at=today_rows[-1].captured_at if today_rows else None,
        deltas=today_deltas, yesterday=yesterday_deltas,
    )


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int_local(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@router.get("/players/{player_id}/legend-activity", response_model=list[LegendActivity])
async def get_legend_activity(
    player_id: int,
    days: int = Query(1, ge=1, le=30),
    sf: async_sessionmaker = Depends(get_session_factory),
) -> list[LegendActivity]:
    """Per-legend kills/damage change over the last `days` days (today by
    default) — diffs the earliest vs latest snapshot's raw_json per legend."""
    since = _beijing_since(days)
    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        rows = (
            await session.execute(
                select(Snapshot)
                .where(
                    Snapshot.player_id == player_id,
                    Snapshot.captured_at >= since,
                )
                .order_by(Snapshot.captured_at)
            )
        ).scalars().all()
    if len(rows) < 2:
        return []
    first_data = json.loads(rows[0].raw_json)
    last_data = json.loads(rows[-1].raw_json)

    def legend_kd(data: dict, name: str) -> tuple[int | None, int | None]:
        info = ((data.get("legends") or {}).get("all") or {}).get(name) or {}
        trackers = info.get("data") or []
        k = next((t.get("value") for t in trackers if isinstance(t, dict) and t.get("key") == "specialEvent_kills"), None)
        kf = next((t.get("value") for t in trackers if isinstance(t, dict) and t.get("key") == "kills"), None)
        d = next((t.get("value") for t in trackers if isinstance(t, dict) and t.get("key") == "specialEvent_damage"), None)
        df = next((t.get("value") for t in trackers if isinstance(t, dict) and t.get("key") == "damage"), None)
        return (k if k is not None else kf), (d if d is not None else df)

    last_all = (last_data.get("legends") or {}).get("all") or {}
    out: list[LegendActivity] = []
    for name in last_all:
        if name == "Global":
            continue
        lk, ld = legend_kd(last_data, name)
        fk, fd = legend_kd(first_data, name)
        if fk is None and fd is None:
            continue  # legend not in the baseline snapshot → can't measure today's delta
        dk = (lk or 0) - (fk or 0)
        dd = (ld or 0) - (fd or 0)
        if dk > 0 or dd > 0:
            out.append(LegendActivity(legend=name, kills=max(dk, 0), damage=max(dd, 0)))
    out.sort(key=lambda x: x.damage, reverse=True)
    return out


def _merge_mastery(recent_raw: list[str], suffix: str) -> list[WeaponStat]:
    """Merge per-weapon mastery (mastery_<w>{suffix}) across recent snapshots,
    keeping the max (monotonic) value per weapon."""
    merged: dict[str, dict] = {}
    for rj in recent_raw:
        for key, entry in (json.loads(rj).get("total") or {}).items():
            if not key.startswith("mastery_") or not key.endswith(suffix) or not isinstance(entry, dict):
                continue
            value = _as_int_local(entry.get("value"))
            if not value or value <= 0:
                continue
            cur = merged.get(key)
            if cur is None or value > cur["value"]:
                merged[key] = {"name": entry.get("name") or key, "value": value}
    out = [WeaponStat(name=m["name"], value=m["value"]) for m in merged.values()]
    out.sort(key=lambda x: x.value, reverse=True)
    return out


def _legend_kd(info: dict) -> tuple[int | None, int | None]:
    """kills/damage from a single legend's info (prefer specialEvent, fall back)."""
    trackers = info.get("data") or []
    k = next((t.get("value") for t in trackers if isinstance(t, dict) and t.get("key") == "specialEvent_kills"), None)
    kf = next((t.get("value") for t in trackers if isinstance(t, dict) and t.get("key") == "kills"), None)
    d = next((t.get("value") for t in trackers if isinstance(t, dict) and t.get("key") == "specialEvent_damage"), None)
    df = next((t.get("value") for t in trackers if isinstance(t, dict) and t.get("key") == "damage"), None)
    return (k if k is not None else kf), (d if d is not None else df)


@router.get("/players/{player_id}/legend-daily", response_model=list[LegendDaily])
async def get_legend_daily(
    player_id: int,
    days: int = Query(7, ge=1, le=30),
    sf: async_sessionmaker = Depends(get_session_factory),
) -> list[LegendDaily]:
    """Per-legend daily kills/damage over the last `days` days (records page).
    Each day's value = (last snapshot that day − first snapshot that day) per legend."""
    since = _beijing_since(days)
    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        rows = (
            await session.execute(
                select(Snapshot)
                .where(Snapshot.player_id == player_id, Snapshot.captured_at >= since)
                .order_by(Snapshot.captured_at)
            )
        ).scalars().all()
    if not rows:
        return []

    # per (legend, date) → first/last kills & damage seen that day
    agg: dict[tuple[str, str], dict] = {}
    for row in rows:
        data = json.loads(row.raw_json)
        date = _beijing_date_str(row.captured_at)
        for name, info in ((data.get("legends") or {}).get("all") or {}).items():
            if name == "Global" or not isinstance(info, dict):
                continue
            k, d = _legend_kd(info)
            e = agg.setdefault((name, date), {"fk": None, "lk": None, "fd": None, "ld": None})
            if e["fk"] is None and k is not None:
                e["fk"] = k
            if k is not None:
                e["lk"] = k
            if e["fd"] is None and d is not None:
                e["fd"] = d
            if d is not None:
                e["ld"] = d

    all_dates = sorted({date for (_, date) in agg})
    by_legend: dict[str, dict[str, dict]] = {}
    for (name, date), v in agg.items():
        dk = (v["lk"] or 0) - (v["fk"] or 0) if v["fk"] is not None else (v["lk"] or 0)
        dd = (v["ld"] or 0) - (v["fd"] or 0) if v["fd"] is not None else (v["ld"] or 0)
        by_legend.setdefault(name, {})[date] = {"kills": max(dk, 0), "damage": max(dd, 0)}

    result = []
    for name, daymap in by_legend.items():
        days_list = [
            DayStat(date=d, kills=daymap.get(d, {}).get("kills", 0), damage=daymap.get(d, {}).get("damage", 0))
            for d in all_dates
        ]
        result.append(LegendDaily(legend=name, days=days_list))
    result = [ld for ld in result if any(d.kills or d.damage for d in ld.days)]  # skip unplayed
    result.sort(key=lambda x: sum(d.damage for d in x.days), reverse=True)
    return result


@router.get("/players/{player_id}/breakdown", response_model=BreakdownOut)
async def get_breakdown(
    player_id: int,
    sf: async_sessionmaker = Depends(get_session_factory),
) -> BreakdownOut:
    """Per-legend tracker percentiles + weapon/class kill breakdown, from the
    latest snapshot's raw_json (no live upstream call)."""
    async with sf() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFoundError(f"player id={player_id} not found")
        snap = (
            await session.execute(
                select(Snapshot)
                .where(Snapshot.player_id == player_id)
                .order_by(Snapshot.captured_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        recent_raw = (
            await session.execute(
                select(Snapshot.raw_json)
                .where(Snapshot.player_id == player_id)
                .order_by(Snapshot.captured_at.desc())
                .limit(20)
            )
        ).scalars().all()
    if snap is None:
        return BreakdownOut(player_id=player_id, trackers=[], weapons=[])

    data = json.loads(snap.raw_json)
    all_legends = ((data.get("legends") or {}).get("all")) or {}

    trackers: list[TrackerPercentile] = []
    for legend_name, info in all_legends.items():
        if legend_name == "Global":
            continue  # 'Global' is apexlegendsapi.com's account-aggregate bucket
                       # (holds weapon mastery), NOT a legend — don't list it as one.
        tracker_list = (info.get("data") or []) if isinstance(info, dict) else []
        for t in tracker_list:
            if not isinstance(t, dict):
                continue
            rps = t.get("rankPlatformSpecific") or {}
            rg = t.get("rank") or {}
            if not rps and not rg:
                continue
            trackers.append(TrackerPercentile(
                legend=legend_name,
                name=t.get("name") or t.get("key") or "",
                key=t.get("key") or "",
                value=_as_int_local(t.get("value")) or 0,
                platform_top_pct=_as_float(rps.get("topPercent")),
                global_top_pct=_as_float(rg.get("topPercent")),
                platform_rank=_as_int_local(rps.get("rankPos")),
            ))
    trackers.sort(key=lambda x: x.platform_top_pct if x.platform_top_pct is not None else 9e9)

    # per-legend rollup: headline kills/damage (specialEvent = real values) +
    # best platform percentile. Sorted by kills desc → legends[0] is the main.
    rollup: dict[str, dict] = {}
    for t in trackers:
        r = rollup.setdefault(
            t.legend,
            {"legend": t.legend, "kills": None, "damage": None, "best_platform_pct": None},
        )
        if t.key == "specialEvent_kills":
            r["kills"] = t.value
        elif t.key == "specialEvent_damage":
            r["damage"] = t.value
        if t.platform_top_pct is not None and (
            r["best_platform_pct"] is None or t.platform_top_pct < r["best_platform_pct"]
        ):
            r["best_platform_pct"] = t.platform_top_pct
    # per-legend splash art (ImgAssets.banner preferred)
    legend_imgs: dict[str, str | None] = {}
    for legend_name, info in all_legends.items():
        ia = (info.get("ImgAssets") or {}) if isinstance(info, dict) else {}
        legend_imgs[legend_name] = ia.get("banner") or ia.get("icon")

    legends = [LegendRollup(**r, img=legend_imgs.get(r["legend"])) for r in rollup.values()]
    legends.sort(key=lambda x: (x.kills or 0), reverse=True)

    # Weapon mastery is flaky (which weapons are returned varies per fetch);
    # merge across recent snapshots keeping the max (monotonic) value per weapon.
    weapons = _merge_mastery(recent_raw, "_kills")
    weapon_damage = _merge_mastery(recent_raw, "_damage_done")

    return BreakdownOut(
        player_id=player_id, captured_at=snap.captured_at,
        trackers=trackers, weapons=weapons, legends=legends,
        weapon_damage=weapon_damage,
    )


@router.get("/img")
async def proxy_img(url: str = Query(...)) -> Response:
    """Proxy upstream images (rank badges, legend art) so the browser doesn't
    need direct access to api.mozambiquehe.re, which may be blocked on the
    client network. httpx honours HTTPS_PROXY, so this works from behind a proxy."""
    if "mozambiquehe.re" not in url:
        raise HTTPException(status_code=400, detail="url not allowed")
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            upstream = await http.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"img fetch failed: {exc}")
    if upstream.status_code != 200:
        raise HTTPException(status_code=502, detail=f"upstream {upstream.status_code}")
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "image/jpeg"),
    )
