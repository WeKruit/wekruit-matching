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
