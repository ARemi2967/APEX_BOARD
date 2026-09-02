"""Inspect an EA data export to pin down its actual structure.

The export format is undocumented; the parser in
backend/services/ea_export_service.py uses tolerant candidate-key lookup. When
a new export doesn't parse (or EA changes field names), run:

    python scripts/inspect_ea_export.py path/to/EA_export.zip

It prints: the zip layout, what the parser extracts (per-legend lifetime
stats, match entries, coverage window), and — when nothing parses — the raw
JSON structure so the candidate tuples in ea_export_service.py can be
extended.

Exit codes: 0 ok, 1 bad file / nothing parsed.
"""

from __future__ import annotations

import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.ea_export_service import (  # noqa: E402
    _iter_game_data_tables,
    iter_match_entries,
    parse_export_zip,
    parse_match_entry,
    parse_legend_stats_rows,
)


def _peek(obj, limit: int = 600) -> str:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    path = Path(argv[1])
    if not path.exists():
        print(f"file not found: {path}")
        return 1

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        print("not a zip file")
        return 1

    names = [n for n in zf.namelist() if not n.endswith("/")]
    print(f"== zip: {len(names)} files ==")
    groups = Counter("/".join(n.split("/")[:2]) for n in names)
    for group, count in groups.most_common(30):
        print(f"  {group:60s} {count} file(s)")

    # --- what the production parser extracts ---
    try:
        payload = parse_export_zip(path.read_bytes())
    except Exception as exc:  # noqa: BLE001 — the inspect tool reports, not raises
        print(f"\nparser raised: {exc!r}")
        payload = None

    if payload is not None:
        print(f"\n== parser result ==")
        print(f"  legend stats: {len(payload.legend_stats)}")
        for s in payload.legend_stats:
            print(f"    {s.raw_key:24s} -> {s.bridge_name!s:16s} "
                  f"k={s.kills!s:>8s} d={s.damage!s:>9s} games={s.games_played!s}")
        print(f"  match entries: {len(payload.matches)}")
        print(f"  as_of: {payload.as_of}   sessions: {payload.session_start} → {payload.session_end}")
        print(f"  files used: {payload.used_files}")

    # --- raw structure dump (for extending the parser when formats drift) ---
    if payload is None or (not payload.legend_stats and not payload.matches):
        print("\n== raw structure (nothing extracted — use this to extend the parser) ==")
        shown = False
        for name in names:
            if not name.lower().endswith(".json"):
                continue
            try:
                doc = json.loads(zf.read(name).decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                print(f"\n-- {name}: <binary or invalid json, skipped>")
                continue
            tables = list(_iter_game_data_tables(doc))
            print(f"\n-- {name}: type={type(doc).__name__}, "
                  f"gameDataTables={len(tables)}, top-level keys={list(doc)[:12] if isinstance(doc, dict) else '-'}")
            if not shown:
                print(_peek(doc))
                if tables:
                    print("  gameDataTable sample rows:")
                    print("  " + _peek((tables[0].get("gameDataTable") or [])[:5]).replace("\n", "\n  "))
                    print("  parsed legend stats from this table:",
                          {k: (v.kills, v.damage) for k, v in
                           parse_legend_stats_rows(tables[0].get("gameDataTable")).items()})
                shown = True

    # match-entry analysis (only meaningful for match-history style files)
    total = parsed_total = 0
    key_union: Counter = Counter()
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        try:
            doc = json.loads(zf.read(name).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        entries = list(iter_match_entries(doc))
        total += len(entries)
        parsed_total += sum(1 for p in (parse_match_entry(e) for e in entries) if p)
        for e in entries:
            key_union.update(e.keys())
    if total:
        print(f"\n== match entries: {total} found, {parsed_total} parsed ==")
        print(f"  key union: {sorted(key_union)}")

    if payload is None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
