"""Purge stale inactive jobs older than a threshold.

Prevents the jobs table from growing indefinitely. Jobs marked inactive
by the scraper are kept for a grace period (default 14 days) to allow
feedback/analytics queries, then permanently deleted.
"""
from datetime import datetime, timedelta, timezone

from loguru import logger

from wekruit_matching.db.connection import get_connection


def purge_stale_jobs(max_age_days: int = 14) -> int:
    """Delete inactive jobs older than max_age_days.

    Args:
        max_age_days: Jobs inactive for longer than this are deleted.
                      Default 14 days.

    Returns:
        Number of rows deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    with get_connection() as conn:
        # Also clean up feedback referencing deleted jobs
        cursor = conn.execute(
            """
            DELETE FROM feedback
            WHERE job_id IN (
                SELECT job_id FROM jobs
                WHERE status = 'inactive' AND last_seen_at < %s
            )
            """,
            (cutoff,),
        )
        feedback_deleted = cursor.rowcount

        cursor = conn.execute(
            """
            DELETE FROM jobs
            WHERE status = 'inactive' AND last_seen_at < %s
            """,
            (cutoff,),
        )
        jobs_deleted = cursor.rowcount
        conn.commit()

    logger.info(
        "Purged {} inactive jobs older than {} days ({} feedback rows cleaned)",
        jobs_deleted,
        max_age_days,
        feedback_deleted,
    )
    return jobs_deleted


if __name__ == "__main__":
    purge_stale_jobs()
