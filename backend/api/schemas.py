"""Pydantic request/response schemas for the API layer (pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# Re-declared here so the API surface owns its own contract (not the client's).
Platform = Literal["PC", "PS4", "X1"]


class PlayerCreate(BaseModel):
    uid: str
    platform: Platform
    username: str | None = None
    display_name: str | None = None


class PlayerUpdate(BaseModel):
    display_name: str | None = None
    username: str | None = None
    tracked: bool | None = None


class PlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uid: str
    username: str | None
    display_name: str | None = None
    platform: str
    tracked: bool
    created_at: datetime
    updated_at: datetime


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    player_id: int
    captured_at: datetime
    level: int | None
    rank_name: str | None
    rank_div: int | None
    rank_score: int | None
    bp_level: int | None
    kills: int | None
    damage: int | None
    wins: int | None
    matches_played: int | None


class CurrentStats(BaseModel):
    """Live (cached 30s) view of a player's current aggregate stats."""

    uid: str
    name: str | None
    level: int | None
    rank_name: str | None
    rank_div: int | None
    rank_score: int | None
    rank_img: str | None
    bp_level: int | None
    kills: int | None
    damage: int | None
    wins: int | None
    # realtime presence (live)
    is_online: bool = False
    is_in_game: bool = False
    selected_legend: str | None = None
    state_text: str | None = None
    fetched_at: datetime


class HistoryPoint(BaseModel):
    captured_at: datetime
    value: float | None
    ranked_season: str | None = None


class HistoryOut(BaseModel):
    player_id: int
    metric: str
    days: int
    points: list[HistoryPoint]


class LegendsOut(BaseModel):
    player_id: int
    selected: str | None
    legends: dict[str, Any]
    # EA-export calibration overlay, keyed by legend name. Absent/empty when
    # no EA data has been imported (or nothing needs fixing).
    calibration: dict[str, dict[str, int]] | None = None


class DeltasOut(BaseModel):
    """Per-metric change over a window, for the Changes UI. `yesterday` is a
    fallback shown when today has no activity yet."""

    player_id: int
    days: int
    first_at: datetime | None = None
    last_at: datetime | None = None
    deltas: dict[str, int | None]
    yesterday: dict[str, int | None] | None = None


class TrackerPercentile(BaseModel):
    """A per-legend tracker with its leaderboard percentile (the free-tier
    'hidden leaderboard' — no match-history whitelist needed)."""

    legend: str
    name: str
    key: str = ""
    value: int
    platform_top_pct: float | None = None
    global_top_pct: float | None = None
    platform_rank: int | None = None


class WeaponStat(BaseModel):
    name: str
    value: int
    # EA-export extras (weapon calibration); absent on tracker-only weapons.
    headshots: int | None = None
    shots: int | None = None
    hits: int | None = None
    is_calibrated: bool = False  # value differs from the raw tracker reading


class LegendActivity(BaseModel):
    """Per-legend kills/damage change over a window (e.g. today), from
    diffing the earliest vs latest snapshot's raw_json."""

    legend: str
    kills: int = 0
    damage: int = 0


class DayStat(BaseModel):
    date: str
    kills: int = 0
    damage: int = 0


class LegendDaily(BaseModel):
    """Per-legend daily kills/damage over N days (the records page)."""

    legend: str
    days: list[DayStat]


class LegendRollup(BaseModel):
    """Per-legend headline stats — drives the 'featured/main legend' panel."""

    legend: str
    kills: int | None = None
    damage: int | None = None
    best_platform_pct: float | None = None
    img: str | None = None


class BreakdownOut(BaseModel):
    """Per-legend tracker percentiles + weapon/class kill breakdown, from the
    latest snapshot's raw_json (no live call)."""

    player_id: int
    captured_at: datetime | None = None
    trackers: list[TrackerPercentile]
    weapons: list[WeaponStat]
    legends: list[LegendRollup] = []
    weapon_damage: list[WeaponStat] = []


# --- EA data export ingestion & calibration ---

class EaImportOut(BaseModel):
    """One ingested EA data export file."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    player_id: int
    file_name: str
    file_hash: str
    data_start: datetime | None = None
    data_end: datetime | None = None
    match_count: int
    legend_count: int = 0
    weapon_count: int = 0
    ingested_at: datetime


class EaImportResult(BaseModel):
    """Summary returned right after an upload (new or duplicate)."""

    duplicate: bool
    import_id: int
    file_name: str
    match_count: int
    legend_count: int = 0
    weapon_count: int = 0
    data_start: datetime | None = None
    data_end: datetime | None = None


class LegendCalibrationOut(BaseModel):
    """Per-legend reconciliation row: EA official career counters vs the
    site's tracker readings."""

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
    is_calibrated: bool = False


class WeaponCalibrationOut(BaseModel):
    """Per-weapon reconciliation row: EA official career counters vs the
    site's flaky mastery trackers."""

    weapon: str
    short_id: str
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
    is_calibrated: bool = False


class CalibrationOut(BaseModel):
    player_id: int
    ea_as_of: datetime | None = None
    total_missing_kills: int = 0
    total_missing_damage: int = 0
    unattributed_kills: int = 0
    unattributed_damage: int = 0
    legends: list[LegendCalibrationOut] = []
    weapons: list[WeaponCalibrationOut] = []
