"""Unified error responses for Apex API integration failures.

Any `ApexError` raised inside a request is translated here into a JSON body
of the form `{"error": {"code": <status>, "message": <str>}}`. DB-level
"player not found" reuses `PlayerNotFoundError` so it maps to 404 with the
same shape.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.integrations.exceptions import (
    ApexAuthError,
    ApexError,
    ApexRateLimitError,
    ApexServerError,
    PlayerNotFoundError,
)

_STATUS_BY_TYPE: dict[type, int] = {
    PlayerNotFoundError: 404,
    ApexAuthError: 502,
    ApexRateLimitError: 503,
    ApexServerError: 502,
    ApexError: 502,
}


async def apex_error_handler(request: Request, exc: ApexError) -> JSONResponse:
    status = _STATUS_BY_TYPE.get(type(exc), 502)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": status, "message": str(exc)}},
    )
