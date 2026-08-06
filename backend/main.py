"""FastAPI application factory + module-level `app` for uvicorn.

    uvicorn backend.main:app --reload

`create_app` is also the test entry point — pass a temp `settings` and a fake
`apex_client` to avoid hitting the network.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from backend.api.errors import apex_error_handler
from backend.api.routes import TTLCache, router
from backend.config import Settings, get_settings
from backend.db import models  # noqa: F401 — register tables on Base.metadata
from backend.db.base import Base
from backend.db.session import make_engine, make_session_factory
from backend.integrations.apex_api import ApexClient
from backend.integrations.exceptions import ApexError
from backend.scheduler.jobs import build_scheduler

logger = logging.getLogger(__name__)


def create_app(
    *,
    start_scheduler: bool = True,
    apex_client: Optional[ApexClient] = None,
    settings: Optional[Settings] = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.db_path)
    session_factory = make_session_factory(engine)

    # Reuse an injected client (tests), else build one if a key is configured.
    own_client = apex_client is None
    if apex_client is not None:
        client = apex_client
    elif settings.apex_api_key:
        client = ApexClient(settings.apex_api_key)
    else:
        client = None  # /current will return 503 until a key is set

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Ensure the data dir + tables exist (idempotent) so `docker compose up`
        # works without a separate init step.
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Lightweight migrations: create_all won't ALTER existing tables,
            # so add columns introduced after the first deploy here.
            cols = [row[1] for row in (await conn.execute(text("PRAGMA table_info(players)"))).all()]
            if "display_name" not in cols:
                await conn.execute(text("ALTER TABLE players ADD COLUMN display_name VARCHAR(64)"))
            snap_cols = [row[1] for row in (await conn.execute(text("PRAGMA table_info(snapshots)"))).all()]
            if "ranked_season" not in snap_cols:
                await conn.execute(text("ALTER TABLE snapshots ADD COLUMN ranked_season VARCHAR(32)"))

        scheduler = None
        if start_scheduler and settings.apex_api_key:
            scheduler = build_scheduler(
                client, session_factory, settings.snapshot_interval_min
            )
            scheduler.start()
            app.state.scheduler = scheduler
            logger.info("scheduler started (interval=%d min)", settings.snapshot_interval_min)
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            if own_client and client is not None:
                await client.close()
            await engine.dispose()

    app = FastAPI(title="APEX_bord", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.apex_client = client
    app.state.current_cache = TTLCache(ttl_seconds=30)
    app.state.display_name_override = settings.display_name or None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(ApexError, apex_error_handler)
    app.include_router(router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # Serve the static frontend at "/" (Phase 5 fills these files).
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app


app = create_app()
