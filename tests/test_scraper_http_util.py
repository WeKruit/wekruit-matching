"""Tests for the retrying GET helper used by the direct-ATS scrapers (rank 19)."""
from __future__ import annotations

import httpx
import pytest

from wekruit_matching.scraper.http_util import ScrapeFetchError, get_with_retry


class _FakeClient:
    """Returns queued responses/exceptions on successive .get() calls."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def get(self, url):
        self.calls += 1
        out = self._outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _resp(status):
    return httpx.Response(status, request=httpx.Request("GET", "https://x/y"))


def test_returns_200_first_try():
    c = _FakeClient([_resp(200)])
    r = get_with_retry(c, "https://x/y", label="t")
    assert r.status_code == 200
    assert c.calls == 1


def test_retries_429_then_succeeds():
    c = _FakeClient([_resp(429), _resp(429), _resp(200)])
    r = get_with_retry(c, "https://x/y", label="t")
    assert r.status_code == 200
    assert c.calls == 3  # retried twice then succeeded


def test_persistent_5xx_raises_scrape_fetch_error():
    c = _FakeClient([_resp(503), _resp(503), _resp(503), _resp(503)])
    with pytest.raises(ScrapeFetchError):
        get_with_retry(c, "https://x/y", label="t")
    assert c.calls == 4  # stop_after_attempt(4)


def test_network_error_retries_then_raises():
    c = _FakeClient([
        httpx.ConnectError("boom"),
        httpx.ConnectError("boom"),
        httpx.ConnectError("boom"),
        httpx.ConnectError("boom"),
    ])
    with pytest.raises(ScrapeFetchError):
        get_with_retry(c, "https://x/y", label="t")
    assert c.calls == 4


def test_non_retryable_404_returned_not_raised():
    """A 404 (board not found) is terminal, not retryable — return it for the
    caller's existing non-200 handling, do not waste retries."""
    c = _FakeClient([_resp(404)])
    r = get_with_retry(c, "https://x/y", label="t")
    assert r.status_code == 404
    assert c.calls == 1
