"""Unit tests for ApexClient — no live network, all responses mocked.

The rate limiter is bypassed in most tests (min_interval=0.0) so the suite
runs fast. Two tests exercise the limiter explicitly to confirm spacing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from backend.integrations.apex_api import ApexClient, RateLimiter
from backend.integrations.exceptions import (
    ApexAuthError,
    ApexError,
    ApexRateLimitError,
    ApexServerError,
    PlayerNotFoundError,
)


def _make_client(handler: Any, **kwargs: Any) -> ApexClient:
    """Build an ApexClient backed by httpx.MockTransport.

    Tests pass a handler callable that receives the httpx.Request and
    returns an httpx.Response. Defaults make retries fast so the suite
    doesn't sleep for real seconds.
    """
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("rate_limiter", RateLimiter(min_interval=0.0))
    kwargs.setdefault("max_retries", 3)
    kwargs.setdefault("retry_min_wait", 0.001)
    kwargs.setdefault("retry_max_wait", 0.01)
    return ApexClient(api_key="test_key", transport=transport, **kwargs)


async def test_get_bridge_success_returns_parsed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"global": {"name": "TestPlayer"}})

    async with _make_client(handler) as client:
        result = await client.get_bridge("TestPlayer", "PC")

    assert result["global"]["name"] == "TestPlayer"


async def test_get_bridge_404_raises_player_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="player not found")

    async with _make_client(handler) as client:
        with pytest.raises(PlayerNotFoundError, match="Nobody"):
            await client.get_bridge("Nobody", "PC")


async def test_get_bridge_401_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    async with _make_client(handler) as client:
        with pytest.raises(ApexAuthError):
            await client.get_bridge("Player", "PC")


async def test_get_bridge_403_also_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async with _make_client(handler) as client:
        with pytest.raises(ApexAuthError):
            await client.get_bridge("Player", "PC")


async def test_get_bridge_429_retries_then_succeeds() -> None:
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"global": {"name": "OK"}})

    async with _make_client(handler) as client:
        result = await client.get_bridge("RetryPlayer", "PC")

    assert result["global"]["name"] == "OK"
    assert state["count"] == 3


async def test_get_bridge_5xx_retries_then_succeeds() -> None:
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"global": {"name": "OK"}})

    async with _make_client(handler) as client:
        result = await client.get_bridge("Player", "PC")

    assert state["count"] == 2
    assert result["global"]["name"] == "OK"


async def test_get_bridge_429_exhausts_retries_raises_rate_limit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async with _make_client(handler, max_retries=2) as client:
        with pytest.raises(ApexRateLimitError):
            await client.get_bridge("Spammy", "PC")


async def test_get_bridge_429_verification_body_raises_auth_error_without_retry() -> None:
    # apexlegendsapi.com returns 429 for unverified keys with a descriptive
    # Error body. This is terminal (retrying is pointless), NOT a rate limit.
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            429,
            json={"Error": "You must verify your API account first by linking "
                  "your Discord account on https://portal.apexlegendsapi.com/discord-auth"},
        )

    async with _make_client(handler, max_retries=3) as client:
        with pytest.raises(ApexAuthError, match="verification"):
            await client.get_bridge("Player", "PC")

    assert calls["count"] == 1  # no retry — raised immediately


async def test_get_bridge_429_non_verification_error_body_still_retries() -> None:
    # A 429 with an Error body that is NOT about verification must still retry
    # (don't over-classify every Error body as terminal).
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] < 2:
            return httpx.Response(429, json={"Error": "Too many requests"})
        return httpx.Response(200, json={"global": {"name": "OK"}})

    async with _make_client(handler) as client:
        result = await client.get_bridge("Player", "PC")

    assert state["count"] == 2
    assert result["global"]["name"] == "OK"


async def test_get_bridge_5xx_exhausts_retries_raises_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _make_client(handler, max_retries=2) as client:
        with pytest.raises(ApexServerError):
            await client.get_bridge("Player", "PC")


async def test_get_bridge_invalid_platform_rejected_before_request() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={})

    async with _make_client(handler) as client:
        with pytest.raises(ValueError, match="invalid platform"):
            await client.get_bridge("Player", "Android")

    assert calls["count"] == 0  # request never sent


async def test_get_bridge_empty_player_rejected() -> None:
    async with _make_client(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ValueError, match="player must not be empty"):
            await client.get_bridge("   ", "PC")


async def test_get_bridge_sends_auth_player_platform_params() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    async with _make_client(handler) as client:
        await client.get_bridge("MyPlayer", "X1")

    assert captured["path"] == "/bridge"
    assert captured["params"]["auth"] == "test_key"
    assert captured["params"]["player"] == "MyPlayer"
    assert captured["params"]["platform"] == "X1"


async def test_get_bridge_by_uid_success_returns_parsed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"global": {"uid": "12345"}})

    async with _make_client(handler) as client:
        result = await client.get_bridge_by_uid("12345", "PC")

    assert result["global"]["uid"] == "12345"


async def test_get_bridge_by_uid_sends_uid_param_not_player() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    async with _make_client(handler) as client:
        await client.get_bridge_by_uid("76561199081861359", "PC")

    assert captured["params"]["uid"] == "76561199081861359"
    assert "player" not in captured["params"]
    assert captured["params"]["platform"] == "PC"


async def test_get_bridge_by_uid_accepts_int_input() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    async with _make_client(handler) as client:
        await client.get_bridge_by_uid(76561199081861359, "PC")  # int, not str

    assert captured["params"]["uid"] == "76561199081861359"


async def test_get_bridge_by_uid_empty_rejected() -> None:
    async with _make_client(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(ValueError, match="uid must not be empty"):
            await client.get_bridge_by_uid("   ", "PC")


async def test_get_bridge_by_uid_404_raises_player_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with _make_client(handler) as client:
        with pytest.raises(PlayerNotFoundError, match="12345"):
            await client.get_bridge_by_uid("12345", "PC")


async def test_all_apex_errors_inherit_from_apex_error() -> None:
    # Sanity check: catch-any pattern works for callers.
    for exc_cls in (
        ApexAuthError,
        PlayerNotFoundError,
        ApexRateLimitError,
        ApexServerError,
    ):
        assert issubclass(exc_cls, ApexError)


async def test_rate_limiter_enforces_min_interval() -> None:
    limiter = RateLimiter(min_interval=0.1)
    start = time.monotonic()
    for _ in range(4):
        await limiter.acquire()
    elapsed = time.monotonic() - start

    # First acquire is instant; next 3 each wait ~min_interval.
    # Tolerance accounts for asyncio.sleep overshoot on Windows (~15ms granularity).
    assert elapsed >= 0.28


async def test_rate_limiter_concurrent_acquires_serialize() -> None:
    # Two acquires scheduled "simultaneously" — second must wait its turn.
    limiter = RateLimiter(min_interval=0.1)
    start = time.monotonic()
    await asyncio.gather(limiter.acquire(), limiter.acquire())
    elapsed = time.monotonic() - start
    assert elapsed >= 0.09


async def test_apex_client_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        ApexClient(api_key="")


async def test_rate_limiter_rejects_negative_interval() -> None:
    with pytest.raises(ValueError, match="min_interval"):
        RateLimiter(min_interval=-0.1)
