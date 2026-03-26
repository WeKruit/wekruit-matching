"""Multi-source scraper orchestrator.

Fetches jobs from SimplifyJobs (GitHub) and JobRight.ai, upserts to
Postgres, and marks disappeared listings as inactive.

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
from wekruit_matching.scraper.jobright_github import scrape_jobright_github
from wekruit_matching.scraper.parser import parse_readme
from wekruit_matching.scraper.upsert import mark_stale_jobs, upsert_jobs


SIMPLIFY_REPOS = [REPO_INTERNSHIPS, REPO_NEW_GRAD]
JOBRIGHT_REPOS = ["jobright-intern", "jobright-newgrad"]


def scrape_all() -> dict[str, dict]:
    """Fetch, parse, and upsert all job listings from all sources.

    Sources:
    1. SimplifyJobs GitHub READMEs (Summer2026-Internships, New-Grad-Positions)
    2. JobRight.ai (intern-list.com, newgrad-jobs.com) — 10 categories each

    Returns per-source stats dict.
    """
    all_stats: dict[str, dict] = {}

    with get_connection() as conn:
        # --- SimplifyJobs ---
        for repo_slug in SIMPLIFY_REPOS:
            logger.info("Scraping SimplifyJobs repo: {}", repo_slug)
            try:
                content = fetch_readme(repo_slug)
                jobs = parse_readme(content, repo_slug)
                logger.info("Parsed {} active jobs from {}", len(jobs), repo_slug)

                if not jobs:
                    all_stats[repo_slug] = {"inserted": 0, "updated": 0, "unchanged": 0, "stale": 0}
                    continue

                upsert_stats = upsert_jobs(jobs, conn)
                seen_ids = {job.job_id for job in jobs}
                stale_count = mark_stale_jobs(seen_ids, repo_slug, conn)
                all_stats[repo_slug] = {**upsert_stats, "stale": stale_count}
            except Exception as e:
                logger.error("Failed to scrape {}: {}", repo_slug, e)
                all_stats[repo_slug] = {"error": str(e)}

        # --- JobRight.ai (GitHub repos) ---
        logger.info("Scraping JobRight GitHub repos (intern + newgrad)")
        try:
            jobright_jobs = scrape_jobright_github()
            logger.info("Fetched {} unique jobs from JobRight", len(jobright_jobs))

            # Group by source_repo for upsert + stale marking
            by_repo: dict[str, list] = {}
            for job in jobright_jobs:
                by_repo.setdefault(job.source_repo, []).append(job)

            for repo_slug, jobs in by_repo.items():
                upsert_stats = upsert_jobs(jobs, conn)
                seen_ids = {job.job_id for job in jobs}
                stale_count = mark_stale_jobs(seen_ids, repo_slug, conn)
                all_stats[repo_slug] = {**upsert_stats, "stale": stale_count}
                logger.info(
                    "JobRight {}: inserted={} updated={} unchanged={} stale={}",
                    repo_slug, upsert_stats["inserted"], upsert_stats["updated"],
                    upsert_stats["unchanged"], stale_count,
                )
        except Exception as e:
            logger.error("Failed to scrape JobRight: {}", e)
            all_stats["jobright"] = {"error": str(e)}

    return all_stats


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting multi-source scraper run")
    stats = scrape_all()
    logger.info("Scrape complete. Stats: {}", stats)
