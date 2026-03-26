#!/usr/bin/env python
"""End-to-end pipeline exerciser for WeKruit Matching Engine.

Runs: scrape -> enrich -> embed -> match -> feedback
Requires a live Postgres instance and valid .env credentials.

Usage:
    uv run python scripts/e2e_test.py
"""
import sys

from loguru import logger

from wekruit_matching import get_matches, record_feedback
from wekruit_matching.scraper.run import scrape_all
from wekruit_matching.enrichment.run import enrich_all
from wekruit_matching.embedding.run import embed_all
from wekruit_matching.models.user_profile import UserProfile, JobType, CompanySizePreference

# Reset loguru to clean single-handler output
logger.remove()
logger.add(sys.stderr, level="INFO")


def run_pipeline() -> None:
    """Execute the full pipeline: scrape -> enrich -> embed -> match -> feedback."""

    # ------------------------------------------------------------------ #
    # Step 1: SCRAPE                                                        #
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 1: SCRAPE ===")
    scrape_stats = scrape_all()

    total_inserted = sum(repo["inserted"] for repo in scrape_stats.values())
    total_updated = sum(repo["updated"] for repo in scrape_stats.values())
    for repo_slug, stats in scrape_stats.items():
        logger.info(
            "  {} → inserted={} updated={} unchanged={} stale={}",
            repo_slug,
            stats["inserted"],
            stats["updated"],
            stats["unchanged"],
            stats["stale"],
        )

    if total_inserted + total_updated == 0:
        logger.warning(
            "No new or updated jobs — database may already be current"
        )

    # ------------------------------------------------------------------ #
    # Step 2: ENRICH                                                        #
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 2: ENRICH ===")
    enrich_stats = enrich_all()
    logger.info(
        "  Enrichment → enriched={} failed={}",
        enrich_stats["enriched"],
        enrich_stats["failed"],
    )

    # ------------------------------------------------------------------ #
    # Step 3: EMBED                                                         #
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 3: EMBED ===")
    embed_stats = embed_all()
    logger.info(
        "  Embedding → embedded={} failed={}",
        embed_stats["embedded"],
        embed_stats["failed"],
    )

    # ------------------------------------------------------------------ #
    # Step 4: MATCH                                                         #
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 4: MATCH ===")
    profile = UserProfile(
        user_id="e2e-test-user",
        skills=["Python", "machine learning", "SQL"],
        preferred_job_type=JobType.INTERN,
        preferred_locations=["Remote", "SF", "NYC"],
        requires_sponsorship=False,
        preferred_company_size=CompanySizePreference.ANY,
    )
    matches = get_matches(profile, top_n=10)

    print("\n=== TOP MATCHES ===")
    if not matches:
        logger.warning("No matches returned — check that jobs are enriched and embedded")
    else:
        for i, job in enumerate(matches):
            print(
                f"  [{i + 1}] {job['role_title']} @ {job['company_name']}"
                f" | score={job['score']:.3f} | {job['location_raw']}"
            )
        # Print detailed signals for the first result
        print(f"\n  Signals for result #1: {matches[0]['signals']}")

    # ------------------------------------------------------------------ #
    # Step 5: FEEDBACK                                                      #
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 5: FEEDBACK ===")
    feedback_recorded = False
    if matches:
        job_id = matches[0]["job_id"]
        record_feedback(
            user_id="e2e-test-user",
            job_id=job_id,
            reaction="like",
        )
        logger.info("Recorded 'like' feedback for job_id={}", job_id)
        feedback_recorded = True

    # ------------------------------------------------------------------ #
    # Step 6: SUMMARY                                                       #
    # ------------------------------------------------------------------ #
    print("\n=== PIPELINE SUMMARY ===")
    print(f"  Scrape stats:   {scrape_stats}")
    print(f"  Enrich stats:   {enrich_stats}")
    print(f"  Embed stats:    {embed_stats}")
    print(f"  Match count:    {len(matches)}")
    print(f"  Feedback recorded: {feedback_recorded}")
    print("E2E complete. All pipeline steps ran successfully.")


def main() -> None:
    try:
        run_pipeline()
        sys.exit(0)
    except Exception:
        logger.exception("E2E pipeline failed with an unhandled exception")
        sys.exit(1)


if __name__ == "__main__":
    main()
