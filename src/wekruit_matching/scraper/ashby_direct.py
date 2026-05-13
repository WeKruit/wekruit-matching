"""Ashby job-board public-API direct scraper.

Phase 73 — career-ops port (v1.7, 2026-05-06).

Ashby exposes its postings via a GraphQL-ish JSON endpoint at
``https://api.ashbyhq.com/posting-api/job-board/{company}``. No auth, no JS.

Output shape varies slightly across companies — both ``jobs`` (flat list) and
``jobBoard.jobs`` (nested) are observed in the wild. We probe both. Fields of
interest per job:
    {"title", "id", "departmentName", "teamName", "locationName",
     "publishedDate", "employmentType", "isRemote",
     "jobUrl" (or fall back to ``descriptionUrl``)}

Output:
    list[Job] — each with ``sources=['ashby']`` and
    ``source_repo='ashby:{slug}'``.

Usage:
    from wekruit_matching.scraper.ashby_direct import scrape_ashby_direct
    jobs = scrape_ashby_direct()
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any, Optional

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

ASHBY_BASE = "https://api.ashbyhq.com/posting-api/job-board"
USER_AGENT = "Mozilla/5.0 (compatible; WeKruit-Matching/1.7; +https://wekruit.com)"
REQUEST_TIMEOUT = 15.0
INTER_COMPANY_DELAY = 1.0
MAX_JOBS_PER_COMPANY = 200
SOURCE_NAME = "ashby"

# ---------------------------------------------------------------------------
# Curated Ashby job-board slugs. Several companies maintain Ashby AND
# Greenhouse/Lever simultaneously (e.g. Ramp, Linear). Multi-source dedup
# at Stage 1.6 will collapse on (company, title, url).
# ---------------------------------------------------------------------------

# Verified-active slugs as of 2026-05-06 — probed via posting-api/job-board.
# Many companies maintain Ashby AND Greenhouse/Lever simultaneously (e.g.
# notion appears here and *not* on greenhouse). Re-probe quarterly. Only
# slugs returning 200 with a non-empty payload are kept.
ASHBY_COMPANIES: list[str] = [
    # AI / ML labs (Ashby is the dominant choice)
    "openai", "ellipsislabs", "elevenlabs", "perplexity",
    "runway", "cohere", "vapi", "beam", "baseten",
    # Fintech
    "ramp", "alchemy",
    # Dev tools / infra
    "linear", "modal", "supabase", "neon", "warp", "notion",
    "semgrep", "clerk", "plain", "speakeasy", "saronic",
    # B2B
    "deel",
    # Bio / consumer
    "benchling", "opensea", "lambda",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrape_ashby_company(
    slug: str,
    *,
    client: Optional[httpx.Client] = None,
    max_jobs: int = MAX_JOBS_PER_COMPANY,
    timeout: float = REQUEST_TIMEOUT,
) -> list[Job]:
    """Scrape one Ashby job board. Tolerates both shapes (flat and nested)."""
    own_client = client is None
    cli = client or httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        url = f"{ASHBY_BASE}/{slug}"
        try:
            resp = cli.get(url)
        except httpx.HTTPError as e:
            logger.warning("ashby:{} request failed: {}", slug, e)
            return []
        if resp.status_code != 200:
            logger.warning("ashby:{} HTTP {} (skipping)", slug, resp.status_code)
            return []
        try:
            data = resp.json()
        except ValueError as e:
            logger.warning("ashby:{} bad JSON: {}", slug, e)
            return []

        company_display = _extract_company_display(data, fallback=slug)
        raw_jobs = _extract_jobs_array(data)
        if not raw_jobs:
            return []

        out: list[Job] = []
        seen_ids: set[str] = set()
        for raw in raw_jobs[:max_jobs]:
            job = _to_job(raw, slug=slug, company_display=company_display)
            if job is None:
                continue
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            out.append(job)
        return out
    finally:
        if own_client:
            cli.close()


def scrape_ashby_direct(
    *,
    client: Optional[httpx.Client] = None,
    companies: Optional[list[str]] = None,
    max_per_company: int = MAX_JOBS_PER_COMPANY,
    inter_company_delay: float = INTER_COMPANY_DELAY,
) -> list[Job]:
    """Iterate every configured Ashby company."""
    own_client = client is None
    cli = client or httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        targets = companies if companies is not None else ASHBY_COMPANIES
        out: list[Job] = []
        for i, slug in enumerate(targets):
            try:
                jobs = scrape_ashby_company(
                    slug, client=cli, max_jobs=max_per_company
                )
                if jobs:
                    logger.info("ashby:{} scraped {} jobs", slug, len(jobs))
                out.extend(jobs)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("ashby:{} unexpected error: {}", slug, e)
            if i < len(targets) - 1 and inter_company_delay > 0:
                time.sleep(inter_company_delay)
        logger.info("ashby_direct: {} total jobs across {} companies",
                    len(out), len(targets))
        return out
    finally:
        if own_client:
            cli.close()


# ---------------------------------------------------------------------------
# Internal — JSON shape probing + Job conversion
# ---------------------------------------------------------------------------


def _extract_jobs_array(data: Any) -> list[dict]:
    """Probe the two known Ashby JSON shapes and return a flat list of dicts."""
    if not isinstance(data, dict):
        return []
    # Shape A: {"jobs": [...]}
    jobs = data.get("jobs")
    if isinstance(jobs, list):
        return [j for j in jobs if isinstance(j, dict)]
    # Shape B: {"jobBoard": {"jobs": [...]}, ...} or {"data": {"jobs": [...]}}
    nested_keys = ("jobBoard", "data")
    for k in nested_keys:
        node = data.get(k)
        if isinstance(node, dict):
            inner = node.get("jobs")
            if isinstance(inner, list):
                return [j for j in inner if isinstance(j, dict)]
    return []


def _extract_company_display(data: Any, *, fallback: str) -> str:
    """Find the company display name in either shape."""
    if isinstance(data, dict):
        for k in ("name", "companyName", "organizationName"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for nested_key in ("jobBoard", "data"):
            node = data.get(nested_key)
            if isinstance(node, dict):
                for k in ("name", "companyName", "organizationName"):
                    v = node.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
    return fallback


def _to_job(
    raw: dict,
    *,
    slug: str,
    company_display: str,
) -> Optional[Job]:
    """Convert one Ashby job dict into a Job."""
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    apply_url = (
        (raw.get("jobUrl") or raw.get("descriptionUrl") or raw.get("applyUrl") or "")
        .strip()
    )
    if not title or not apply_url:
        return None

    company_norm = normalize_company_name(company_display or slug)
    if not company_norm:
        return None

    job_id = generate_job_id(f"{SOURCE_NAME}:{slug}", company_norm, title)
    content_hash = compute_content_hash(company_norm, title)

    location_raw = (raw.get("locationName") or raw.get("location") or "").strip()

    posted_str: str | None = None
    pub = raw.get("publishedDate") or raw.get("publishedAt")
    if isinstance(pub, str) and pub.strip():
        posted_str = pub.strip()
    elif isinstance(pub, (int, float)):
        try:
            posted_str = datetime.fromtimestamp(pub / 1000.0, UTC).isoformat()
        except (OSError, ValueError, OverflowError):
            posted_str = None

    seniority = infer_seniority(title)
    now = datetime.now(UTC)

    return Job(
        job_id=job_id,
        source_repo=f"{SOURCE_NAME}:{slug}",
        sources=[SOURCE_NAME],
        company_name=company_norm,
        role_title=title,
        primary_url=apply_url,
        location_raw=location_raw,
        date_posted_raw=posted_str,
        status=JobStatus.ACTIVE,
        content_hash=content_hash,
        seniority_level=seniority,
        role_function=infer_role_function(title),
        first_seen_at=now,
        last_seen_at=now,
    )


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    jobs = scrape_ashby_direct()
    logger.info("Scraped {} ashby jobs", len(jobs))
    for j in jobs[:10]:
        logger.info("  {} @ {} ({})", j.role_title, j.company_name, j.seniority_level)
