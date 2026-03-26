"""GitHub raw content fetcher for SimplifyJobs README files.

Authenticates using a GitHub PAT to avoid rate limits (GitHub May 2025
stricter limits on unauthenticated requests).

Usage:
    from wekruit_matching.scraper.fetcher import fetch_readme, REPO_INTERNSHIPS
    content: bytes = fetch_readme(REPO_INTERNSHIPS)
"""
import time

import httpx
from loguru import logger

from wekruit_matching.config import get_settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_INTERNSHIPS = "Summer2026-Internships"
REPO_NEW_GRAD = "New-Grad-Positions"

SIMPLIFY_ORG = "SimplifyJobs"
_RAW_BASE = "https://raw.githubusercontent.com"

_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_url(repo_slug: str) -> str:
    """Construct the raw GitHub URL for a SimplifyJobs repo README."""
    return f"{_RAW_BASE}/{SIMPLIFY_ORG}/{repo_slug}/dev/README.md"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_readme(repo_slug: str) -> bytes:
    """Fetch raw README bytes from a SimplifyJobs GitHub repo.

    Uses PAT authentication via Authorization header to avoid GitHub
    rate limits (GitHub May 2025 stricter limits on unauthenticated requests).
    Retries up to 3 times on 429 with exponential backoff (1s, 2s, 4s).
    Raises httpx.HTTPStatusError on 401, 403, or exhausted 429 retries.

    Args:
        repo_slug: SimplifyJobs repo name, e.g. REPO_INTERNSHIPS or REPO_NEW_GRAD.

    Returns:
        Raw README bytes (UTF-8 markdown content).

    Raises:
        httpx.HTTPStatusError: On 401, 403, or after 3 failed 429 retries.
    """
    url = _build_url(repo_slug)
    token = get_settings().github_token
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/plain",
    }

    last_response = None
    for attempt in range(_MAX_RETRIES):
        logger.debug("Fetching {} (attempt {}/{})", url, attempt + 1, _MAX_RETRIES)
        response = httpx.get(url, headers=headers)

        if response.status_code == 429:
            backoff = 2 ** attempt  # 1s, 2s, 4s
            logger.warning(
                "Rate limited (429) fetching {} — retrying in {}s (attempt {}/{})",
                url, backoff, attempt + 1, _MAX_RETRIES,
            )
            if attempt < _MAX_RETRIES - 1:
                time.sleep(backoff)
            last_response = response
            continue

        # For any non-429 response (success or other error), raise_for_status and return
        response.raise_for_status()
        logger.debug("Fetched {} bytes from {}", len(response.content), url)
        return response.content

    # Exhausted all retries on 429 — raise the last response's status error
    last_response.raise_for_status()
    # Should never reach here, but satisfy type checker
    raise RuntimeError("Unreachable: raise_for_status should have raised above")
