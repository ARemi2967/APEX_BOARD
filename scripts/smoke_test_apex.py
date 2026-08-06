"""Live smoke test for ApexClient against apexlegendsapi.com.

Makes exactly ONE real request to validate the integration end-to-end:
key validity, request shape, and the fields we plan to snapshot later.

Usage (run from project root):
    python scripts/smoke_test_apex.py <player_or_uid> <platform>

If the identifier is all digits, it is treated as a UID (recommended — name
search is unreliable for some accounts). Otherwise it is treated as a name.

    python scripts/smoke_test_apex.py 76561199081861359 PC    # by UID
    python scripts/smoke_test_apex.py MyName PC                # by name

Reads APEX_API_KEY from .env (falls back to the process environment).
Intentionally does NOT loop — one request, then exit, to respect the rate limit.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Make `backend` importable when running as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.integrations.apex_api import ApexClient  # noqa: E402
from backend.integrations.exceptions import ApexError  # noqa: E402


def load_env(path: Path) -> dict[str, str]:
    """Minimal .env parser — KEY=VALUE lines, ignores comments/blanks."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


async def main(identifier: str, platform: str) -> int:
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    api_key = env.get("APEX_API_KEY", "")
    if not api_key or api_key == "your_key_here":
        print("ERROR: APEX_API_KEY not set in .env", file=sys.stderr)
        return 2

    # Numeric identifier → UID lookup (bypasses the flaky name-search fallback).
    use_uid = identifier.isdigit()
    mode = "uid" if use_uid else "name"
    print(f"Fetching /bridge by {mode}={identifier!r} platform={platform!r} ...")
    async with ApexClient(api_key) as client:
        try:
            if use_uid:
                data = await client.get_bridge_by_uid(identifier, platform)
            else:
                data = await client.get_bridge(identifier, platform)
        except ApexError as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    _print_summary(data)
    print("\nOK — live integration verified.")
    return 0


def _print_summary(data: dict) -> None:
    g = data.get("global", {}) or {}
    rank = g.get("rank", {}) or {}
    bp = g.get("battlepass", {}) or {}
    total = data.get("total", [])
    legends = data.get("legends", {}) or {}

    print("\n=== Bridge response summary ===")
    print(f"  name      : {g.get('name')!r}")
    print(f"  uid       : {g.get('uid')}")
    print(f"  level     : {g.get('level')}")
    print(
        f"  rank      : {rank.get('rankName')} div {rank.get('rankDiv')} "
        f"(score={rank.get('rankScore')})"
    )
    print(f"  bp level  : {bp.get('level')}")
    print("  totals    :")
    _print_totals(total)
    print(f"  legends   : {len(legends)} entries (sample: {list(legends.keys())[:5]})")
    print(f"  total[raw]: {json.dumps(total)[:400]}")


def _print_totals(total: Any) -> None:
    """Tolerant printer — `total` may be a list of dicts OR a dict of dicts."""
    if isinstance(total, dict):
        for key, val in total.items():
            if isinstance(val, dict):
                print(f"    {key}: {val.get('value', val)}")
            else:
                print(f"    {key}: {val}")
    elif isinstance(total, list):
        for entry in total:
            if isinstance(entry, dict):
                label = entry.get("name") or entry.get("key")
                print(f"    {label}: {entry.get('value')}")
            else:
                print(f"    {entry}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python scripts/smoke_test_apex.py <player_or_uid> <platform>",
              file=sys.stderr)
        print("  platform must be one of: PC, PS4, X1", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
