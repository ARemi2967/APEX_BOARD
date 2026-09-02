"""ORM models: Player, Snapshot, EaImport, EaMatch.

Design notes
------------
- `Player.uid` is the unique identity (Apex/Steam UID), NOT username — Steam-
  linked accounts can have an empty indexed name, so name is a nullable
  display field. See the apexlegendsapi.com quirks memory.
- `Snapshot` stores the full `raw_json` alongside parsed fields, so we can
  re-parse later if upstream adds/changes metrics without losing history.
- All parsed snapshot fields are nullable: the upstream `total` tracker set is
  often partially unpopulated (e.g. `kd == -1`, lifetime kills missing).
- Deleting a player cascades to its snapshots (FK ON DELETE CASCADE), with
  `passive_deletes=True` so the DB does the work rather than SQLAlchemy
  loading every child row.
- `EaMatch` rows come from the EA account data export (≈1 year of match
  history) and act as ground truth for reconciling stale legend trackers.
  Each match keeps its own `raw_json` for the same re-parse-later reason as
  snapshots. Re-importing overlapping exports is idempotent via the
  `(player_id, match_key)` unique constraint.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform: Mapped[str] = mapped_column(String(8), nullable=False)
    tracked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="player",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    # Parsed fields — all nullable (see module docstring).
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rank_div: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ranked_season: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bp_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    damage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matches_played: Mapped[int | None] = mapped_column(Integer, nullable=True)

    player: Mapped["Player"] = relationship(back_populates="snapshots")

    __table_args__ = (
        Index("ix_snapshots_player_captured", "player_id", "captured_at"),
    )


class EaImport(Base):
    """One ingested EA data export file (zip of JSON, ≈1 year of data).

    `file_hash` makes re-uploading the same file a no-op instead of a double
    import; overlapping *different* files are deduplicated at the match level
    (see EaMatch).
    """

    __tablename__ = "ea_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # Coverage window of the matches actually extracted (naive UTC).
    data_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    match_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Per-legend lifetime stats extracted (the actual reconciliation source —
    # real exports carry stat tables, not match lists).
    legend_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    player: Mapped["Player"] = relationship()


class EaLegendStat(Base):
    """Official per-legend lifetime stats from an EA export's gameDataTable
    (`stats.characters[<char>].kills/damage_done/games_played`).

    These are the same career counters the in-game trackers read, so the
    reconciliation with tracker values is a direct comparison — no window
    arithmetic. Rows belong to one import; calibration reads the latest
    import's rows. `legend` is the anonymized EA key verbatim for rows we
    can't map (e.g. "unknown") — `mapped_name` is the bridge-API legend name
    or NULL.
    """

    __tablename__ = "ea_legend_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("ea_imports.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    legend: Mapped[str] = mapped_column(String(64), nullable=False)
    kills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    damage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    games_played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Export generation date (top-level `date` field, or last session time).
    as_of: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("import_id", "legend", name="uq_ea_legend_stats_import_legend"),
    )


class EaMatch(Base):
    """A single match from the EA data export, normalized for reconciliation.

    `match_key` prefers the export's own session/match id; without one it
    degrades to a composite of start time + mode + legend so overlapping
    imports converge on the same rows. `kills`/`damage` are nullable — the
    export may omit stats for some entries.
    """

    __tablename__ = "ea_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    match_key: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    legend: Mapped[str] = mapped_column(String(32), nullable=False)
    kills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    damage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    player: Mapped["Player"] = relationship()

    __table_args__ = (
        UniqueConstraint("player_id", "match_key", name="uq_ea_matches_player_key"),
        Index("ix_ea_matches_player_time", "player_id", "started_at"),
    )
