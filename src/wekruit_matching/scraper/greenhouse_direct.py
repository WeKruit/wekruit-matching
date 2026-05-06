"""Greenhouse Boards public-API direct scraper.

Phase 73 — career-ops port (v1.7, 2026-05-06).

Greenhouse exposes ``https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true``
as public, unauthenticated JSON. No OAuth, no JS, no Playwright. We iterate a
curated list of high-value engineering companies known to use Greenhouse and
collect every active posting they have.

Output:
    list[Job] — each with ``sources=['greenhouse']`` and
    ``source_repo='greenhouse:{slug}'`` so downstream dedup_multi_source()
    can collapse on (company, title, url) across providers (Lever, Ashby,
    LinkedIn, etc).

Rate-limit/politeness:
    - 1 req/sec inter-company delay
    - 15s timeout per request
    - On any non-200 / parse error → skip silently, continue with next slug

Usage:
    from wekruit_matching.scraper.greenhouse_direct import scrape_greenhouse_direct
    jobs = scrape_greenhouse_direct()
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

# ---------------------------------------------------------------------------
# Endpoints + tunables
# ---------------------------------------------------------------------------

GH_BASE = "https://boards-api.greenhouse.io/v1/boards"
USER_AGENT = "Mozilla/5.0 (compatible; WeKruit-Matching/1.7; +https://wekruit.com)"
REQUEST_TIMEOUT = 15.0
INTER_COMPANY_DELAY = 1.0  # seconds — polite to free public API
MAX_JOBS_PER_COMPANY = 200
SOURCE_NAME = "greenhouse"

# ---------------------------------------------------------------------------
# Curated company slugs — career-ops style. ~50 high-value engineering shops
# known to publish on Greenhouse Boards. Verified slug shapes via boards-api
# probes; some companies appear here AND in lever/ashby (handled by dedup).
# ---------------------------------------------------------------------------

# Verified-active slugs as of 2026-05-06. Probed with HEAD request against
# boards-api.greenhouse.io/v1/boards/<slug>/jobs — only those returning 200
# with non-empty payload are listed here. Companies that have moved off
# Greenhouse (e.g. notion → ashby, openai → ashby) are intentionally omitted
# to keep daily pipeline runtime down. Re-probe quarterly.
GREENHOUSE_COMPANIES: list[str] = [
    # AI / ML labs (greenhouse-hosted)
    "anthropic", "togetherai", "scaleai",
    # Dev tools / infra
    "stripe", "databricks", "figma", "vercel", "webflow",
    "instabase", "mercury", "brex",
    # Big eng orgs
    "discord", "datadog", "elastic", "mongodb", "cloudflare",
    "newrelic", "pagerduty", "twilio", "smartsheet", "workato",
    # Consumer / marketplaces
    "reddit", "twitch", "calendly", "axios",
    # Fintech / consumer
    "robinhood", "chime", "sofi", "instacart", "lyft", "airbnb",
    "affirm", "attentive", "block",
    # Collab / SaaS / B2B
    "asana", "intercom", "zoominfo",
    # Security / health
    "abnormalsecurity", "axonius", "benevity", "ionq",
]

# ---------------------------------------------------------------------------
# Title-based seniority regex — matches linkedin.py / wellfound.py shape.
# Order matters: more specific patterns come first.
# ---------------------------------------------------------------------------

SENIORITY_REGEX: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(c[teafmid]o|cxo)\b", re.IGNORECASE), "c_level"),
    (re.compile(r"\bchief\s+\w+(\s+\w+)?\s+officer\b", re.IGNORECASE), "c_level"),
    (re.compile(r"\b(vp|vice\s*president)\b", re.IGNORECASE), "vp"),
    (re.compile(r"\b(director|head\s+of)\b", re.IGNORECASE), "director"),
    (
        re.compile(
            r"\b(manager|engineering\s+lead|tech\s+lead|team\s+lead|architect)\b",
            re.IGNORECASE,
        ),
        "manager",
    ),
    (re.compile(r"\bprincipal\b", re.IGNORECASE), "principal"),
    (re.compile(r"\bstaff\b", re.IGNORECASE), "staff"),
    (re.compile(r"\b(intern(ship)?|co-?op)\b", re.IGNORECASE), "intern"),
    (
        re.compile(
            r"\b(new\s*grad(uate)?|entry[\s-]*level|early\s*career)\b",
            re.IGNORECASE,
        ),
        "entry_level",
    ),
    (
        re.compile(
            r"\b(senior|sr\.?\s+(eng(ineer)?|developer|analyst|manager|associate|consultant|designer|scientist|architect))\b",
            re.IGNORECASE,
        ),
        "senior",
    ),
    (
        re.compile(
            r"\b(junior|jr\.?\s+(eng(ineer)?|developer|analyst|associate|consultant))\b",
            re.IGNORECASE,
        ),
        "junior",
    ),
]


def _infer_seniority(title: str) -> str:
    """Map a free-text title to a canonical seniority bucket.

    Falls back to ``"mid_level"`` when no pattern matches — Greenhouse listings
    that are simply "Software Engineer" are typically mid-level/IC.
    """
    if not title or not isinstance(title, str):
        return "mid_level"
    for pattern, level in SENIORITY_REGEX:
        if pattern.search(title):
            return level
    return "mid_level"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrape_greenhouse_company(
    slug: str,
    *,
    client: Optional[httpx.Client] = None,
    max_jobs: int = MAX_JOBS_PER_COMPANY,
    timeout: float = REQUEST_TIMEOUT,
) -> list[Job]:
    """Scrape one Greenhouse Boards company page.

    Returns ``[]`` on any non-200 / network / parse error rather than raising,
    so a single dead slug doesn't kill the multi-company sweep.
    """
    own_client = client is None
    cli = client or httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        url = f"{GH_BASE}/{slug}/jobs?content=true"
        try:
            resp = cli.get(url)
        except httpx.HTTPError as e:
            logger.warning("greenhouse:{} request failed: {}", slug, e)
            return []
        if resp.status_code != 200:
            logger.warning(
                "greenhouse:{} HTTP {} (skipping)", slug, resp.status_code
            )
            return []
        try:
            data = resp.json()
        except ValueError as e:
            logger.warning("greenhouse:{} bad JSON: {}", slug, e)
            return []

        company_display = (data.get("name") or slug).strip()
        raw_jobs = data.get("jobs") or []
        if not isinstance(raw_jobs, list):
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


def scrape_greenhouse_direct(
    *,
    client: Optional[httpx.Client] = None,
    companies: Optional[list[str]] = None,
    max_per_company: int = MAX_JOBS_PER_COMPANY,
    inter_company_delay: float = INTER_COMPANY_DELAY,
) -> list[Job]:
    """Scrape every configured Greenhouse company in sequence.

    Args:
        client: Optional injected httpx.Client (testing).
        companies: Override default GREENHOUSE_COMPANIES list.
        max_per_company: Per-company hard cap.
        inter_company_delay: Politeness sleep between companies.

    Returns:
        Flat list[Job] across all companies. Caller is expected to feed this
        into dedup_multi_source() before upsert.
    """
    own_client = client is None
    cli = client or httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    try:
        targets = companies if companies is not None else GREENHOUSE_COMPANIES
        out: list[Job] = []
        for i, slug in enumerate(targets):
            try:
                jobs = scrape_greenhouse_company(
                    slug, client=cli, max_jobs=max_per_company
                )
                if jobs:
                    logger.info("greenhouse:{} scraped {} jobs", slug, len(jobs))
                out.extend(jobs)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("greenhouse:{} unexpected error: {}", slug, e)
            if i < len(targets) - 1 and inter_company_delay > 0:
                time.sleep(inter_company_delay)
        logger.info("greenhouse_direct: {} total jobs across {} companies",
                    len(out), len(targets))
        return out
    finally:
        if own_client:
            cli.close()


# ---------------------------------------------------------------------------
# Internal — JSON → Job conversion
# ---------------------------------------------------------------------------


_HTML_TAG_RX = re.compile(r"<[^>]+>")
_WHITESPACE_RX = re.compile(r"\s+")


def _strip_html(content: str | None) -> str:
    if not content:
        return ""
    text = _HTML_TAG_RX.sub(" ", content)
    text = _WHITESPACE_RX.sub(" ", text).strip()
    return text


def _to_job(
    raw: dict,
    *,
    slug: str,
    company_display: str,
) -> Optional[Job]:
    """Convert one Greenhouse job-dict into our Job model.

    Greenhouse JSON shape:
        {"id": int, "title": str, "absolute_url": str,
         "location": {"name": str},
         "updated_at": ISO8601 str (with Z),
         "content": HTML-encoded JD blob}
    """
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    apply_url = (raw.get("absolute_url") or "").strip()
    if not title or not apply_url:
        return None

    company_norm = normalize_company_name(company_display or slug)
    if not company_norm:
        return None

    job_id = generate_job_id(company_norm, title, apply_url)
    content_hash = compute_content_hash(company_norm, title)

    location_raw = ""
    loc = raw.get("location")
    if isinstance(loc, dict):
        location_raw = (loc.get("name") or "").strip()
    elif isinstance(loc, str):
        location_raw = loc.strip()

    posted_str: str | None = raw.get("updated_at") or None

    # JD content is HTML-escaped; strip tags + cap to keep upserts cheap.
    description_html = raw.get("content")
    description = _strip_html(description_html)[:5000] if description_html else None

    seniority = _infer_seniority(title)
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
        first_seen_at=now,
        last_seen_at=now,
        job_description=description,
    )


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    jobs = scrape_greenhouse_direct()
    logger.info("Scraped {} greenhouse jobs", len(jobs))
    for j in jobs[:10]:
        logger.info("  {} @ {} ({})", j.role_title, j.company_name, j.seniority_level)
