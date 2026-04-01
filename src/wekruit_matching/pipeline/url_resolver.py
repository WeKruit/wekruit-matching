"""Phase 16 URL resolution.

resolve_simplify_jobs() covers SimplifyJobs rows (RESOLVE-02): copies
primary_url directly to ats_apply_url when the URL is a known ATS link
(Greenhouse, Lever, Ashby, Workday). No network calls required.

resolve_via_slug_registry() covers JobRight rows (RESOLVE-03): fuzzy-matches
company_name against the 27K slug registry, then queries the ATS listings API
by job title to find the real ats_apply_url. No auth required.

resolve_via_serper() covers JobRight rows still missing after slug-registry
(RESOLVE-04): posts a targeted Serper.dev search query for each job and
extracts the first ATS result. Requires SERPER_API_KEY; gracefully skips when
the key is empty. 2,500 free queries/month covers daily resolution of unmatched
JobRight jobs.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from wekruit_matching.pipeline.url_classifier import FetchRoute, classify_job_url
from wekruit_matching.scraper.url_classifier import ATSTier

if TYPE_CHECKING:
    from wekruit_matching.scraper.slug_registry import SlugRegistry

# ATS tiers attempted in priority order — Workday is paid/complex so excluded.
_PRIORITY_ATS = [ATSTier.GREENHOUSE, ATSTier.LEVER, ATSTier.ASHBY]


def _normalize_title(title: str) -> str:
    """Lowercase and strip punctuation for title comparison."""
    return re.sub(r"[^a-z0-9 ]", " ", title.lower()).strip()


def _title_match(job_title: str, candidate: str, *, threshold: float = 0.4) -> bool:
    """Return True if job_title and candidate share sufficient token overlap (Jaccard).

    Args:
        job_title: The job title from the DB row.
        candidate: A title returned by an ATS listings API.
        threshold: Jaccard similarity floor (default 0.4).

    Returns:
        True if intersection / union >= threshold.
    """
    a = set(_normalize_title(job_title).split())
    b = set(_normalize_title(candidate).split())
    if not a or not b:
        return False
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union >= threshold


def _fetch_ats_listings(
    slug: str,
    ats_tier: ATSTier,
    client: httpx.Client,
) -> list[tuple[str, str]]:
    """Fetch all job listings for a company from a free ATS API.

    Returns a list of (title, url) tuples. Returns [] on any error or non-200.

    Args:
        slug: The ATS-specific company slug.
        ats_tier: Which ATS to query.
        client: Shared httpx.Client (caller owns lifecycle).

    Returns:
        List of (title, url) pairs, may be empty.
    """
    try:
        if ats_tier == ATSTier.GREENHOUSE:
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
            response = client.get(url)
            if response.status_code != 200:
                return []
            data = response.json()
            return [
                (str(job.get("title") or ""), str(job.get("absolute_url") or ""))
                for job in (data.get("jobs") or [])
                if isinstance(job, dict)
            ]

        if ats_tier == ATSTier.LEVER:
            url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
            response = client.get(url)
            if response.status_code != 200:
                return []
            data = response.json()
            if not isinstance(data, list):
                return []
            return [
                (str(job.get("text") or ""), str(job.get("hostedUrl") or ""))
                for job in data
                if isinstance(job, dict)
            ]

        if ats_tier == ATSTier.ASHBY:
            url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
            response = client.get(url)
            if response.status_code != 200:
                return []
            data = response.json()
            return [
                (str(job.get("title") or ""), str(job.get("jobUrl") or ""))
                for job in (data.get("jobs") or [])
                if isinstance(job, dict)
            ]

    except httpx.HTTPError as exc:
        logger.debug("url_resolver: ATS fetch failed for {}/{}: {}", ats_tier.value, slug, exc)

    return []


@dataclass(frozen=True, slots=True)
class ResolveResult:
    """Resolution outcome for a single job."""

    job_id: str
    company_name: str
    role_title: str
    resolved_url: str | None
    source: str | None  # "simplify_copy" | "slug_registry" | "serper" | None
    tier: str | None


def resolve_simplify_jobs(
    conn,
    *,
    batch_size: int = 500,
) -> dict:
    """Resolve SimplifyJobs rows by copying primary_url to ats_apply_url.

    For every active SimplifyJobs job (source_repo NOT LIKE 'jobright%') that
    has no ats_apply_url yet, classify the primary_url. If the route is NOT
    FIRECRAWL (i.e., it is a known ATS URL — Greenhouse, Lever, Ashby, or
    Workday), copy the primary_url directly to ats_apply_url. No network call
    required — zero-cost resolution.

    Skip condition: route == FetchRoute.FIRECRAWL — URL is unknown/aggregator,
    cannot be promoted blindly.

    Args:
        conn: Active psycopg3 connection (caller owns lifecycle/commit).
        batch_size: Max rows per DB round-trip. Capped at 500 per pipeline
            convention.

    Returns:
        dict with keys: resolved, skipped, errors.
    """
    effective_batch_size = min(batch_size, 500)
    resolved = skipped = errors = 0

    while True:
        rows = conn.execute(
            """
            SELECT job_id, company_name, role_title, primary_url
            FROM jobs
            WHERE status = 'active'
              AND source_repo NOT LIKE 'jobright%%'
              AND ats_apply_url IS NULL
              AND primary_url IS NOT NULL
              AND primary_url != ''
            ORDER BY first_seen_at DESC
            LIMIT %(limit)s
            """,
            {"limit": effective_batch_size},
        ).fetchall()

        if not rows:
            break

        batch_resolved = 0

        for row in rows:
            primary_url: str = row["primary_url"] or ""
            job_id: str = row["job_id"]

            try:
                classification = classify_job_url(primary_url)
            except Exception as exc:
                logger.warning(
                    "url_resolver: classify failed for job={} url={}: {}",
                    job_id,
                    primary_url,
                    exc,
                )
                errors += 1
                continue

            if classification.route == FetchRoute.FIRECRAWL:
                # Unknown or aggregator URL — skip, cannot copy blindly
                skipped += 1
                continue

            # Direct ATS URL — copy to ats_apply_url
            conn.execute(
                """
                UPDATE jobs
                SET ats_apply_url = %(url)s
                WHERE job_id = %(job_id)s
                """,
                {"url": primary_url, "job_id": job_id},
            )
            resolved += 1
            batch_resolved += 1

        if batch_resolved > 0:
            conn.commit()

    return {"resolved": resolved, "skipped": skipped, "errors": errors}


def resolve_via_slug_registry(
    conn,
    registry: "SlugRegistry",
    *,
    batch_size: int = 500,
) -> dict:
    """Resolve JobRight jobs via slug registry and ATS listings API (RESOLVE-03).

    For each active JobRight job without an ats_apply_url:
    1. Fuzzy-match company_name against the 27K slug registry.
    2. For each matched ATS (Greenhouse → Lever → Ashby in priority order):
       a. Fetch all job listings from the public ATS listings endpoint.
       b. Title-match the role_title against each listing.
       c. On first match, write ats_apply_url + jd_fetch_source and stop.
    3. If no ATS match, increment skipped.

    A 0.2s throttle is applied between requests to the same ATS domain to
    avoid hammering free endpoints.

    Args:
        conn: Active psycopg3 connection (caller owns lifecycle/commit).
        registry: Populated SlugRegistry with in-memory slug lookups.
        batch_size: Max rows per DB round-trip (capped at 500).

    Returns:
        dict with keys: resolved, skipped, errors.
    """
    effective_batch_size = min(batch_size, 500)
    resolved = skipped = errors = 0

    # Track last-request time per ATS domain for 0.2s throttle
    last_request_at: dict[str, float] = {}

    client = httpx.Client(timeout=10.0, follow_redirects=True)
    try:
        while True:
            rows = conn.execute(
                """
                SELECT job_id, company_name, role_title, primary_url
                FROM jobs
                WHERE status = 'active'
                  AND source_repo LIKE 'jobright%%'
                  AND ats_apply_url IS NULL
                ORDER BY first_seen_at DESC
                LIMIT %(limit)s
                """,
                {"limit": effective_batch_size},
            ).fetchall()

            if not rows:
                break

            batch_resolved = 0

            for row in rows:
                job_id: str = row["job_id"]
                company_name: str = row["company_name"] or ""
                role_title: str = row["role_title"] or ""

                try:
                    slug_map = registry.lookup_all_ats(company_name)
                except Exception as exc:
                    logger.warning(
                        "url_resolver: slug lookup failed for job={} company={}: {}",
                        job_id,
                        company_name,
                        exc,
                    )
                    errors += 1
                    continue

                if not slug_map:
                    skipped += 1
                    continue

                matched_url: str | None = None
                matched_source: str | None = None

                for ats_tier in _PRIORITY_ATS:
                    slug = slug_map.get(ats_tier)
                    if not slug:
                        continue

                    # 0.2s throttle per ATS domain
                    domain = ats_tier.value
                    now = time.monotonic()
                    prev = last_request_at.get(domain)
                    if prev is not None:
                        remaining = 0.2 - (now - prev)
                        if remaining > 0:
                            time.sleep(remaining)
                    last_request_at[domain] = time.monotonic()

                    try:
                        listings = _fetch_ats_listings(slug, ats_tier, client)
                    except Exception as exc:
                        logger.debug(
                            "url_resolver: listings fetch error for {}/{}: {}",
                            ats_tier.value,
                            slug,
                            exc,
                        )
                        continue

                    for title, listing_url in listings:
                        if not listing_url:
                            continue
                        if _title_match(role_title, title):
                            matched_url = listing_url
                            matched_source = f"slug_registry_{ats_tier.value}"
                            break

                    if matched_url:
                        break

                if matched_url:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET ats_apply_url = %(url)s,
                            jd_fetch_source = %(source)s
                        WHERE job_id = %(job_id)s
                        """,
                        {"url": matched_url, "source": matched_source, "job_id": job_id},
                    )
                    resolved += 1
                    batch_resolved += 1
                else:
                    skipped += 1

            if batch_resolved > 0:
                conn.commit()

    finally:
        client.close()

    return {"resolved": resolved, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# RESOLVE-04: Serper.dev search fallback
# ---------------------------------------------------------------------------

# ATS hostnames to accept in Serper organic results
_ATS_HOSTNAMES_FOR_SERPER = ("greenhouse.io", "lever.co", "ashbyhq.com")

_SERPER_URL = "https://google.serper.dev/search"


def _extract_serper_url(organic: list[dict]) -> str | None:
    """Return the first organic result link that matches a known ATS hostname.

    Args:
        organic: List of organic search result dicts from Serper.dev response.

    Returns:
        The first link containing a known ATS hostname, or None if not found.
    """
    for result in organic:
        link = result.get("link") or ""
        if any(host in link for host in _ATS_HOSTNAMES_FOR_SERPER):
            return link
    return None


def resolve_via_serper(
    conn,
    serper_api_key: str,
    *,
    batch_size: int = 500,
) -> dict:
    """Resolve JobRight jobs still missing ats_apply_url via Serper.dev search (RESOLVE-04).

    For each active JobRight job without an ats_apply_url after the slug-registry
    pass, issues a targeted Serper.dev search:
      '"{role_title}" "{company_name}" site:greenhouse.io OR site:lever.co OR site:ashbyhq.com'

    On finding a matching ATS URL in the organic results, writes it to
    ats_apply_url and sets jd_fetch_source = 'serper'.

    If serper_api_key is empty, returns immediately with queries_used=0 — no
    crash, no DB access.

    Rate limiting: 0.5s sleep between Serper API calls (free tier safety).

    Args:
        conn: Active psycopg3 connection (caller owns lifecycle/commit).
        serper_api_key: Serper.dev API key. Empty string = skip gracefully.
        batch_size: Max rows per DB round-trip (capped at 500).

    Returns:
        dict with keys: resolved, skipped, errors, queries_used.
    """
    if not serper_api_key:
        return {"resolved": 0, "skipped": 0, "errors": 0, "queries_used": 0}

    effective_batch_size = min(batch_size, 500)
    resolved = skipped = errors = queries_used = 0

    headers = {"X-API-KEY": serper_api_key, "Content-Type": "application/json"}

    with httpx.Client(timeout=15.0) as client:
        while True:
            rows = conn.execute(
                """
                SELECT job_id, company_name, role_title
                FROM jobs
                WHERE status = 'active'
                  AND source_repo LIKE 'jobright%%'
                  AND ats_apply_url IS NULL
                ORDER BY first_seen_at DESC
                LIMIT %(limit)s
                """,
                {"limit": effective_batch_size},
            ).fetchall()

            if not rows:
                break

            batch_resolved = 0

            for row in rows:
                job_id: str = row["job_id"]
                company_name: str = row["company_name"] or ""
                role_title: str = row["role_title"] or ""

                query = (
                    f'"{role_title}" "{company_name}"'
                    " site:greenhouse.io OR site:lever.co OR site:ashbyhq.com"
                )

                try:
                    response = client.post(
                        _SERPER_URL,
                        json={"q": query, "num": 5},
                        headers=headers,
                    )
                    queries_used += 1
                    response.raise_for_status()
                    data = response.json()
                except httpx.HTTPError as exc:
                    logger.warning(
                        "url_resolver: serper request failed for job={} company={}: {}",
                        job_id,
                        company_name,
                        exc,
                    )
                    errors += 1
                    time.sleep(0.5)
                    continue

                organic = data.get("organic") or []
                ats_url = _extract_serper_url(organic)

                if ats_url:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET ats_apply_url = %(url)s,
                            jd_fetch_source = 'serper'
                        WHERE job_id = %(job_id)s
                        """,
                        {"url": ats_url, "job_id": job_id},
                    )
                    resolved += 1
                    batch_resolved += 1
                else:
                    skipped += 1

                time.sleep(0.5)

            if batch_resolved > 0:
                conn.commit()

    return {"resolved": resolved, "skipped": skipped, "errors": errors, "queries_used": queries_used}
