"""Create the SQLite database file and all tables. Run from project root.

    python scripts/init_db.py

Reads DB_PATH from .env (default data/apex.db), creates the data dir if
missing, runs Base.metadata.create_all, then prints the resulting schema /
PRAGMA state so the acceptance criteria are visibly confirmed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

# Make `backend` importable when run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings  # noqa: E402
from backend.db.base import Base  # noqa: E402
from backend.db.models import Player, Snapshot  # noqa: E402,F401 — register models
from backend.db.session import make_engine  # noqa: E402


async def main() -> int:
    settings = get_settings()
    db_path = settings.db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"Creating database at {db_path} ...")
    engine = make_engine(db_path)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with engine.connect() as conn:
            journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
            tables = (
                await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "ORDER BY name"
                    )
                )
            ).scalars().all()
            indexes = (
                await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='index' "
                        "ORDER BY name"
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    print(f"  journal_mode : {journal_mode}")
    print(f"  foreign_keys : {foreign_keys}")
    print(f"  tables       : {tables}")
    print(f"  indexes      : {indexes}")
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
