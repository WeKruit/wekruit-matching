"""Tests for GitHub authenticated fetcher (SCRP-07).

All HTTP calls are mocked — no real network calls made.
"""
import pytest
import httpx
from unittest.mock import patch, MagicMock

from wekruit_matching.config import Settings


# ---------------------------------------------------------------------------
# Fixture: isolated Settings (no .env file)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_settings(monkeypatch):
    """Provide Settings with test values, no .env file read."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x:x@localhost/x",
        anthropic_api_key="x",
        openai_api_key="x",
        github_token="test-token",
        api_secret_key="test-secret",
        siliconflow_api_key="sf-test",
    )
    # Patch get_settings to return our test settings
    with patch("wekruit_matching.scraper.fetcher.get_settings", return_value=settings):
        yield settings


# ---------------------------------------------------------------------------
# Helper: build a mock httpx response
# ---------------------------------------------------------------------------

def _make_response(status_code: int, content: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fetch_readme_internships_returns_bytes(mock_settings):
    """Test 1: fetch_readme returns bytes starting with b'#' for internships repo."""
    from wekruit_matching.scraper.fetcher import fetch_readme, REPO_INTERNSHIPS

    markdown_content = b"# Summer 2026 Internships\n\n## Jobs\n"
    mock_resp = _make_response(200, markdown_content)

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        result = fetch_readme(REPO_INTERNSHIPS)

    assert isinstance(result, bytes)
    assert result.startswith(b"#")
    mock_get.assert_called_once()
    # Verify Authorization header was passed
    call_kwargs = mock_get.call_args
    headers = call_kwargs.kwargs.get("headers") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else {}
    if not headers and call_kwargs.kwargs:
        headers = call_kwargs.kwargs.get("headers", {})
    assert "Authorization" in headers
    assert headers["Authorization"] == "Bearer test-token"


def test_fetch_readme_new_grad_returns_bytes(mock_settings):
    """Test 2: fetch_readme returns bytes for new grad repo."""
    from wekruit_matching.scraper.fetcher import fetch_readme, REPO_NEW_GRAD

    markdown_content = b"# New Grad Positions\n\n## Jobs\n"
    mock_resp = _make_response(200, markdown_content)

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        result = fetch_readme(REPO_NEW_GRAD)

    assert isinstance(result, bytes)
    assert result.startswith(b"#")
    mock_get.assert_called_once()


def test_fetch_readme_raises_on_401(mock_settings):
    """Test 3a: fetch_readme raises HTTPStatusError on 401 — not silent empty bytes."""
    from wekruit_matching.scraper.fetcher import fetch_readme, REPO_INTERNSHIPS

    mock_resp = _make_response(401)

    with patch("httpx.get", return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            fetch_readme(REPO_INTERNSHIPS)


def test_fetch_readme_raises_on_403(mock_settings):
    """Test 3b: fetch_readme raises HTTPStatusError on 403 — not silent empty bytes."""
    from wekruit_matching.scraper.fetcher import fetch_readme, REPO_INTERNSHIPS

    mock_resp = _make_response(403)

    with patch("httpx.get", return_value=mock_resp):
        with pytest.raises(httpx.HTTPStatusError):
            fetch_readme(REPO_INTERNSHIPS)


def test_fetch_readme_retries_429_then_raises(mock_settings):
    """Test 4: fetch_readme retries 3 times on 429 with backoff, then raises HTTPStatusError."""
    from wekruit_matching.scraper.fetcher import fetch_readme, REPO_INTERNSHIPS

    mock_resp_429 = _make_response(429)

    with patch("httpx.get", return_value=mock_resp_429) as mock_get:
        with patch("time.sleep") as mock_sleep:  # prevent actual sleeping in tests
            with pytest.raises(httpx.HTTPStatusError):
                fetch_readme(REPO_INTERNSHIPS)

    # Should have been called 3 times (the 3 retry attempts)
    assert mock_get.call_count == 3
    # Should have slept between retries (2 sleeps for 3 attempts: after attempt 0, after attempt 1)
    assert mock_sleep.call_count == 2


def test_repo_constants(mock_settings):
    """Test 5: Module exports correct repo slug constants."""
    from wekruit_matching.scraper.fetcher import REPO_INTERNSHIPS, REPO_NEW_GRAD

    assert REPO_INTERNSHIPS == "Summer2026-Internships"
    assert REPO_NEW_GRAD == "New-Grad-Positions"
