"""Shared retrying HTTP GET for the direct-ATS scrapers (reliability audit
ranks 19-20).

The greenhouse/lever/ashby scrapers each did a bare ``client.get`` and returned
``[]`` on ANY error — including a transient 429/5xx/timeout. That collapses
"the board is rate-limiting us right now" into "this company has no jobs", which
(before the mark_stale circuit-breaker) silently mass-deactivated live rows and
still loses a day of freshness with no alarm. CLAUDE.md mandates tenacity +
exponential backoff for 429 handling.

``get_with_retry`` retries 429/5xx/network with exponential backoff + jitter and,
crucially, RAISES after exhausting retries so the caller can distinguish a
genuine empty-200 ("no jobs") from "we gave up" (a dependency error the daily
orchestrator should surface). A non-retryable 4xx (e.g. 404 board not found) is
returned as-is for the caller to handle.
"""
from __future__ import annotations

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ScrapeFetchError(RuntimeError):
    """Raised when a GET fails after exhausting retries — a real dependency
    error, NOT an empty board. Lets the caller record it instead of returning
    a misleading empty list."""


def _is_retryable(exc: BaseException) -> bool:
    # Network/timeout errors and our retryable-status wrapper.
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException, _RetryableStatus))


class _RetryableStatus(Exception):
    """Internal: a retryable HTTP status, surfaced so tenacity retries it."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"retryable HTTP {status_code}")


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
def _get_once(client: httpx.Client, url: str) -> httpx.Response:
    resp = client.get(url)
    if resp.status_code in _RETRYABLE_STATUS:
        raise _RetryableStatus(resp.status_code)
    return resp


def get_with_retry(client: httpx.Client, url: str, *, label: str) -> httpx.Response:
    """GET ``url`` with retry on 429/5xx/network. Returns the Response on a
    final non-retryable status (incl. a 4xx like 404). Raises ScrapeFetchError
    if all retries are exhausted on a retryable failure.

    ``label`` (e.g. "greenhouse:airbnb") is used only for logging.
    """
    try:
        return _get_once(client, url)
    except _RetryableStatus as e:
        logger.warning("{} gave up after retries (HTTP {})", label, e.status_code)
        raise ScrapeFetchError(f"{label}: HTTP {e.status_code} after retries") from e
    except (httpx.TransportError, httpx.TimeoutException) as e:
        logger.warning("{} gave up after retries (network: {})", label, e)
        raise ScrapeFetchError(f"{label}: network error after retries") from e
