"""Lever postings public-API direct scraper.

Phase 73 — career-ops port (v1.7, 2026-05-06).

Lever exposes ``https://api.lever.co/v0/postings/{company}?mode=json`` as a
public, unauthenticated JSON endpoint. Same pattern as Greenhouse: pre-curated
slug list, no JS, no Playwright, no auth tokens.

Output:
    list[Job] — each with ``sources=['lever']`` and
    ``source_repo='lever:{slug}'`` so downstream dedup_multi_source()
    collapses cross-provider duplicates.

Rate-limit/politeness:
    - 1 req/sec inter-company delay
    - 15s timeout per request
    - On any non-200 / parse error → skip silently

Usage:
    from wekruit_matching.scraper.lever_direct import scrape_lever_direct
    jobs = scrape_lever_direct()
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

LEVER_BASE = "https://api.lever.co/v0/postings"
USER_AGENT = "Mozilla/5.0 (compatible; WeKruit-Matching/1.7; +https://wekruit.com)"
REQUEST_TIMEOUT = 15.0
INTER_COMPANY_DELAY = 1.0
MAX_JOBS_PER_COMPANY = 200
SOURCE_NAME = "lever"

# ---------------------------------------------------------------------------
# Curated company slugs known to publish on Lever. Order is irrelevant.
# Cross-provider duplicates with greenhouse_direct.GREENHOUSE_COMPANIES are
# expected (e.g. some companies appear on both) — the multi-source dedup pass
# at Stage 1.6 will collapse them.
# ---------------------------------------------------------------------------

# Verified-active slugs as of 2026-05-06 — most lever public boards 404 because
# the company has migrated, customized the subdomain, or never opened the
# api.lever.co/v0/postings/<slug> path. Keep this list small and verified;
# add new slugs only after probing returns >0 jobs. Re-probe quarterly.
LEVER_COMPANIES: list[str] = [
    "spotify",      # ~10 active postings
    "ledger",       # crypto wallet — small
    "palantir",     # 10+ verified
    "clari",        # SaaS
    "highspot",     # SaaS
    "voltus",       # energy
    "olo",          # restaurant tech
    "livefront",    # consultancy
    "kraken",       # crypto exchange (lower-case slug)
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrape_lever_company(
    slug: str,
    *,
    client: Optional[httpx.Client] = None,
    max_jobs: int = MAX_JOBS_PER_COMPANY,
    timeout: float = REQUEST_TIMEOUT,
) -> list[Job]:
    """Scrape one Lever postings page. Returns ``[]`` on any failure."""
    own_client = client is None
    cli = client or httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        url = f"{LEVER_BASE}/{slug}?mode=json"
        try:
            resp = cli.get(url)
        except httpx.HTTPError as e:
            logger.warning("lever:{} request failed: {}", slug, e)
            return []
        if resp.status_code != 200:
            logger.warning("lever:{} HTTP {} (skipping)", slug, resp.status_code)
            return []
        try:
            data = resp.json()
        except ValueError as e:
            logger.warning("lever:{} bad JSON: {}", slug, e)
            return []

        if not isinstance(data, list):
            return []

        out: list[Job] = []
        seen_ids: set[str] = set()
        for raw in data[:max_jobs]:
            job = _to_job(raw, slug=slug)
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


def scrape_lever_direct(
    *,
    client: Optional[httpx.Client] = None,
    companies: Optional[list[str]] = None,
    max_per_company: int = MAX_JOBS_PER_COMPANY,
    inter_company_delay: float = INTER_COMPANY_DELAY,
) -> list[Job]:
    """Iterate every configured Lever company. See greenhouse_direct.scrape_greenhouse_direct."""
    own_client = client is None
    cli = client or httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        targets = companies if companies is not None else LEVER_COMPANIES
        out: list[Job] = []
        for i, slug in enumerate(targets):
            try:
                jobs = scrape_lever_company(
                    slug, client=cli, max_jobs=max_per_company
                )
                if jobs:
                    logger.info("lever:{} scraped {} jobs", slug, len(jobs))
                out.extend(jobs)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("lever:{} unexpected error: {}", slug, e)
            if i < len(targets) - 1 and inter_company_delay > 0:
                time.sleep(inter_company_delay)
        logger.info("lever_direct: {} total jobs across {} companies",
                    len(out), len(targets))
        return out
    finally:
        if own_client:
            cli.close()


# ---------------------------------------------------------------------------
# Internal — JSON → Job conversion
# ---------------------------------------------------------------------------


def _to_job(raw: dict, *, slug: str) -> Optional[Job]:
    """Convert one Lever posting dict into a Job model.

    Lever JSON shape (postings v0):
        {"id": str, "text": str (title), "hostedUrl": str, "applyUrl": str,
         "categories": {"location": str, "team": str, "commitment": str},
         "createdAt": int (epoch ms),
         "descriptionPlain": str,
         "additionalPlain": str}
    """
    if not isinstance(raw, dict):
        return None
    title = (raw.get("text") or "").strip()
    apply_url = (raw.get("hostedUrl") or raw.get("applyUrl") or "").strip()
    if not title or not apply_url:
        return None

    # Lever doesn't tell us company name in posting JSON — slug is canonical.
    company_norm = normalize_company_name(slug)
    if not company_norm:
        return None

    job_id = generate_job_id(f"{SOURCE_NAME}:{slug}", company_norm, title)
    content_hash = compute_content_hash(company_norm, title)

    location_raw = ""
    cats = raw.get("categories")
    if isinstance(cats, dict):
        location_raw = (cats.get("location") or "").strip()

    posted_str: str | None = None
    created_at = raw.get("createdAt")
    if isinstance(created_at, (int, float)):
        try:
            posted_str = datetime.fromtimestamp(
                created_at / 1000.0, UTC
            ).isoformat()
        except (OSError, ValueError, OverflowError):
            posted_str = None

    description_parts: list[str] = []
    desc_plain = raw.get("descriptionPlain")
    if isinstance(desc_plain, str) and desc_plain.strip():
        description_parts.append(desc_plain.strip())
    add_plain = raw.get("additionalPlain")
    if isinstance(add_plain, str) and add_plain.strip():
        description_parts.append(add_plain.strip())
    description = "\n\n".join(description_parts)[:5000] if description_parts else None

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
        job_description=description,
    )


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    jobs = scrape_lever_direct()
    logger.info("Scraped {} lever jobs", len(jobs))
    for j in jobs[:10]:
        logger.info("  {} @ {} ({})", j.role_title, j.company_name, j.seniority_level)
