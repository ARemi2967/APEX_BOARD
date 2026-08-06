"""ORM models: Player and Snapshot.

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
