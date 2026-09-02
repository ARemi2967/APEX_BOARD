"""EA account data export ingestion.

The "Download your EA data" export (EA account privacy settings) ships as a
zip of ``Request-<id>-ApexLegends.json`` files. Verified real-world structure
(2026-09):

    {
      "date": "31-Aug-2026",
      "userData": [{
        "product": "Apex Legends",
        "subscribers": [{
          "subscriber": "prod-respawn-entertainment",
          "userData": {
            "gameDataTable": [{"name": "stats.characters[character_octane].kills", "value": "5337"}, ...],
            "antiCheatSessions": [{"ip": ..., "time_start": ..., "time_end": ...}, ...],
          },
        }],
      }],
    }

There is NO per-match history. The valuable part is ``gameDataTable`` — a
flattened dump of the server-side profile containing **official per-legend
lifetime career stats** (``stats.characters[<char>].kills/damage_done/
games_played``), i.e. the exact counters the in-game trackers read. That makes
reconciliation a direct value comparison (see calibration_service). Some keys
are anonymized by EA (season ids, occasionally a character id shows up as
``unknown``) — unmappable characters are kept verbatim and flagged.

The parser stays tolerant (candidate paths/keys, values as strings) so a
format drift degrades to a clear error rather than silent miscounts; run
``scripts/inspect_ea_export.py`` on a new-format export to extend the
candidates. Per-match parsing is also kept: if EA ever ships real match
history, it lands in ``ea_matches``.

Importing is idempotent: the file hash is unique in ``ea_imports`` (same file
re-uploaded is a no-op), and matches upsert on ``(player_id, match_key)``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.db.models import EaImport, EaLegendStat, EaMatch, EaWeaponStat

logger = logging.getLogger(__name__)


class EaExportError(Exception):
    """The uploaded file is not a parseable EA data export."""


# Bump when the parser learns to extract new things from the same bytes —
# re-uploads of already-imported files then re-extract instead of no-op'ing.
PARSER_VERSION = 2


# Canonical bridge-API legend names (must match the keys the snapshots' raw
# `legends.all` uses, so calibration can join the two sources).
_BRIDGE_LEGENDS = {
    "Bloodhound", "Gibraltar", "Lifeline", "Pathfinder", "Wraith",
    "Bangalore", "Caustic", "Mirage", "Octane", "Wattson", "Crypto",
    "Revenant", "Loba", "Rampart", "Horizon", "Fuse", "Valkyrie", "Seer",
    "Ash", "Mad Maggie", "Newcastle", "Vantage", "Catalyst", "Ballistic",
    "Conduit", "Alter", "Sparrow", "Axle",
}

# Export-internal ids / spellings -> bridge display name.
_LEGEND_ALIASES: dict[str, str] = {
    **{re.sub(r"[^a-z0-9]", "", name.lower()): name for name in _BRIDGE_LEGENDS},
    "madmaggie": "Mad Maggie",
    "maggy": "Mad Maggie",
    "blisk": "Ash",       # internal codename seen in some EA payloads
    "immortal": "Revenant",  # internal codename for Revenant
}


def normalize_legend_name(raw: Any) -> str | None:
    """Map an export legend identifier to the bridge display name."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    hit = _LEGEND_ALIASES.get(key)
    if hit is not None:
        return hit
    return raw.strip()[:32]  # unknown legend: keep verbatim (frontend falls back to it)


def ea_character_to_bridge(raw: str) -> str | None:
    """``character_octane`` → ``Octane``; anonymized ids (``unknown``) → None."""
    if not raw:
        return None
    key = raw.strip().removeprefix("character_")
    if not key or key == "unknown":
        return None
    return normalize_legend_name(key)


# Candidate keys for match-history-style entries (kept for future exports).
_MATCH_ID_KEYS = ("sessionId", "session_id", "matchId", "match_id", "id", "gameId", "uid")
_TIME_KEYS = ("completionDate", "completion_date", "completedAt", "timestamp", "date",
              "startedAt", "started_at", "startTime", "start_time", "endTime",
              "playedAt", "matchDate", "datetime")
_LEGEND_KEYS = ("character", "legend", "legendName", "legend_name", "characterName")
_MODE_KEYS = ("playlist", "gameMode", "game_mode", "mode", "queue", "gameType", "category")
_KILLS_KEYS = ("kills", "killCount", "kill_count", "numKills", "killsCount")
_DAMAGE_KEYS = ("damage", "damageDealt", "damage_dealt", "damageDone", "damage_done",
                "totalDamage", "damageDoneTotal")
_NESTED_STATS_KEYS = ("stats", "matchStats", "performance", "values", "attributes")

# gameDataTable flat-key patterns (the real export format).
_LEGEND_STAT_RE = re.compile(
    r"^stats\.characters\[([^\]]+)\]\.(kills|damage_done|games_played)$"
)
_WEAPON_STAT_RE = re.compile(
    r"^stats\.weapons\[([^\]]+)\]\.(kills|damage_done|headshots|shots|hits)$"
)

# EA weapon ids -> (bridge tracker short id, display name). Short ids match the
# mastery_<id>_kills tracker keys (verified against live snapshot data);
# display names align with the frontend's WEAPON_CN translation keys.
# Cross-validated quirk: mp_weapon_shotgun_pistol is the MOZAMBIQUE (the
# "shotgun pistol"), NOT the Mastiff — mp_weapon_mastiff is.
WEAPON_ID_MAP: dict[str, tuple[str, str]] = {
    "mp_weapon_alternator_smg": ("alternator", "Alternator"),
    "mp_weapon_r97": ("r99", "R-99"),
    "mp_weapon_rspn101": ("r301", "R-301"),
    "mp_weapon_semipistol": ("p2020", "P2020"),
    "mp_weapon_autopistol": ("re45", "RE-45"),
    "mp_weapon_nemesis": ("nemesis", "Nemesis"),
    "mp_weapon_hemlok": ("hemlok", "Hemlok"),
    "mp_weapon_vinson": ("flatline", "Flatline"),
    "mp_weapon_car": ("car", "C.A.R"),
    "mp_weapon_volt_smg": ("volt", "Volt"),
    "mp_weapon_pdw": ("prowler", "Prowler"),
    "mp_weapon_lmg": ("spitfire", "Spitfire"),
    "mp_weapon_dragon_lmg": ("rampage", "Rampage"),
    "mp_weapon_energy_ar": ("havoc", "Havoc"),
    "mp_weapon_esaw": ("devotion", "Devotion"),
    "mp_weapon_lstar": ("lstar", "L-STAR"),
    "mp_weapon_shotgun": ("eva8", "EVA-8"),
    "mp_weapon_shotgun_pistol": ("mozambique", "Mozambique"),
    "mp_weapon_mastiff": ("mastiff", "Mastiff"),
    "mp_weapon_energy_shotgun": ("peacekeeper", "Peacekeeper"),
    "mp_weapon_dragon_sniper": ("kraber", "Kraber"),
    "mp_weapon_sniper": ("kraber", "Kraber"),
    "mp_weapon_dmr": ("longbow", "Longbow"),
    "mp_weapon_g2": ("g7", "G7"),
    "mp_weapon_defender": ("chargerifle", "Charge Rifle"),
    "mp_weapon_doubletake": ("tripletake", "Triple Take"),
    "mp_weapon_3030": ("3030", "30-30"),
    "mp_weapon_sentinel": ("sentinel", "Sentinel"),
    "mp_weapon_wingman": ("wingman", "Wingman"),
    "mp_weapon_bow": ("bocek", "Bocek"),
}


def ea_weapon_display(raw_key: str) -> tuple[str, str] | None:
    """EA weapon id -> (tracker short id, display name); unmapped → None."""
    entry = WEAPON_ID_MAP.get((raw_key or "").strip())
    return entry if entry else None


def _pick(entry: dict, keys: tuple[str, ...]) -> Any:
    """First present, non-null value among candidate keys (flat, then nested)."""
    for key in keys:
        value = entry.get(key)
        if value is not None:
            return value
    for container_key in _NESTED_STATS_KEYS:
        container = entry.get(container_key)
        if isinstance(container, dict):
            for key in keys:
                value = container.get(key)
                if value is not None:
                    return value
    return None


def parse_timestamp(value: Any) -> datetime | None:
    """Coerce an export timestamp to naive UTC. Handles ISO strings and epoch
    seconds/millis (as int, float or numeric string)."""
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"\d{10}(\.\d+)?", stripped):
            value = float(stripped)
        elif re.fullmatch(r"\d{13}(\.\d+)?", stripped):
            value = float(stripped) / 1000.0
        else:
            text = stripped.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:  # epoch millis
            seconds /= 1000.0
        try:
            return datetime.utcfromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


# --- zip traversal -----------------------------------------------------------

def _is_apex_json(name: str) -> bool:
    lowered = name.lower()
    return "apex" in lowered and lowered.endswith(".json")


def _iter_game_data_tables(payload: Any) -> Iterator[dict]:
    """Walk the userData → subscribers tree, yielding every subscriber's
    userData dict that carries a gameDataTable."""
    if not isinstance(payload, dict):
        return
    for product in payload.get("userData") or []:
        if not isinstance(product, dict):
            continue
        for subscriber in product.get("subscribers") or []:
            if not isinstance(subscriber, dict):
                continue
            user_data = subscriber.get("userData")
            if isinstance(user_data, dict) and "gameDataTable" in user_data:
                yield user_data


def _parse_export_date(payload: Any) -> datetime | None:
    """Top-level `date` field, e.g. '31-Aug-2026'."""
    if isinstance(payload, dict):
        raw = payload.get("date")
        if isinstance(raw, str):
            try:
                return datetime.strptime(raw.strip(), "%d-%b-%Y")
            except ValueError:
                return parse_timestamp(raw)
    return None


def iter_match_entries(payload: Any) -> Iterator[dict]:
    """Yield match dicts from whatever container shape a file uses:
    a bare list, a dict with a known list key, or a dict keyed by match id."""
    if isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, dict))
        return
    if not isinstance(payload, dict):
        return
    for key in ("matches", "matchHistory", "match_history", "histories", "history", "data", "results"):
        inner = payload.get(key)
        if isinstance(inner, list):
            yield from (item for item in inner if isinstance(item, dict))
            return
    # dict keyed by match id -> values are the match dicts
    values = list(payload.values())
    if values and all(isinstance(v, dict) for v in values):
        yield from values


@dataclass
class ParsedMatch:
    match_key: str
    started_at: datetime
    legend: str
    kills: int | None
    damage: int | None
    mode: str | None
    raw: dict


@dataclass
class LegendStats:
    """Official per-legend lifetime career stats from one export."""

    raw_key: str                      # EA's key verbatim, e.g. "character_octane"
    kills: int | None = None
    damage: int | None = None
    games_played: int | None = None

    @property
    def bridge_name(self) -> str | None:
        return ea_character_to_bridge(self.raw_key)


@dataclass
class WeaponStats:
    """Official per-weapon lifetime career stats from one export."""

    raw_key: str                      # EA id verbatim, e.g. "mp_weapon_r97"
    kills: int | None = None
    damage: int | None = None
    headshots: int | None = None
    shots: int | None = None
    hits: int | None = None

    @property
    def display(self) -> tuple[str, str] | None:
        return ea_weapon_display(self.raw_key)


@dataclass
class ExportPayload:
    matches: list[ParsedMatch] = field(default_factory=list)
    legend_stats: list[LegendStats] = field(default_factory=list)
    weapon_stats: list[WeaponStats] = field(default_factory=list)
    as_of: datetime | None = None          # export generation date
    session_start: datetime | None = None  # earliest anti-cheat session (data coverage)
    session_end: datetime | None = None
    used_files: list[str] = field(default_factory=list)


def parse_match_entry(entry: dict) -> ParsedMatch | None:
    started_at = parse_timestamp(_pick(entry, _TIME_KEYS))
    legend = normalize_legend_name(_pick(entry, _LEGEND_KEYS))
    if started_at is None or legend is None:
        return None
    kills = _as_int(_pick(entry, _KILLS_KEYS))
    damage = _as_int(_pick(entry, _DAMAGE_KEYS))
    mode = _pick(entry, _MODE_KEYS)
    mode_str = str(mode)[:32] if mode is not None else None

    explicit_id = _pick(entry, _MATCH_ID_KEYS)
    if explicit_id is not None and str(explicit_id).strip():
        match_key = str(explicit_id).strip()[:128]
    else:
        epoch = int(started_at.replace(tzinfo=timezone.utc).timestamp())
        match_key = f"{epoch}|{mode_str or '?'}|{legend}"[:128]

    return ParsedMatch(
        match_key=match_key, started_at=started_at, legend=legend,
        kills=kills, damage=damage, mode=mode_str, raw=entry,
    )


def parse_legend_stats_rows(table_rows: list) -> dict[str, LegendStats]:
    """Extract per-legend lifetime stats from a gameDataTable's {name, value}
    rows. Returns raw-key → LegendStats."""
    out: dict[str, LegendStats] = {}
    for row in table_rows or []:
        if not isinstance(row, dict):
            continue
        m = _LEGEND_STAT_RE.match(str(row.get("name") or ""))
        if m is None:
            continue
        char_key, stat = m.group(1), m.group(2)
        value = _as_int(row.get("value"))
        if value is None:
            continue
        entry = out.setdefault(char_key, LegendStats(raw_key=char_key))
        setattr(entry, {"kills": "kills", "damage_done": "damage",
                        "games_played": "games_played"}[stat], value)
    return out


def parse_weapon_stats_rows(table_rows: list) -> dict[str, WeaponStats]:
    """Extract per-weapon lifetime stats from a gameDataTable's {name, value}
    rows. Returns EA-id → WeaponStats."""
    field_for = {"kills": "kills", "damage_done": "damage", "headshots": "headshots",
                 "shots": "shots", "hits": "hits"}
    out: dict[str, WeaponStats] = {}
    for row in table_rows or []:
        if not isinstance(row, dict):
            continue
        m = _WEAPON_STAT_RE.match(str(row.get("name") or ""))
        if m is None:
            continue
        weapon_key, stat = m.group(1), m.group(2)
        value = _as_int(row.get("value"))
        if value is None:
            continue
        entry = out.setdefault(weapon_key, WeaponStats(raw_key=weapon_key))
        setattr(entry, field_for[stat], value)
    return out


def parse_export_zip(data: bytes) -> ExportPayload:
    """Extract everything useful from the export zip.

    Raises EaExportError when the zip is unreadable or contains neither match
    entries nor per-legend stats.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise EaExportError("not a zip file — download the full export archive from EA") from exc

    names = [n for n in zf.namelist() if not n.endswith("/")]
    payload = ExportPayload()
    seen_legend_keys: set[str] = set()
    seen_weapon_keys: set[str] = set()

    for name in names:
        if not name.lower().endswith(".json"):
            continue
        try:
            doc = json.loads(zf.read(name).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue

        apex_tables = list(_iter_game_data_tables(doc))
        for user_data in apex_tables:
            table_rows = user_data.get("gameDataTable")
            for raw_key, stats in parse_legend_stats_rows(table_rows).items():
                if raw_key not in seen_legend_keys:
                    seen_legend_keys.add(raw_key)
                    payload.legend_stats.append(stats)
            for raw_key, stats in parse_weapon_stats_rows(table_rows).items():
                if raw_key not in seen_weapon_keys:
                    seen_weapon_keys.add(raw_key)
                    payload.weapon_stats.append(stats)
            sessions = user_data.get("antiCheatSessions") or []
            times = [
                t for t in (
                    parse_timestamp(s.get("time_start"))
                    for s in sessions if isinstance(s, dict)
                ) if t is not None
            ]
            ends = [
                t for t in (
                    parse_timestamp(s.get("time_end"))
                    for s in sessions if isinstance(s, dict)
                ) if t is not None
            ]
            if times:
                payload.session_start = min(payload.session_start or times[0], *times)
            if ends:
                payload.session_end = max(payload.session_end or ends[0], *ends)
            if payload.as_of is None:
                payload.as_of = _parse_export_date(doc)
            if name not in payload.used_files:
                payload.used_files.append(name)

        if apex_tables:
            continue  # stat-table file; not a match list

        # match-history style files (future-proofing; current exports have none)
        before = len(payload.matches)
        for entry in iter_match_entries(doc):
            parsed = parse_match_entry(entry)
            if parsed is not None:
                payload.matches.append(parsed)
        if len(payload.matches) > before and name not in payload.used_files:
            payload.used_files.append(name)

    if not payload.legend_stats and not payload.weapon_stats and not payload.matches:
        detail = f"zip contains {len(names)} files, json files: " + ", ".join(
            n for n in names if n.lower().endswith(".json")
        )[:200]
        raise EaExportError(
            f"no Apex legend stats or match entries found ({detail}) — run "
            "scripts/inspect_ea_export.py on this file and extend the candidates "
            "in ea_export_service.py"
        )

    if payload.as_of is None:
        payload.as_of = payload.session_end  # best available proxy
    payload.matches.sort(key=lambda m: m.started_at)
    return payload


# --- import ------------------------------------------------------------------

async def import_ea_export(
    session_factory: async_sessionmaker,
    player_id: int,
    file_name: str,
    data: bytes,
) -> dict[str, Any]:
    """Parse + persist one export. Idempotent on file hash.

    Returns a summary dict: duplicate flag, match/legend counts, coverage.
    """
    file_hash = hashlib.sha256(data).hexdigest()

    async with session_factory() as session:
        existing = (
            await session.execute(select(EaImport).where(EaImport.file_hash == file_hash))
        ).scalar_one_or_none()
    # Same bytes + same parser version → true duplicate. A parser upgrade
    # re-extracts the file as a fresh import (stat tables live per-import, so
    # nothing doubles up).
    if existing is not None and existing.parser_version == PARSER_VERSION:
        return {
            "duplicate": True,
            "import_id": existing.id,
            "file_name": existing.file_name,
            "match_count": existing.match_count,
            "legend_count": existing.legend_count,
            "weapon_count": existing.weapon_count,
            "data_start": existing.data_start,
            "data_end": existing.data_end,
        }

    payload = parse_export_zip(data)

    match_rows = [
        {
            "player_id": player_id,
            "match_key": m.match_key,
            "started_at": m.started_at,
            "legend": m.legend,
            "kills": m.kills,
            "damage": m.damage,
            "mode": m.mode,
            "raw_json": json.dumps(m.raw, ensure_ascii=False),
        }
        for m in payload.matches
    ]

    async with session_factory() as session:
        for i in range(0, len(match_rows), 200):  # chunked upsert
            stmt = sqlite_insert(EaMatch).values(match_rows[i : i + 200])
            stmt = stmt.on_conflict_do_update(
                index_elements=["player_id", "match_key"],
                set_={
                    "started_at": stmt.excluded.started_at,
                    "legend": stmt.excluded.legend,
                    "kills": stmt.excluded.kills,
                    "damage": stmt.excluded.damage,
                    "mode": stmt.excluded.mode,
                    "raw_json": stmt.excluded.raw_json,
                },
            )
            await session.execute(stmt)

        if existing is not None:
            # Same file re-imported under a NEW parser version: refresh the
            # existing row in place (file_hash is UNIQUE — one row per file).
            imp = await session.get(EaImport, existing.id)
            assert imp is not None
            await session.execute(
                delete(EaLegendStat).where(EaLegendStat.import_id == imp.id)
            )
            await session.execute(
                delete(EaWeaponStat).where(EaWeaponStat.import_id == imp.id)
            )
            imp.file_name = file_name[:255]
            imp.data_start = payload.session_start
            imp.data_end = payload.session_end
            imp.match_count = len(match_rows)
            imp.legend_count = len(payload.legend_stats)
            imp.weapon_count = len(payload.weapon_stats)
            imp.parser_version = PARSER_VERSION
        else:
            imp = EaImport(
                player_id=player_id,
                file_name=file_name[:255],
                file_hash=file_hash,
                data_start=payload.session_start,
                data_end=payload.session_end,
                match_count=len(match_rows),
                legend_count=len(payload.legend_stats),
                weapon_count=len(payload.weapon_stats),
                parser_version=PARSER_VERSION,
            )
            session.add(imp)
        await session.flush()  # need imp.id for the stat rows

        if payload.legend_stats:
            session.add_all([
                EaLegendStat(
                    import_id=imp.id,
                    player_id=player_id,
                    legend=s.raw_key[:64],
                    kills=s.kills,
                    damage=s.damage,
                    games_played=s.games_played,
                    as_of=payload.as_of,
                )
                for s in payload.legend_stats
            ])
        if payload.weapon_stats:
            session.add_all([
                EaWeaponStat(
                    import_id=imp.id,
                    player_id=player_id,
                    weapon=s.raw_key[:64],
                    kills=s.kills,
                    damage=s.damage,
                    headshots=s.headshots,
                    shots=s.shots,
                    hits=s.hits,
                    as_of=payload.as_of,
                )
                for s in payload.weapon_stats
            ])
        await session.commit()
        await session.refresh(imp)

    logger.info(
        "EA export imported player_id=%s file=%s legends=%d weapons=%d matches=%d as_of=%s sessions=%s..%s",
        player_id, file_name, imp.legend_count, imp.weapon_count, imp.match_count,
        payload.as_of, payload.session_start, payload.session_end,
    )
    return {
        "duplicate": False,
        "import_id": imp.id,
        "file_name": imp.file_name,
        "match_count": imp.match_count,
        "legend_count": imp.legend_count,
        "weapon_count": imp.weapon_count,
        "data_start": imp.data_start,
        "data_end": imp.data_end,
    }


async def latest_import_id(session_factory: async_sessionmaker, player_id: int) -> int | None:
    async with session_factory() as session:
        return (
            await session.execute(
                select(EaImport.id).where(EaImport.player_id == player_id)
                .order_by(EaImport.id.desc()).limit(1)
            )
        ).scalar_one_or_none()
