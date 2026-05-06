"""Job deduplication module — URL-based.

Dedup key is the canonical job URL (stripped of tracking params like
utm_source, utm_campaign, ref, etc.). Title-based dedup is wrong because
the same company can have multiple distinct roles with the same title
(different teams/orgs).

Run: uv run python -m wekruit_matching.scraper.dedup
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import psycopg
from loguru import logger

from wekruit_matching.models.job import Job

# Tracking params to strip from URLs before comparison
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "src", "referrer", "trk", "gh_jid", "gh_src",
    "lever_origin", "lever_source",
}

# Source priority: higher number = preferred (richer data)
SOURCE_PRIORITY = {
    "Summer2026-Internships": 1,
    "New-Grad-Positions": 1,
    "jobright-intern": 2,
    "jobright-newgrad": 2,
    # Phase 63 — multi-source v1.7
    "wellfound": 3,
    "linkedin": 4,
    "otta": 1,
}


def canonicalize_url(url: str) -> str:
    """Strip tracking params and normalize a job URL for dedup comparison.

    Examples:
        jobright.ai/jobs/info/abc123?utm_source=1099&utm_campaign=SWE
        → jobright.ai/jobs/info/abc123

        jobs.lever.co/company/abc123/apply?utm_source=Simplify&ref=Simplify
        → jobs.lever.co/company/abc123/apply
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        # Parse query params, drop tracking ones
        params = parse_qs(parsed.query, keep_blank_values=False)
        clean_params = {
            k: v for k, v in params.items()
            if k.lower() not in TRACKING_PARAMS
        }
        # Rebuild URL without tracking params
        clean_query = urlencode(clean_params, doseq=True) if clean_params else ""
        canonical = urlunparse((
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.params,
            clean_query,
            "",  # drop fragment
        ))
        return canonical
    except Exception:
        return url.strip().lower()


def _normalize_company(company: str | None) -> str:
    """Lowercase + strip non-alphanumerics for dedup company key."""
    if not company:
        return ""
    return re.sub(r"[^a-z0-9]", "", company.lower())


def _normalize_title(title: str | None) -> str:
    """Lowercase + sort tokens for fuzzy title key.

    Sorting tokens means "Senior Software Engineer" and "Software Engineer,
    Senior" hash to the same key.
    """
    if not title:
        return ""
    tokens = sorted(title.lower().split())
    return " ".join(tokens)


def dedup_multi_source(jobs: list[Job]) -> list[Job]:
    """Collapse the same job appearing in multiple sources into one Job.

    Phase 63 — v1.7. Run BEFORE upsert_jobs() so each (company, title,
    apply_url) shows up once with a merged ``sources`` array.

    Key: ``f"{company_norm}|{title_norm}|{apply_url_canonical}"``
        - company_norm: lowercase, alphanumeric-only
        - title_norm: lowercase, tokens sorted
        - apply_url_canonical: stripped of utm_*, ref, etc.

    On hit: merge ``sources`` arrays + take freshest ``first_seen_at``.
    The kept Job is the one with higher SOURCE_PRIORITY (linkedin > wellfound
    > jobright > simplify), preserving the richest payload available.

    Args:
        jobs: list of Job objects from one or more scrapers.

    Returns:
        Deduplicated list. Length ≤ len(jobs). Ordering not preserved.
    """
    if not jobs:
        return []

    seen: dict[str, Job] = {}
    for job in jobs:
        key = _build_key(job)
        if key not in seen:
            # Ensure sources is populated — fall back to source_repo
            if not job.sources:
                job.sources = [job.source_repo] if job.source_repo else []
            seen[key] = job
            continue

        existing = seen[key]
        # Merge sources — sorted, deduped
        merged_sources = sorted(set(
            (existing.sources or [])
            + (job.sources or [])
            + ([job.source_repo] if job.source_repo and job.source_repo not in (existing.sources or []) else [])
        ))
        existing.sources = merged_sources

        # Take fresher first_seen_at if newer
        if job.first_seen_at and (
            not existing.first_seen_at or job.first_seen_at > existing.first_seen_at
        ):
            existing.first_seen_at = job.first_seen_at

        # Keep last_seen_at as the latest of the two
        if job.last_seen_at and (
            not existing.last_seen_at or job.last_seen_at > existing.last_seen_at
        ):
            existing.last_seen_at = job.last_seen_at

        # Promote to higher-priority source_repo so downstream pipelines
        # treat this row as coming from the richer source.
        if (
            SOURCE_PRIORITY.get(job.source_repo, 0)
            > SOURCE_PRIORITY.get(existing.source_repo, 0)
        ):
            existing.source_repo = job.source_repo
            # Preserve richer payload fields if available
            if job.job_description and not existing.job_description:
                existing.job_description = job.job_description
            if job.required_skills and not existing.required_skills:
                existing.required_skills = job.required_skills
            if job.seniority_level and not existing.seniority_level:
                existing.seniority_level = job.seniority_level

    return list(seen.values())


def _build_key(job: Job) -> str:
    """3-tuple dedup key: company_norm | title_norm | url_canonical."""
    company = _normalize_company(job.company_name)
    title = _normalize_title(job.role_title)
    url = canonicalize_url(job.primary_url or "")
    return f"{company}|{title}|{url}"


def dedup_by_url(
    conn: psycopg.Connection,
    dry_run: bool = False,
    batch_size: int = 2000,
) -> dict[str, int]:
    """Deduplicate jobs by canonical URL.

    Jobs with the same base URL (after stripping tracking params) are
    duplicates. Keep the row with the richest data (prefer jobright source,
    then skills, then embedding). Mark the rest as status='duplicate'.

    Processes in batches to avoid statement timeouts on Supabase.
    """
    logger.info("Fetching all active job URLs...")

    # Fetch all active jobs with URLs — only the fields we need
    rows = conn.execute(
        """
        SELECT job_id, primary_url, source_repo,
               CASE WHEN required_skills IS NOT NULL AND required_skills != '{}' THEN 1 ELSE 0 END as has_skills,
               CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END as has_embedding,
               CASE WHEN job_description IS NOT NULL AND job_description != '' THEN 1 ELSE 0 END as has_jd
        FROM jobs
        WHERE status = 'active' AND primary_url IS NOT NULL AND primary_url != ''
        """
    ).fetchall()

    logger.info("Loaded {} active jobs with URLs", len(rows))

    # Group by canonical URL
    url_groups: dict[str, list[dict]] = {}
    for r in rows:
        canon = canonicalize_url(r["primary_url"])
        if not canon:
            continue
        url_groups.setdefault(canon, []).append(r)

    # Find groups with duplicates
    dup_ids_to_mark = []
    groups_merged = 0

    for canon_url, group in url_groups.items():
        if len(group) < 2:
            continue

        # Sort: prefer higher source priority, then has_jd, then has_skills, then has_embedding
        group.sort(key=lambda r: (
            SOURCE_PRIORITY.get(r["source_repo"], 0),
            r["has_jd"],
            r["has_skills"],
            r["has_embedding"],
        ), reverse=True)

        keeper = group[0]
        dupes = group[1:]

        groups_merged += 1
        for d in dupes:
            dup_ids_to_mark.append(d["job_id"])

    logger.info(
        "Found {} duplicate groups, {} total duplicate rows to mark",
        groups_merged, len(dup_ids_to_mark),
    )

    if dry_run:
        logger.info("Dry run — no changes made")
        return {"groups_merged": groups_merged, "duplicates_marked": len(dup_ids_to_mark)}

    # Mark duplicates in batches
    marked = 0
    for i in range(0, len(dup_ids_to_mark), batch_size):
        batch = dup_ids_to_mark[i : i + batch_size]
        conn.execute(
            "UPDATE jobs SET status = 'duplicate' WHERE job_id = ANY(%(ids)s)",
            {"ids": batch},
        )
        conn.commit()
        marked += len(batch)
        logger.info("Marked batch: {}/{}", marked, len(dup_ids_to_mark))

    logger.info("Dedup complete: {} groups merged, {} duplicates marked", groups_merged, marked)
    return {"groups_merged": groups_merged, "duplicates_marked": marked}


def run_dedup(conn: psycopg.Connection, dry_run: bool = False) -> dict[str, int]:
    """Run URL-based deduplication."""
    logger.info("=== Running URL-based job deduplication ===")
    return dedup_by_url(conn, dry_run=dry_run)


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    dry_run = "--dry-run" in sys.argv

    from wekruit_matching.config import get_settings
    conn = psycopg.connect(get_settings().database_url, row_factory=psycopg.rows.dict_row)

    stats = run_dedup(conn, dry_run=dry_run)
    logger.info("Final stats: {}", stats)

    # Show remaining active count
    active = conn.execute("SELECT COUNT(*) as c FROM jobs WHERE status='active'").fetchone()["c"]
    logger.info("Active jobs after dedup: {}", active)

    conn.close()
