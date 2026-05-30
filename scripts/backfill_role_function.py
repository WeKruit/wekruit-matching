"""Backfill role_function for active jobs that have it empty/NULL.

role_function is a HARD filter (D1) in the wekruit-pa canonical matcher, but
``infer_role_function(title)`` was only wired into the jobright scraper for NEW
jobs (2026-05-21). Every job scraped before that date — ~18.7k jobright rows —
kept ``role_function = {}`` and was never backfilled. This script fills them
from the title using the SAME deterministic heuristic the scraper uses, so the
inference is consistent with new jobs. No LLM, no external API, no cost.

Reversible: every changed job_id is written to
``data/role_function_backfilled_ids.txt``. To revert, set role_function = '{}'
for those ids (all candidates were empty/NULL before this run).

Idempotent: re-running only touches rows still empty/NULL.

Propagation note: the Postgres->Firestore incremental sync is driven by an
``embedded_at`` watermark (job_sync.py), so this Postgres change does NOT reach
the live matcher until a full_sync runs OR embedded_at is bumped. This script
only fixes Postgres; propagation is a separate, deliberate step.

Usage:
    uv run python scripts/backfill_role_function.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.scraper.title_inference import infer_role_function

_REVERT_FILE = Path("data/role_function_backfilled_ids.txt")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn_cm = get_connection()
    conn = conn_cm.__enter__()

    limit_sql = f" LIMIT {int(args.limit)}" if args.limit else ""
    rows = conn.execute(
        f"""
        SELECT job_id, role_title
        FROM jobs
        WHERE status = 'active'
          AND (role_function IS NULL OR cardinality(role_function) = 0)
          AND role_title IS NOT NULL AND role_title <> ''
        ORDER BY job_id
        {limit_sql}
        """
    ).fetchall()

    total = len(rows)
    logger.info(f"candidates (active, role_function empty/NULL, has title): {total}")

    # Compute inferences locally first (no DB writes yet).
    updates: list[tuple[str, list[str]]] = []
    no_match = 0
    for r in rows:
        job_id = r["job_id"] if isinstance(r, dict) else r[0]
        title = r["role_title"] if isinstance(r, dict) else r[1]
        tags = infer_role_function(title)
        if tags:
            updates.append((job_id, tags))
        else:
            no_match += 1

    logger.info(
        f"would update: {len(updates)} | title yielded no role_function: {no_match}"
    )

    if args.dry_run:
        # Show a few examples so the mapping can be eyeballed.
        for job_id, tags in updates[:8]:
            title = next(
                (r["role_title"] if isinstance(r, dict) else r[1])
                for r in rows
                if (r["job_id"] if isinstance(r, dict) else r[0]) == job_id
            )
            logger.info(f"  e.g. {tags}  <-  {title!r}")
        logger.info("DRY-RUN: no writes performed.")
        return 0

    if not updates:
        logger.info("nothing to update.")
        return 0

    # Write changes. Single connection, single transaction, commit at end.
    _REVERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with _REVERT_FILE.open("w") as fh:
        with conn.cursor() as cur:
            for job_id, tags in updates:
                cur.execute(
                    "UPDATE jobs SET role_function = %(rf)s WHERE job_id = %(id)s",
                    {"rf": tags, "id": job_id},
                )
                fh.write(f"{job_id}\t{','.join(tags)}\n")
                written += 1
                if written % 2000 == 0:
                    logger.info(f"  updated {written}/{len(updates)}")
    conn.commit()
    logger.info(
        f"DONE: updated role_function on {written} rows. "
        f"Reversal ids -> {_REVERT_FILE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
