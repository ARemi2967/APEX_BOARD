"""Manually trigger snapshots outside the scheduler.

    python scripts/backfill_snapshots.py                  # all tracked players
    python scripts/backfill_snapshots.py --uid 7656119... # one player by uid

Useful for backfilling after downtime, or for an immediate first snapshot
right after adding a player.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings  # noqa: E402
from backend.db.models import Player  # noqa: E402
from backend.db.session import make_engine, make_session_factory  # noqa: E402
from backend.integrations.apex_api import ApexClient  # noqa: E402
from backend.integrations.exceptions import ApexError  # noqa: E402
from backend.scheduler.jobs import capture_all_tracked  # noqa: E402
from backend.services.snapshot_service import capture_snapshot  # noqa: E402
from sqlalchemy import select  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.apex_api_key or settings.apex_api_key == "your_key_here":
        print("ERROR: APEX_API_KEY not set in .env", file=sys.stderr)
        return 2

    engine = make_engine(settings.db_path)
    session_factory = make_session_factory(engine)

    try:
        async with ApexClient(settings.apex_api_key) as client:
            if args.uid:
                async with session_factory() as session:
                    player = (
                        await session.execute(
                            select(Player).where(Player.uid == args.uid)
                        )
                    ).scalar_one_or_none()
                if player is None:
                    print(
                        f"no player with uid={args.uid} in DB "
                        "(add it via the API first)",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    snap = await capture_snapshot(client, session_factory, player)
                except ApexError as exc:
                    print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
                    return 1
                print(
                    f"captured snapshot id={snap.id} for uid={player.uid} "
                    f"(level={snap.level}, rank={snap.rank_name})"
                )
            else:
                count = await capture_all_tracked(client, session_factory)
                print(f"backfill cycle complete: {count} snapshot(s) captured")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill player snapshots.")
    parser.add_argument(
        "--uid", help="snapshot a single player by uid (default: all tracked)"
    )
    sys.exit(asyncio.run(main(parser.parse_args())))
