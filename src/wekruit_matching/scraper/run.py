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
from wekruit_matching.scraper.upsert import (
    mark_specific_ids_inactive,
    mark_stale_jobs,
    upsert_jobs,
)
from wekruit_matching.scraper.yc import (
    SOURCE_REPO_BARE as YC_SOURCE_REPO_BARE,
    fetch_yc_companies,
    scrape_yc,
)


SIMPLIFY_REPOS = [REPO_INTERNSHIPS, REPO_NEW_GRAD]
JOBRIGHT_REPOS = ["jobright-intern", "jobright-newgrad"]

# Phase A1 (2026-05-15): YC scraper kill-switch. Default ON; set
# YC_SCRAPER_ENABLED=0 (or false/no) to skip the YC fetch in emergencies
# (e.g. YC anti-bot / API outage) without code change.
def _yc_enabled() -> bool:
    import os
    raw = os.environ.get("YC_SCRAPER_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def scrape_all() -> dict[str, dict]:
    """Fetch, parse, and upsert all job listings from all sources.

    Sources:
    1. SimplifyJobs GitHub READMEs (Summer2026-Internships, New-Grad-Positions)
    2. JobRight.ai (intern-list.com, newgrad-jobs.com) — 10 categories each
    3. Y Combinator (ycombinator.com/jobs + companies API directory)

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
        # C/v3 (2026-05-13): when JOBRIGHT_USE_GIT_DELTA=1 the scrape returns the
        # pure-diff set (added rows -> new Jobs, removed rows -> stale_ids).
        # We mark only the specifically-removed ids inactive; we do NOT run
        # full mark_stale_jobs() on the delta set because the delta is a partial
        # view of "what changed" and the full active set lives in jobs that the
        # README still references but weren't touched in HEAD~1..HEAD.
        logger.info("Scraping JobRight GitHub repos (intern + newgrad)")
        try:
            jobright_jobs, stale_action_by_repo = scrape_jobright_github()
            logger.info("Fetched {} unique jobs from JobRight", len(jobright_jobs))

            # Group new jobs by source_repo for upsert.
            by_repo: dict[str, list] = {}
            for job in jobright_jobs:
                by_repo.setdefault(job.source_repo, []).append(job)

            # Iterate union of repos seen in either jobs-to-upsert or stale-action
            # registries so a repo with only removals (or only fallback parse) still
            # gets its stale tracking applied.
            all_repos = set(by_repo.keys()) | set(stale_action_by_repo.keys())
            for repo_slug in all_repos:
                jobs = by_repo.get(repo_slug, [])
                mode, ids = stale_action_by_repo.get(repo_slug, ("specific", set()))

                upsert_stats = upsert_jobs(jobs, conn) if jobs else {"inserted": 0, "updated": 0, "unchanged": 0}

                # mode == "specific": pure-diff path provided exact removed ids
                # mode == "full_scope": fallback path provided the full current
                # active set, so we use inverse semantics to flip anything missing.
                if mode == "specific":
                    stale_count = mark_specific_ids_inactive(ids, repo_slug, conn) if ids else 0
                elif mode == "full_scope":
                    stale_count = mark_stale_jobs(ids, repo_slug, conn)
                else:
                    stale_count = 0

                all_stats[repo_slug] = {**upsert_stats, "stale": stale_count, "stale_mode": mode}
                logger.info(
                    "JobRight {}: inserted={} updated={} unchanged={} stale={} ({})",
                    repo_slug, upsert_stats["inserted"], upsert_stats["updated"],
                    upsert_stats["unchanged"], stale_count, mode,
                )
        except Exception as e:
            logger.error("Failed to scrape JobRight: {}", e)
            all_stats["jobright"] = {"error": str(e)}

        # --- Y Combinator (Phase A1) ---
        # Two phases: (a) scrape active job postings off
        # ycombinator.com/jobs and upsert by source_repo='yc:<batch>';
        # (b) cache YC's public companies directory to disk so the PA-
        # side nightly enricher (paEnrichCompaniesNightly) can lookup
        # batch + tags without re-hitting YC's API. Both are wrapped
        # in their own try/except — neither blocks pipeline progress.
        if not _yc_enabled():
            logger.info("yc: disabled via YC_SCRAPER_ENABLED env")
            all_stats["yc"] = {"skipped": True}
        else:
            try:
                logger.info("Scraping Y Combinator job postings")
                yc_jobs = scrape_yc()
                logger.info("Fetched {} jobs from YC", len(yc_jobs))

                # YC postings come tagged with per-batch source_repo
                # ('yc:W25' etc.), so we group by repo for both upsert
                # and stale-marking — mirrors the jobright pattern.
                by_repo: dict[str, list] = {}
                for job in yc_jobs:
                    by_repo.setdefault(job.source_repo, []).append(job)

                for repo_slug, jobs in by_repo.items():
                    upsert_stats = upsert_jobs(jobs, conn)
                    seen_ids = {j.job_id for j in jobs}
                    stale_count = mark_stale_jobs(seen_ids, repo_slug, conn)
                    all_stats[repo_slug] = {**upsert_stats, "stale": stale_count}
                    logger.info(
                        "YC {}: inserted={} updated={} unchanged={} stale={}",
                        repo_slug, upsert_stats.get("inserted", 0),
                        upsert_stats.get("updated", 0),
                        upsert_stats.get("unchanged", 0), stale_count,
                    )

                if not yc_jobs:
                    all_stats[YC_SOURCE_REPO_BARE] = {
                        "inserted": 0, "updated": 0, "unchanged": 0, "stale": 0,
                    }
            except Exception as e:
                logger.error("Failed to scrape YC jobs: {}", e)
                all_stats["yc"] = {"error": str(e)}

            # Companies directory cache — non-fatal: if YC API is down
            # we just keep the previous snapshot. Logs but never raises.
            try:
                logger.info("Refreshing YC companies directory cache")
                companies = fetch_yc_companies()
                all_stats["yc_companies"] = {"cached": len(companies)}
            except Exception as e:
                logger.error("Failed to refresh YC companies cache: {}", e)
                all_stats["yc_companies"] = {"error": str(e)}

    return all_stats


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting multi-source scraper run")
    stats = scrape_all()
    logger.info("Scrape complete. Stats: {}", stats)
