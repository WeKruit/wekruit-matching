"""One-time bulk sync of all active embedded jobs plus inactive jobs to Firebase."""
from __future__ import annotations

import argparse
import sys

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

sys.path.insert(0, "src")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync matching jobs to Firebase in staged or full backfill mode."
    )
    parser.add_argument(
        "--active-limit",
        type=int,
        default=None,
        help="Sync at most this many active embedded jobs.",
    )
    parser.add_argument(
        "--active-offset",
        type=int,
        default=0,
        help="Skip this many active embedded jobs before syncing.",
    )
    parser.add_argument(
        "--skip-inactive",
        action="store_true",
        help="Skip inactive jobs for lightweight staged backfills.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    args = parse_args(argv)
    stats = sync_jobs_to_firebase(
        full_sync=True,
        active_limit=args.active_limit,
        active_offset=args.active_offset,
        include_inactive=not args.skip_inactive,
    )
    logger.info("Bulk Firebase sync complete: {}", stats)


if __name__ == "__main__":
    main()
