"""LinkedIn scraper for senior+ SWE jobs.

Phase 63 — v1.7 (2026-05-06).

Two paths:

1. Official API mode (preferred when LINKEDIN_ACCESS_TOKEN is set in env).
   Uses LinkedIn's `Talent Solutions / Jobs` endpoints. This is the path
   Phase 69 SECRETS-03 will provision the token for. Until then we run in
   HTML mode only.

2. HTML scrape mode (default fallback). Hits LinkedIn's public unauthenticated
   `/jobs/search/?keywords=...&f_E=4,5,6` endpoint. Polite User-Agent,
   exponential backoff on 429, hard cap on jobs/run.

Output:
    list[Job] with sources=['linkedin'], seniority_level inferred from
    title regex.

Rate-limit: max 100 jobs/run by default, exponential backoff on 429.

Usage:
    from wekruit_matching.scraper.linkedin import scrape_linkedin
    jobs = scrape_linkedin()
"""
from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from typing import Optional

import httpx
from loguru import logger

from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper.id_utils import (
    compute_content_hash,
    generate_job_id,
    normalize_company_name,
)
from wekruit_matching.scraper.title_inference import infer_role_function, infer_seniority

# ---------------------------------------------------------------------------
# Endpoints + tunables
# ---------------------------------------------------------------------------

LINKEDIN_BASE = "https://www.linkedin.com"
LINKEDIN_PUBLIC_SEARCH = "https://www.linkedin.com/jobs/search/"
LINKEDIN_API_BASE = "https://api.linkedin.com/v2"

USER_AGENT = (
    "Mozilla/5.0 (compatible; WeKruit-Matching/0.1; +https://wekruit.com)"
)
REQUEST_TIMEOUT = 20
MAX_JOBS_PER_RUN = 100
RATE_LIMIT_DELAY = 1.5  # seconds between requests
SOURCE_REPO_SLUG = "linkedin"

# Exponential backoff schedule on 429: 5s, 15s, 45s, 120s
BACKOFF_DELAYS = [5, 15, 45, 120]

# LinkedIn's `f_E` (experience-level) filter codes:
#   1 = internship
#   2 = entry level
#   3 = associate
#   4 = mid-senior level
#   5 = director
#   6 = executive
# Senior+ = 4,5,6
SENIOR_FILTERS = "4,5,6"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrape_linkedin(
    *,
    keywords: str = "Software Engineer",
    location: str = "United States",
    client: Optional[httpx.Client] = None,
    max_jobs: int = MAX_JOBS_PER_RUN,
    access_token: Optional[str] = None,
) -> list[Job]:
    """Scrape senior+ SWE jobs from LinkedIn.

    Args:
        keywords: Free-text keyword query (default "Software Engineer").
        location: Location filter (default "United States").
        client: Optional injected httpx.Client (for testing).
        max_jobs: Hard cap on jobs returned per run.
        access_token: Optional LINKEDIN_ACCESS_TOKEN — if missing, falls back
            to HTML scraping. Phase 69 SECRETS-03 will provision this.

    Returns:
        list of Job objects, all with sources=['linkedin'].
    """
    own_client = client is None
    cli = client or httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
        follow_redirects=True,
    )

    token = access_token or os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()

    try:
        if token:
            logger.info("linkedin: using API mode (token present)")
            raw_listings = _fetch_via_api(cli, token, keywords, location, max_jobs)
        else:
            logger.info("linkedin: HTML scrape mode (no LINKEDIN_ACCESS_TOKEN)")
            raw_listings = _fetch_via_html(cli, keywords, location, max_jobs)

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        for raw in raw_listings[:max_jobs]:
            job = _to_job(raw)
            if job is None:
                continue
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
        logger.info("linkedin: parsed {} jobs", len(jobs))
        return jobs
    finally:
        if own_client:
            cli.close()


# ---------------------------------------------------------------------------
# Internal — API path
# ---------------------------------------------------------------------------


def _fetch_via_api(
    cli: httpx.Client,
    token: str,
    keywords: str,
    location: str,
    max_jobs: int,
) -> list[dict]:
    """Fetch via LinkedIn Talent Solutions API. Phase 69 will activate this."""
    url = f"{LINKEDIN_API_BASE}/jobSearch"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    params = {
        "keywords": keywords,
        "location": location,
        "experienceLevel": SENIOR_FILTERS,
        "count": min(max_jobs, 100),
    }
    try:
        resp = _get_with_backoff(cli, url, headers=headers, params=params)
        if resp is None or resp.status_code != 200:
            return []
        data = resp.json()
        return _normalize_api_items(data.get("elements", []))
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("linkedin: API mode failed: {}", e)
        return []


def _normalize_api_items(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        company = (
            item.get("companyDetails", {}).get("company", {}).get("name")
            if isinstance(item.get("companyDetails"), dict)
            else item.get("company")
        )
        location = item.get("formattedLocation") or item.get("location") or ""
        apply_url = item.get("applyUrl") or item.get("url")
        listed_at = item.get("listedAt")  # epoch ms
        posted_str = (
            datetime.fromtimestamp(listed_at / 1000, UTC).isoformat()
            if listed_at
            else None
        )

        if not title or not company:
            continue
        out.append({
            "title": str(title).strip(),
            "company": str(company).strip(),
            "location": str(location).strip(),
            "apply_url": apply_url,
            "posted_date": posted_str,
        })
    return out


# ---------------------------------------------------------------------------
# Internal — HTML path
# ---------------------------------------------------------------------------


def _fetch_via_html(
    cli: httpx.Client,
    keywords: str,
    location: str,
    max_jobs: int,
) -> list[dict]:
    """Polite HTML scrape of LinkedIn's public job-search page.

    Conservative: fetches only the first page of results (≈25 jobs), uses
    User-Agent header, sleeps RATE_LIMIT_DELAY between requests, and on 429
    backs off exponentially.
    """
    params = {
        "keywords": keywords,
        "location": location,
        "f_E": SENIOR_FILTERS,
        "f_TPR": "r604800",  # last 7 days
    }
    try:
        resp = _get_with_backoff(cli, LINKEDIN_PUBLIC_SEARCH, params=params)
    except httpx.HTTPError as e:
        logger.warning("linkedin: HTML fetch failed: {}", e)
        return []

    if resp is None or resp.status_code != 200:
        return []

    return _parse_html_jobs(resp.text)[:max_jobs]


# Pull job tiles out of the public search HTML. LinkedIn renders job cards
# as <a class="base-card__full-link" href="..."> with sub-spans for title /
# company / location. The exact class names move sometimes — keep this
# regex generous on whitespace.
_TITLE_TAG_RE = re.compile(
    r'<h3[^>]*class="[^"]*base-search-card__title[^"]*"[^>]*>\s*(?P<title>[^<]+)\s*</h3>',
    re.IGNORECASE,
)
_COMPANY_TAG_RE = re.compile(
    r'<h4[^>]*class="[^"]*base-search-card__subtitle[^"]*"[^>]*>'
    r'.*?<a[^>]*>\s*(?P<company>[^<]+)\s*</a>',
    re.DOTALL | re.IGNORECASE,
)
_LOCATION_TAG_RE = re.compile(
    r'<span[^>]*class="[^"]*job-search-card__location[^"]*"[^>]*>\s*(?P<loc>[^<]+)\s*</span>',
    re.IGNORECASE,
)
_HREF_TAG_RE = re.compile(
    r'<a[^>]*class="[^"]*base-card__full-link[^"]*"[^>]+href="(?P<href>[^"]+)"',
    re.IGNORECASE,
)
_CARD_BLOCK_RE = re.compile(
    r'<li[^>]*>\s*<div[^>]*class="[^"]*base-card[^"]*".*?</li>',
    re.DOTALL | re.IGNORECASE,
)


def _parse_html_jobs(html: str) -> list[dict]:
    """Best-effort extraction of job tiles from LinkedIn search HTML.

    Tile shape on the public page:
        <li><div class="base-card ...">
            <a class="base-card__full-link" href="https://...">
                <h3 class="base-search-card__title">Title</h3>
                <h4 class="base-search-card__subtitle">
                    <a>Company</a>
                </h4>
                <span class="job-search-card__location">Location</span>
            </a>
        </div></li>
    """
    items: list[dict] = []
    for block in _CARD_BLOCK_RE.findall(html):
        title_m = _TITLE_TAG_RE.search(block)
        company_m = _COMPANY_TAG_RE.search(block)
        href_m = _HREF_TAG_RE.search(block)
        loc_m = _LOCATION_TAG_RE.search(block)
        if not (title_m and company_m and href_m):
            continue
        items.append({
            "title": title_m.group("title").strip(),
            "company": company_m.group("company").strip(),
            "location": loc_m.group("loc").strip() if loc_m else "",
            "apply_url": href_m.group("href").strip(),
            "posted_date": None,
        })
    return items


# ---------------------------------------------------------------------------
# Internal — shared
# ---------------------------------------------------------------------------


def _get_with_backoff(
    cli: httpx.Client,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Optional[httpx.Response]:
    """GET with rate-limit aware exponential backoff on 429 / 503."""
    headers = headers or {}
    for attempt, delay in enumerate([0] + BACKOFF_DELAYS):
        if delay:
            logger.info("linkedin: backoff {}s (attempt {})", delay, attempt)
            time.sleep(delay)
        try:
            resp = cli.get(url, headers=headers, params=params)
        except httpx.HTTPError as e:
            logger.warning("linkedin: GET failed (attempt {}): {}", attempt, e)
            continue
        # success or unrecoverable — return as-is
        if resp.status_code not in (429, 503):
            time.sleep(RATE_LIMIT_DELAY)
            return resp
        logger.warning(
            "linkedin: rate-limited ({}) attempt={} url={}",
            resp.status_code, attempt, url,
        )
    logger.error("linkedin: gave up after {} retries on {}", len(BACKOFF_DELAYS), url)
    return None


def _to_job(raw: dict) -> Optional[Job]:
    """Convert a raw LinkedIn listing dict into a Job model."""
    title = (raw.get("title") or "").strip()
    company_raw = (raw.get("company") or "").strip()
    apply_url = (raw.get("apply_url") or "").strip()

    if not title or not company_raw:
        return None

    company = normalize_company_name(company_raw)
    if not company:
        return None

    job_id = generate_job_id(company, title, apply_url)
    content_hash = compute_content_hash(company, title)
    seniority = infer_seniority(title)

    posted = raw.get("posted_date")
    if isinstance(posted, datetime):
        posted_str = posted.isoformat()
    elif posted:
        posted_str = str(posted)
    else:
        posted_str = None

    return Job(
        job_id=job_id,
        source_repo=SOURCE_REPO_SLUG,
        sources=[SOURCE_REPO_SLUG],
        company_name=company,
        role_title=title,
        primary_url=apply_url or None,
        location_raw=str(raw.get("location") or ""),
        date_posted_raw=posted_str,
        status=JobStatus.ACTIVE,
        content_hash=content_hash,
        seniority_level=seniority,
        role_function=infer_role_function(title),
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    jobs = scrape_linkedin()
    logger.info("Scraped {} linkedin jobs", len(jobs))
    for j in jobs[:5]:
        logger.info("  {} @ {} ({})", j.role_title, j.company_name, j.seniority_level)
