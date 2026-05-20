"""Y Combinator scraper — Phase A1 (Company-Tier-YC, 2026-05-15).

Two endpoints, both public + unauth:

1. ``https://www.ycombinator.com/jobs`` — Inertia.js page where the active
   job-postings array is embedded as JSON in a ``<div data-page="...">``
   attribute on the root element. We parse this directly; no headless
   browser required. Cap: ~20 postings per page (YC's landing page exposes
   only the most-recent batch). When YC adds pagination this scraper
   continues to emit what it can — never blocks on missing data.

2. ``https://api.ycombinator.com/v0.1/companies`` — paginated JSON
   directory of every YC company across all batches. Used for downstream
   PA-side enrichment (Phase A5). We cache the full directory snapshot to
   disk at ``data/yc-companies-cache.json`` so the wekruit-pa
   ``paEnrichCompaniesNightly`` Cloud Function can read it through the
   Firestore-sync pipeline (or via direct copy if needed).

``source_repo`` convention (matches Adam's lock):
  * Batched jobs → ``yc:<batch>``, e.g. ``yc:W25`` / ``yc:S24``
  * Un-batched / missing-batch jobs → bare ``yc``

Job IDs are URL-free per v2 (``id_utils.generate_job_id``) so the YC
applyUrl rotating the signup_job_id query param won't generate
duplicates.

Usage::

    from wekruit_matching.scraper.yc import scrape_yc, fetch_yc_companies
    jobs = scrape_yc()              # list[Job]
    companies = fetch_yc_companies()  # list[dict] + writes cache file
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
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

YC_JOBS_URL = "https://www.ycombinator.com/jobs"
YC_COMPANIES_API = "https://api.ycombinator.com/v0.1/companies"

USER_AGENT = "WeKruit-Matching/0.1 (+https://wekruit.com; YC scraper)"
REQUEST_TIMEOUT = 20
RATE_LIMIT_DELAY = 1.0  # seconds between requests
MAX_JOB_PAGES = 20  # safety cap if YC ever paginates /jobs
MAX_COMPANY_PAGES = 300  # ~7500 cos at 25/page; YC currently has ~5900
SOURCE_REPO_BARE = "yc"


# ---------------------------------------------------------------------------
# Cache location — overridable via env for tests / alt deploys.
# ---------------------------------------------------------------------------

def _default_cache_path() -> Path:
    """Resolve <repo-root>/data/yc-companies-cache.json.

    Env override: ``YC_COMPANIES_CACHE_PATH`` (absolute path).
    """
    env = os.environ.get("YC_COMPANIES_CACHE_PATH")
    if env:
        return Path(env)
    # src/wekruit_matching/scraper/yc.py  → up 4 levels → repo root
    return Path(__file__).resolve().parents[3] / "data" / "yc-companies-cache.json"


# ---------------------------------------------------------------------------
# Public API — jobs
# ---------------------------------------------------------------------------


def scrape_yc(
    *,
    client: Optional[httpx.Client] = None,
    max_pages: int = MAX_JOB_PAGES,
) -> list[Job]:
    """Scrape active YC job postings from www.ycombinator.com/jobs.

    The page is an Inertia.js SPA whose initial state is embedded as a
    JSON string in the root ``data-page`` attribute. We extract it via
    regex (fast, no DOM parser dep) and walk ``props.jobPostings``.

    YC currently only exposes the first ~20 postings on this surface. We
    keep ``max_pages`` plumbing in case ``?page=N`` is enabled later
    (Inertia X-Inertia header path currently returns 409 — skipped).

    Returns:
        list[Job] with ``source_repo='yc:<batch>'`` (or bare ``'yc'``).
        Empty list on total failure — never raises.
    """
    own_client = client is None
    cli = client or httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        follow_redirects=True,
    )

    try:
        all_raw: list[dict] = []
        for page in range(1, max_pages + 1):
            try:
                params = {"page": page} if page > 1 else None
                resp = cli.get(YC_JOBS_URL, params=params)
                time.sleep(RATE_LIMIT_DELAY)
            except httpx.HTTPError as e:
                logger.warning("yc: jobs fetch failed page={}: {}", page, e)
                break

            if resp.status_code != 200:
                logger.info("yc: jobs page={} status={} — stopping", page, resp.status_code)
                break

            postings = _extract_job_postings(resp.text)
            if not postings:
                logger.info("yc: jobs page={} returned 0 postings — stopping", page)
                break

            all_raw.extend(postings)

            # YC currently only serves page 1; bail out after the first
            # successful pull so we don't burn 19 wasted requests fetching
            # an identical landing page. If a future scraper run sees a
            # different posting set on page 2 vs page 1 we'll know to
            # flip this back on.
            if page == 1:
                break

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        for raw in all_raw:
            job = _to_job(raw)
            if job is None:
                continue
            if job.job_id in seen_ids:
                continue
            seen_ids.add(job.job_id)
            jobs.append(job)

        logger.info("yc: parsed {} jobs ({} raw postings)", len(jobs), len(all_raw))
        return jobs
    finally:
        if own_client:
            cli.close()


# ---------------------------------------------------------------------------
# Public API — companies directory (cached to disk for A5 enricher)
# ---------------------------------------------------------------------------


def fetch_yc_companies(
    *,
    client: Optional[httpx.Client] = None,
    max_pages: int = MAX_COMPANY_PAGES,
    cache_path: Optional[Path] = None,
) -> list[dict]:
    """Crawl YC's public companies directory + write a cache snapshot.

    Endpoint paginates 25 companies/page (server ignores ``count`` query
    param) and exposes ``totalPages`` on every response so we know when
    to stop. We tolerate partial failure: if page K fails we return
    whatever we collected through K-1 and persist that to cache so the
    nightly enricher has at least *something* fresh.

    Args:
        client: Optional injected httpx.Client (for tests).
        max_pages: Safety cap on total pages walked. Defaults to 300.
        cache_path: Override destination for the cache file.

    Returns:
        list[dict] — every YC company we successfully fetched.
    """
    own_client = client is None
    cli = client or httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
    out_path = cache_path or _default_cache_path()

    companies: list[dict] = []
    total_pages: Optional[int] = None
    last_ok_page = 0

    try:
        for page in range(1, max_pages + 1):
            try:
                resp = cli.get(YC_COMPANIES_API, params={"page": page})
                time.sleep(RATE_LIMIT_DELAY)
            except httpx.HTTPError as e:
                logger.warning("yc: companies fetch failed page={}: {}", page, e)
                break

            if resp.status_code != 200:
                logger.warning("yc: companies page={} status={} — stopping", page, resp.status_code)
                break

            try:
                payload = resp.json()
            except ValueError as e:
                logger.warning("yc: companies json parse failed page={}: {}", page, e)
                break

            batch = payload.get("companies") or []
            if not isinstance(batch, list):
                logger.warning("yc: companies page={} bad shape — stopping", page)
                break

            companies.extend(batch)
            last_ok_page = page

            tp = payload.get("totalPages")
            if isinstance(tp, int) and tp > 0:
                total_pages = tp
                if page >= tp:
                    break

            if not payload.get("nextPage"):
                break

        _write_cache(companies, out_path, total_pages=total_pages, last_page=last_ok_page)
        logger.info(
            "yc: cached {} companies (pages 1..{} of {}) -> {}",
            len(companies), last_ok_page, total_pages or "?", out_path,
        )
        return companies
    finally:
        if own_client:
            cli.close()


def _write_cache(
    companies: list[dict],
    path: Path,
    *,
    total_pages: Optional[int],
    last_page: int,
) -> None:
    """Persist companies snapshot atomically (.tmp → rename)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        snapshot = {
            "fetched_at": datetime.now(UTC).isoformat(),
            "total_pages_seen": total_pages,
            "last_ok_page": last_page,
            "company_count": len(companies),
            "companies": companies,
        }
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False))
        tmp.replace(path)
    except OSError as e:
        logger.warning("yc: failed to write cache {}: {}", path, e)


# ---------------------------------------------------------------------------
# Internal: Inertia data-page extraction
# ---------------------------------------------------------------------------

# Matches the root element's ``data-page="{...}"`` attribute. YC currently
# HTML-escapes its embedded JSON, so we run html.unescape() before json.loads.
# Regex is intentionally narrow on the attribute boundary; we only want the
# topmost Inertia page mount, not any incidental data-page elsewhere.
_DATA_PAGE_REGEX = re.compile(r'data-page="((?:[^"\\]|\\.)*)"')


def _extract_job_postings(html_text: str) -> list[dict]:
    """Pull the Inertia.js ``jobPostings`` array out of YC's /jobs HTML.

    Returns [] if the marker is missing, JSON malformed, or postings list
    is absent — every failure is logged at debug level so a YC redesign
    surfaces in the daily run logs without breaking the rest of the
    pipeline.
    """
    m = _DATA_PAGE_REGEX.search(html_text)
    if not m:
        logger.debug("yc: no data-page attribute found in HTML")
        return []

    raw = html.unescape(m.group(1))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.debug("yc: data-page json parse failed: {}", e)
        return []

    props = data.get("props") or {}
    postings = props.get("jobPostings")
    if not isinstance(postings, list):
        logger.debug("yc: props.jobPostings missing or wrong type")
        return []

    return [p for p in postings if isinstance(p, dict)]


# ---------------------------------------------------------------------------
# Internal: posting → Job
# ---------------------------------------------------------------------------


def _resolve_source_repo(raw: dict) -> str:
    """Build ``yc:<batch>`` slug or fall back to bare ``yc``.

    Batch strings look like ``W25``, ``S24``, ``IK12`` (special programs).
    We accept any non-empty alphanumeric token but lowercase + strip
    whitespace for hash stability.
    """
    batch = (raw.get("companyBatchName") or "").strip()
    if not batch:
        return SOURCE_REPO_BARE
    # keep only alnum chars; SHA-256 hash key is case-insensitive anyway
    safe = re.sub(r"[^A-Za-z0-9]", "", batch)
    if not safe:
        return SOURCE_REPO_BARE
    return f"{SOURCE_REPO_BARE}:{safe.upper()}"


def _resolve_primary_url(raw: dict) -> Optional[str]:
    """Prefer the stable canonical ``/companies/<slug>/jobs/<jobslug>`` URL
    on ycombinator.com over the rotating account-signup ``applyUrl``.
    """
    url = raw.get("url") or raw.get("companyUrl")
    if isinstance(url, str) and url:
        if url.startswith("/"):
            return f"https://www.ycombinator.com{url}"
        return url
    apply_url = raw.get("applyUrl") or raw.get("ctaUrl")
    if isinstance(apply_url, str) and apply_url.startswith("http"):
        return apply_url
    return None


_JD_KEYS_IN_INERTIA = (
    "description",
    "jobDescription",
    "role",
    "summary",
    "roleDescription",
    "responsibilities",
    "details",
)


def _extract_jd_from_inertia(raw: dict) -> Optional[str]:
    """Pull a job-description body from the Inertia jobPostings dict, if present.

    YC's Inertia payload sometimes carries the full role description inline
    (long-form HTML/markdown) and sometimes only carries title + company —
    the SPA fetches the body via a follow-up GraphQL request that static
    Firecrawl scraping cannot replay. When the inline body is present, we
    pull it here so the Track D sync gate (>=200 chars JD) accepts the row
    without needing Firecrawl to land hydrated DOM. When absent we return
    None and the row falls through to the normal Stage 2b enrichment path.

    Strategy: probe a small list of plausible keys in priority order. Each
    value must be a non-empty string ≥200 chars to count — shorter strings
    are likely teaser snippets, not the full body.

    Idempotent: pure function on the input dict. Same payload → same output.
    """
    for key in _JD_KEYS_IN_INERTIA:
        value = raw.get(key)
        if isinstance(value, str) and len(value.strip()) >= 200:
            return value.strip()
    return None


def _to_job(raw: dict) -> Optional[Job]:
    """Convert a single Inertia ``jobPostings`` entry to a Job model."""
    title = (raw.get("title") or "").strip()
    company_raw = (raw.get("companyName") or "").strip()
    if not title or not company_raw:
        return None

    company = normalize_company_name(company_raw)
    if not company:
        return None

    source_repo = _resolve_source_repo(raw)
    job_id = generate_job_id(source_repo, company, title)
    content_hash = compute_content_hash(company, title)
    primary_url = _resolve_primary_url(raw)

    location = raw.get("location") or ""
    if isinstance(location, list):
        location = ", ".join(str(x) for x in location)

    posted = raw.get("createdAt") or raw.get("lastActive")
    posted_str = str(posted) if posted else None

    # 2026-05-20 (matching-quality launch blocker): pull inline JD from the
    # Inertia payload when available so the YC source contributes more than
    # title-only docs. Falls back to None when YC's payload doesn't include
    # the body — Stage 2b Firecrawl pass remains the secondary path.
    job_description = _extract_jd_from_inertia(raw)

    return Job(
        job_id=job_id,
        source_repo=source_repo,
        sources=[SOURCE_REPO_BARE],
        company_name=company,
        role_title=title,
        primary_url=primary_url,
        location_raw=str(location),
        date_posted_raw=posted_str,
        status=JobStatus.ACTIVE,
        content_hash=content_hash,
        job_description=job_description,
        seniority_level=infer_seniority(title),
        role_function=infer_role_function(title),
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    jobs = scrape_yc()
    logger.info("Scraped {} YC jobs", len(jobs))
    for j in jobs[:5]:
        logger.info("  {} @ {} [{}]", j.role_title, j.company_name, j.source_repo)

    cos = fetch_yc_companies(max_pages=2)
    logger.info("Companies cached: {}", len(cos))
