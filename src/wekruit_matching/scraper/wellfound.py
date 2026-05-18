"""Wellfound (formerly AngelList) scraper for senior+ SWE jobs.

Phase 63 — v1.7 (2026-05-06).

Wellfound's public job search has no auth requirement for unauthenticated
listings. We use their public sitemap.xml endpoint when available, and
fall back to HTML page scraping with conservative rate-limits.

Output:
    list[Job] with sources=['wellfound'], seniority_level inferred from
    title regex, primary_url pointing at the original wellfound listing.

Rate-limit: 1 req/sec, max 200 jobs/run. We do not page deep — Wellfound
discovery is "first 200 fresh results" only; deeper pagination is left to
manual + Phase 69 LinkedIn API.

Usage:
    from wekruit_matching.scraper.wellfound import scrape_wellfound
    jobs = scrape_wellfound()
"""
from __future__ import annotations

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

WELLFOUND_BASE = "https://wellfound.com"
WELLFOUND_JOBS_API = "https://wellfound.com/jobs/search"  # public JSON-ish endpoint
SITEMAP_URL = "https://wellfound.com/sitemap_jobs.xml"

USER_AGENT = "WeKruit-Matching/0.1 (+https://wekruit.com; senior-job aggregator)"
REQUEST_TIMEOUT = 15
RATE_LIMIT_DELAY = 1.0  # seconds between requests
MAX_JOBS_PER_RUN = 200
SOURCE_REPO_SLUG = "wellfound"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrape_wellfound(
    *,
    client: Optional[httpx.Client] = None,
    max_jobs: int = MAX_JOBS_PER_RUN,
) -> list[Job]:
    """Scrape senior+ SWE jobs from Wellfound.

    Strategy:
        1. Try sitemap.xml for fresh URLs
        2. Try JSON search endpoint (preferred — structured data)
        3. HTML scrape fallback per individual page

    Args:
        client: Optional injected httpx.Client (used for tests).
        max_jobs: Hard cap on jobs returned per run.

    Returns:
        list of Job objects, all with sources=['wellfound'].
    """
    own_client = client is None
    cli = client or httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html"},
        follow_redirects=True,
    )

    try:
        listings = _fetch_listings(cli, max_jobs=max_jobs)
        jobs = []
        seen_ids: set[str] = set()
        for raw in listings[:max_jobs]:
            job = _to_job(raw)
            if job is None:
                continue
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
        logger.info("wellfound: parsed {} jobs", len(jobs))
        return jobs
    finally:
        if own_client:
            cli.close()


# ---------------------------------------------------------------------------
# Internal: fetch + parse
# ---------------------------------------------------------------------------


def _fetch_listings(cli: httpx.Client, *, max_jobs: int) -> list[dict]:
    """Fetch raw listing dicts. Tries JSON endpoint, falls back to HTML.

    Each dict has at minimum: title, company, location, apply_url, posted_date.
    """
    listings: list[dict] = []

    # Try JSON search endpoint (with conservative query)
    try:
        params = {
            "role_types[]": "engineer",
            "experience_level[]": "senior",
            "experience_level[]": "principal",
            "country[]": "united-states",
            "remote[]": "yes",
            "limit": min(max_jobs, 200),
        }
        resp = cli.get(WELLFOUND_JOBS_API, params=params)
        time.sleep(RATE_LIMIT_DELAY)
        if resp.status_code == 200:
            try:
                data = resp.json()
                # Wellfound JSON shape varies — try common keys
                items = data.get("jobs") or data.get("results") or data.get("items") or []
                if items:
                    listings.extend(_normalize_json_items(items))
                    if listings:
                        return listings
            except (ValueError, KeyError) as e:
                logger.debug("wellfound: JSON parse failed: {}", e)
        elif resp.status_code in (403, 429):
            logger.warning("wellfound: rate-limited / blocked at JSON ({})", resp.status_code)
    except httpx.HTTPError as e:
        logger.warning("wellfound: JSON fetch failed: {}", e)

    # HTML scrape fallback — search page with senior+ filter
    try:
        url = f"{WELLFOUND_BASE}/role/senior-software-engineer"
        resp = cli.get(url)
        time.sleep(RATE_LIMIT_DELAY)
        if resp.status_code == 200:
            html_listings = _parse_html_jobs(resp.text)
            if html_listings:
                listings.extend(html_listings)
    except httpx.HTTPError as e:
        logger.warning("wellfound: HTML fetch failed: {}", e)

    return listings


def _normalize_json_items(items: list[dict]) -> list[dict]:
    """Normalize Wellfound JSON shape variations into a uniform dict."""
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name") or item.get("role_title")
        company = (
            item.get("startup", {}).get("name")
            if isinstance(item.get("startup"), dict)
            else item.get("company") or item.get("company_name")
        )
        location = item.get("location") or item.get("locations") or ""
        if isinstance(location, list):
            location = ", ".join(str(x) for x in location)
        apply_url = item.get("apply_url") or item.get("url") or item.get("link")
        if apply_url and not apply_url.startswith("http"):
            apply_url = f"{WELLFOUND_BASE}{apply_url}"
        posted = item.get("posted_date") or item.get("created_at") or item.get("posted_at")

        if not title or not company:
            continue
        out.append({
            "title": str(title).strip(),
            "company": str(company).strip(),
            "location": str(location).strip(),
            "apply_url": apply_url,
            "posted_date": posted,
        })
    return out


# Loose regex pulling job tile fields out of HTML. Wellfound's markup is
# CSR-heavy so this is a best-effort extractor — JSON endpoint is the
# preferred path. Brittle on purpose; if it breaks we fall through to zero.
_JOB_TILE_REGEX = re.compile(
    r'<a[^>]+href="(?P<href>/jobs/\d+[^"]*)"[^>]*>'
    r'.*?(?P<title>[A-Za-z][^<]{3,80})\s*</a>'
    r'.*?at\s+<[^>]+>(?P<company>[^<]+)</[^>]+>',
    re.DOTALL | re.IGNORECASE,
)


def _parse_html_jobs(html: str) -> list[dict]:
    """Parse Wellfound role page HTML for job tiles."""
    items: list[dict] = []
    for m in _JOB_TILE_REGEX.finditer(html):
        href = m.group("href")
        items.append({
            "title": m.group("title").strip(),
            "company": m.group("company").strip(),
            "location": "",
            "apply_url": f"{WELLFOUND_BASE}{href}",
            "posted_date": None,
        })
    return items


def _to_job(raw: dict) -> Optional[Job]:
    """Convert a raw Wellfound listing dict to a Job model."""
    title = (raw.get("title") or "").strip()
    company_raw = (raw.get("company") or "").strip()
    apply_url = (raw.get("apply_url") or "").strip()

    if not title or not company_raw:
        return None

    company = normalize_company_name(company_raw)
    if not company:
        return None

    job_id = generate_job_id(SOURCE_REPO_SLUG, company, title)
    content_hash = compute_content_hash(company, title)
    seniority = infer_seniority(title)

    posted_raw = raw.get("posted_date")
    if isinstance(posted_raw, datetime):
        posted_str = posted_raw.isoformat()
    elif posted_raw:
        posted_str = str(posted_raw)
    else:
        posted_str = None

    return Job(
        job_id=job_id,
        source_repo=SOURCE_REPO_SLUG,
        sources=[SOURCE_REPO_SLUG],
        company_name=company,
        role_title=title,
        primary_url=apply_url or None,
        ats_apply_url=(apply_url or None) if (apply_url or None) and "jobright" not in (apply_url or None) else None,
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

    jobs = scrape_wellfound()
    logger.info("Scraped {} wellfound jobs", len(jobs))
    for j in jobs[:5]:
        logger.info("  {} @ {} ({})", j.role_title, j.company_name, j.seniority_level)
