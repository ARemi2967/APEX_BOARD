"""Async client for the apexlegendsapi.com /bridge endpoint.

Design notes
------------
- **Rate limiting**: a single `RateLimiter` serializes request starts so the
  gap between them is at least `min_interval` seconds. Default 0.3s →
  steady-state ~3.3 req/s, which keeps us safely under the upstream 5 req/s
  ceiling even accounting for the initial burst.
- **Retries**: tenacity wraps each call. Only `_RetryableStatus` (429/5xx)
  and `httpx.TransportError` are retried, with exponential backoff. Terminal
  4xx errors (auth / not found) bypass retry and raise immediately.
- **Error mapping**: at call boundaries we convert retryable failures into
  `ApexRateLimitError` / `ApexServerError`; non-retryable 4xx map to
  `ApexAuthError` / `PlayerNotFoundError`. All derive from `ApexError`.
- **429 disambiguation**: apexlegendsapi.com returns 429 both for real rate
  limits AND for "account not Discord-verified" policy errors. We peek at the
  body — a verification error maps to terminal `ApexAuthError` (retrying it is
  pointless); a plain 429 still retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .exceptions import (
    ApexAuthError,
    ApexError,
    ApexRateLimitError,
    ApexServerError,
    PlayerNotFoundError,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: float = 10.0
DEFAULT_MIN_INTERVAL: float = 0.3  # ~3.3 req/s steady-state, safely under 5 req/s
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_RETRY_MIN_WAIT: float = 0.5
DEFAULT_RETRY_MAX_WAIT: float = 4.0

# apexlegendsapi.com accepts these platform codes. PS5/Xbox Series are
# aliased to PS4/X1 by the upstream.
VALID_PLATFORMS: frozenset[str] = frozenset({"PC", "PS4", "X1"})


class RateLimiter:
    """Serialize request starts so consecutive starts are >= `min_interval` apart.

    The internal lock is held across the optional sleep, so concurrent
    `acquire()` callers form an orderly queue — they don't all race to the
    same wakeup instant.
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        self._min_interval = min_interval
        self._last_start: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_start)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_start = time.monotonic()


class _RetryableStatus(Exception):
    """Internal sentinel: tenacity retries when this is raised.

    Holds the upstream response so callers (mostly tests) can inspect what
    the final attempt returned.
    """

    def __init__(self, status: int, reason: str, response: httpx.Response) -> None:
        super().__init__(f"{status} {reason}")
        self.status = status
        self.response = response


def _is_verification_required(response: httpx.Response) -> bool:
    """True if a 429 is actually an 'account not Discord-verified' policy error.

    apexlegendsapi.com reuses 429 for this; the body is JSON with an `Error`
    field whose text mentions verification. A genuine rate-limit 429 has no
    such body, so we fall through to retry.
    """
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    return "verify" in str(body.get("Error", "")).lower()


class ApexClient:
    """Async client for `https://api.mozambiquehe.re/bridge`.

    Example:
        async with ApexClient(api_key) as client:
            data = await client.get_bridge("PlayerName", "PC")
    """

    BASE_URL = "https://api.mozambiquehe.re"

    def __init__(
        self,
        api_key: str,
        *,
        rate_limiter: RateLimiter | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_min_wait: float = DEFAULT_RETRY_MIN_WAIT,
        retry_max_wait: float = DEFAULT_RETRY_MAX_WAIT,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._limiter = rate_limiter or RateLimiter()
        self._max_retries = max_retries
        self._retry_min_wait = retry_min_wait
        self._retry_max_wait = retry_max_wait
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> "ApexClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def get_bridge(self, player: str, platform: str) -> dict[str, Any]:
        """Fetch the bridge profile by in-game username.

        Note: name lookup can miss — the upstream name-search falls back to a
        flaky 'low priority search service', and Steam-linked accounts may have
        an empty indexed name. Prefer `get_bridge_by_uid` when you have a UID.
        """
        if not player or not player.strip():
            raise ValueError("player must not be empty")
        return await self._fetch(
            {"player": player}, platform, identifier=player
        )

    async def get_bridge_by_uid(self, uid: str | int, platform: str) -> dict[str, Any]:
        """Fetch the bridge profile by Apex/Steam UID.

        More reliable than name lookup — bypasses the name-search fallback
        service entirely. Use this in the snapshot loop and anywhere a UID is
        already known.
        """
        uid_str = str(uid).strip()
        if not uid_str:
            raise ValueError("uid must not be empty")
        return await self._fetch(
            {"uid": uid_str}, platform, identifier=uid_str
        )

    async def _fetch(
        self,
        id_params: dict[str, str],
        platform: str,
        *,
        identifier: str,
    ) -> dict[str, Any]:
        """Shared GET /bridge with rate limiting, retry, and error mapping.

        Args:
            id_params: Either `{"player": name}` or `{"uid": uid}`.
            platform: One of `VALID_PLATFORMS`.
            identifier: The name or uid, used only for error messages.
        """
        if platform not in VALID_PLATFORMS:
            raise ValueError(
                f"invalid platform {platform!r}; expected one of {sorted(VALID_PLATFORMS)}"
            )

        params = {"auth": self._api_key, "platform": platform, **id_params}

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(
                multiplier=self._retry_min_wait,
                min=self._retry_min_wait,
                max=self._retry_max_wait,
            ),
            retry=retry_if_exception_type(
                (httpx.TransportError, _RetryableStatus)
            ),
            reraise=True,
        )

        response: httpx.Response | None = None
        try:
            async for attempt in retrying:
                with attempt:
                    await self._limiter.acquire()
                    logger.debug(
                        "bridge request %s platform=%s", identifier, platform
                    )
                    response = await self._client.get("/bridge", params=params)
                    self._classify_status(response, identifier, platform)
        except _RetryableStatus as e:
            if e.status == 429:
                raise ApexRateLimitError(
                    f"upstream rate limit (429) after {self._max_retries} attempts"
                ) from e
            raise ApexServerError(
                f"upstream {e.status} after {self._max_retries} attempts"
            ) from e
        except httpx.TransportError as e:
            raise ApexServerError(
                f"transport error after {self._max_retries} attempts: {e}"
            ) from e

        # Loop exited normally → last attempt succeeded and `response` is set.
        assert response is not None
        return response.json()

    @staticmethod
    def _classify_status(
        response: httpx.Response,
        identifier: str,
        platform: str,
    ) -> None:
        """Translate HTTP status into the right action.

        - 200: return (no raise).
        - 429 / 5xx: raise `_RetryableStatus` → tenacity retries.
        - 401 / 403: `ApexAuthError` (terminal).
        - 404: `PlayerNotFoundError` (terminal).
        - other 4xx: generic `ApexError` (terminal).
        """
        status = response.status_code
        if status == 200:
            return
        if status == 429:
            if _is_verification_required(response):
                raise ApexAuthError(
                    "API key requires Discord verification — visit "
                    "https://portal.apexlegendsapi.com/discord-auth"
                )
            raise _RetryableStatus(status, "rate limited", response)
        if status in (401, 403):
            raise ApexAuthError(
                f"auth failed (status={status}); check APEX_API_KEY"
            )
        if status == 404:
            raise PlayerNotFoundError(
                f"player {identifier!r} on platform {platform} not found"
            )
        if 500 <= status < 600:
            raise _RetryableStatus(status, "upstream server error", response)
        raise ApexError(f"unexpected status {status}: {response.text[:200]}")
