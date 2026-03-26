"""SimplifyJobs scraper orchestrator.

Fetches both SimplifyJobs README files, parses them into Job records,
upserts to Postgres, and marks disappeared listings as inactive.

Standalone CLI usage:
    uv run python -m wekruit_matching.scraper.run

Or import and call programmatically:
    from wekruit_matching.scraper.run import scrape_all
    stats = scrape_all()
"""
import sys

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.scraper.fetcher import REPO_INTERNSHIPS, REPO_NEW_GRAD, fetch_readme
from wekruit_matching.scraper.parser import parse_readme
from wekruit_matching.scraper.upsert import mark_stale_jobs, upsert_jobs


REPOS = [REPO_INTERNSHIPS, REPO_NEW_GRAD]


def scrape_all() -> dict[str, dict]:
    """Fetch, parse, and upsert all SimplifyJobs listings.

    Fetches both Summer2026-Internships and New-Grad-Positions READMEs,
    parses each into Job objects, upserts to the jobs table, and marks
    any previously-seen jobs that did not appear in the latest scrape
    as inactive (per-repo scoped, never deletes rows).

    Returns per-repo stats:
        {
          "Summer2026-Internships": {"inserted": N, "updated": N, "unchanged": N, "stale": N},
          "New-Grad-Positions": {"inserted": N, "updated": N, "unchanged": N, "stale": N},
        }
    """
    all_stats: dict[str, dict] = {}

    with get_connection() as conn:
        for repo_slug in REPOS:
            logger.info("Scraping repo: {}", repo_slug)

            # Fetch
            content = fetch_readme(repo_slug)
            logger.debug("Fetched {} bytes from {}", len(content), repo_slug)

            # Parse
            jobs = parse_readme(content, repo_slug)
            logger.info("Parsed {} active jobs from {}", len(jobs), repo_slug)

            if not jobs:
                logger.warning("No jobs parsed from {} — skipping upsert", repo_slug)
                all_stats[repo_slug] = {"inserted": 0, "updated": 0, "unchanged": 0, "stale": 0}
                continue

            # Upsert
            upsert_stats = upsert_jobs(jobs, conn)

            # Mark stale: any active job from this repo NOT in this scrape's ID set
            seen_ids = {job.job_id for job in jobs}
            stale_count = mark_stale_jobs(seen_ids, repo_slug, conn)

            all_stats[repo_slug] = {**upsert_stats, "stale": stale_count}
            logger.info(
                "Repo {}: inserted={} updated={} unchanged={} stale={}",
                repo_slug,
                upsert_stats["inserted"],
                upsert_stats["updated"],
                upsert_stats["unchanged"],
                stale_count,
            )

    return all_stats


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting SimplifyJobs scraper run")
    stats = scrape_all()
    logger.info("Scrape complete. Stats: {}", stats)
