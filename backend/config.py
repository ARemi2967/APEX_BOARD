"""Application configuration loaded from .env / process environment.

Plain dataclass + manual .env parse — keeps the storage layer free of extra
dependencies. Field names match what a pydantic-settings class would use, so
Phase 4 can swap the loader without touching call sites.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    apex_api_key: str = ""
    db_path: str = "data/apex.db"
    snapshot_interval_min: int = 10
    platform_default: str = "PC"
    display_name: str = ""
    # Shared secret for /api/admin/* (EA export upload etc.). Empty disables
    # the whole admin surface (routes 404), so a public deployment without a
    # token accepts no uploads at all.
    admin_token: str = ""


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file (real env vars take precedence)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def get_settings() -> Settings:
    _load_dotenv(_PROJECT_ROOT / ".env")
    return Settings(
        apex_api_key=os.environ.get("APEX_API_KEY", ""),
        db_path=os.environ.get("DB_PATH", "data/apex.db"),
        snapshot_interval_min=int(os.environ.get("SNAPSHOT_INTERVAL_MIN", "10")),
        platform_default=os.environ.get("PLATFORM_DEFAULT", "PC"),
        display_name=os.environ.get("DISPLAY_NAME", ""),
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
    )
