"""Exception hierarchy for the Apex Legends API integration layer.

All errors raised by `ApexClient` derive from `ApexError`, so callers can
catch the entire family with one `except ApexError` if they don't care
about the specific reason.
"""

from __future__ import annotations


class ApexError(Exception):
    """Base for all Apex API integration errors."""


class ApexAuthError(ApexError):
    """API key is missing, invalid, or unauthorized (HTTP 401/403)."""


class PlayerNotFoundError(ApexError):
    """The requested player does not exist on the given platform (HTTP 404)."""


class ApexRateLimitError(ApexError):
    """Upstream returned 429 after exhausting retries."""


class ApexServerError(ApexError):
    """Upstream returned 5xx (or transport error) after exhausting retries."""
