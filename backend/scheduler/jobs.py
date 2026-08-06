"""APscheduler setup: periodically capture snapshots for all tracked players.

The scheduler runs a single job (`snapshot_cycle`) every `interval_minutes`.
Each cycle fetches every `tracked=True` player; failures are isolated — one
player's bad upstream response does not stop the rest.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.db.models import Player
from backend.integrations.apex_api import ApexClient
from backend.integrations.exceptions import ApexError
from backend.services.snapshot_service import capture_snapshot

logger = logging.getLogger(__name__)


async def capture_all_tracked(
    client: ApexClient,
    session_factory: async_sessionmaker,
) -> int:
    """Capture a snapshot for every tracked player. Returns the count captured.

    A failure on one player is logged and skipped; the loop continues.
    """
    async with session_factory() as session:
        result = await session.execute(
            select(Player).where(Player.tracked.is_(True))
        )
        players = result.scalars().all()

    if not players:
        logger.info("no tracked players; skipping snapshot cycle")
        return 0

    captured = 0
    for player in players:
        try:
            await capture_snapshot(client, session_factory, player)
            captured += 1
        except ApexError as exc:
            logger.warning(
                "snapshot failed for uid=%s player_id=%s: %s",
                player.uid, player.id, exc,
            )
    logger.info("snapshot cycle done: %d/%d captured", captured, len(players))
    return captured


def build_scheduler(
    client: ApexClient,
    session_factory: async_sessionmaker,
    interval_minutes: int,
) -> AsyncIOScheduler:
    """Build (but do not start) the snapshot scheduler."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        capture_all_tracked,
        trigger=IntervalTrigger(minutes=interval_minutes),
        args=[client, session_factory],
        id="snapshot_cycle",
        replace_existing=True,
        max_instances=1,  # never overlap cycles
        coalesce=True,    # collapse missed runs into one
    )
    return scheduler
