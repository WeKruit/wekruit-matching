"""Otta scraper for senior+ SWE jobs.

Phase 63 — v1.7 (2026-05-06).

Otta's site is heavily client-side rendered (React SPA), so the public HTML
returns minimal job data without running JS. We attempt sitemap.xml first.
If the sitemap is empty / blocked, we return [] and skip cleanly — no
playwright dependency for now (deferred to v1.8 if Wellfound + LinkedIn
volume isn't sufficient).

Output:
    list[Job] with sources=['otta'].

Usage:
    from wekruit_matching.scraper.otta import scrape_otta
    jobs = scrape_otta()
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Optional
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper.id_utils import (
    compute_content_hash,
    generate_job_id,
    normalize_company_name,
)
from wekruit_matching.scraper.title_inference import infer_role_function, infer_seniority

OTTA_BASE = "https://app.otta.com"
OTTA_SITEMAP = "https://app.otta.com/sitemap.xml"
USER_AGENT = "WeKruit-Matching/0.1 (+https://wekruit.com)"
REQUEST_TIMEOUT = 15
RATE_LIMIT_DELAY = 1.5
MAX_JOBS_PER_RUN = 100
SOURCE_REPO_SLUG = "otta"

# TODO(v1.8): Otta is React SPA — for full job data we'd need playwright
# rendering. The current sitemap path returns URLs but title/company are
# derived from the slug only. Defer until Wellfound + LinkedIn coverage is
# insufficient. Phase 63 ships this as a stub returning [] when the
# sitemap returns no parseable entries.


def scrape_otta(
    *,
    client: Optional[httpx.Client] = None,
    max_jobs: int = MAX_JOBS_PER_RUN,
) -> list[Job]:
    """Scrape Otta job listings via sitemap.

    Returns an empty list if Otta's sitemap is unreachable or returns
    nothing parseable — which is the expected default at v1.7. Phase 63
    ships this as a stub for source-flag plumbing; full Otta integration
    is deferred to v1.8 (requires playwright SPA render).
    """
    own_client = client is None
    cli = client or httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml"},
        follow_redirects=True,
    )

    try:
        urls = _fetch_job_urls(cli, max_jobs=max_jobs)
        if not urls:
            logger.info("otta: no job URLs from sitemap; returning empty (deferred to v1.8)")
            return []

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        for entry in urls[:max_jobs]:
            job = _to_job(entry)
            if job is None:
                continue
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)
        logger.info("otta: parsed {} jobs from sitemap stubs", len(jobs))
        return jobs
    finally:
        if own_client:
            cli.close()


def _fetch_job_urls(cli: httpx.Client, *, max_jobs: int) -> list[dict]:
    """Read Otta's sitemap.xml — best-effort, returns [] if blocked."""
    try:
        resp = cli.get(OTTA_SITEMAP)
        time.sleep(RATE_LIMIT_DELAY)
    except httpx.HTTPError as e:
        logger.warning("otta: sitemap fetch failed: {}", e)
        return []

    if resp.status_code != 200:
        logger.info("otta: sitemap status={}", resp.status_code)
        return []

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.warning("otta: sitemap parse failed: {}", e)
        return []

    # Sitemap namespace
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    entries: list[dict] = []
    for url_node in root.findall(".//sm:url", ns):
        loc = url_node.findtext("sm:loc", default="", namespaces=ns).strip()
        lastmod = url_node.findtext("sm:lastmod", default=None, namespaces=ns)
        if not loc or "/jobs/" not in loc:
            continue
        # Slug-based title/company guessing
        slug = loc.rstrip("/").split("/")[-1]
        title_guess = slug.replace("-", " ").strip()
        if not title_guess:
            continue
        entries.append({
            "title": title_guess.title(),
            "company": "Unknown",  # sitemap doesn't expose company directly
            "apply_url": loc,
            "location": "",
            "posted_date": lastmod,
        })
        if len(entries) >= max_jobs:
            break
    return entries


def _to_job(raw: dict) -> Optional[Job]:
    title = (raw.get("title") or "").strip()
    company_raw = (raw.get("company") or "").strip()
    apply_url = (raw.get("apply_url") or "").strip()

    if not title or not apply_url:
        return None

    company = normalize_company_name(company_raw or "unknown")
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

    jobs = scrape_otta()
    logger.info("Scraped {} otta jobs", len(jobs))
    for j in jobs[:5]:
        logger.info("  {} @ {} ({})", j.role_title, j.company_name, j.seniority_level)
