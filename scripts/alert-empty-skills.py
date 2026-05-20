"""Cron-callable alert: count active rows with empty required_skills.

User directive (2026-05-20): rows missing required_skills are observability
problems, not auto-flip targets. This script is a standalone wrapper around
``wekruit_matching.enrichment.worker.count_active_empty_skills`` so ops can
schedule it via system cron independently of the daily pipeline.

Exit codes (for cron / alerting integration):
  * 0 — count below threshold (no alert)
  * 1 — count at-or-above threshold (alert fired)
  * 2 — query/connection error

Threshold is read from ``ALERT_EMPTY_SKILLS_THRESHOLD`` env var (default 100).

Idempotent: read-only query, no DB writes. Safe to invoke at any frequency.

Usage::

    uv run python scripts/alert-empty-skills.py
    ALERT_EMPTY_SKILLS_THRESHOLD=500 uv run python scripts/alert-empty-skills.py
"""
from __future__ import annotations

import os
import sys

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.enrichment.worker import (
    ALERT_EMPTY_SKILLS_THRESHOLD,
    count_active_empty_skills,
)


def main() -> int:
    threshold = int(os.environ.get("ALERT_EMPTY_SKILLS_THRESHOLD", str(ALERT_EMPTY_SKILLS_THRESHOLD)))
    try:
        with get_connection() as conn:
            count = count_active_empty_skills(conn)
    except Exception as exc:
        logger.error("alert.empty_skills.query_failed: {}", exc)
        return 2

    if count >= threshold:
        logger.warning(
            "alert.empty_skills.active count={} threshold={} — investigate enrichment quality",
            count,
            threshold,
        )
        return 1
    logger.info("alert.empty_skills.active count={} threshold={} (ok)", count, threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
