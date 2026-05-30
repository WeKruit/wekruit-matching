"""Backfill content_hash for active jobs where it's NULL/empty.

WHY: the Firestore sync receiver (matching-api matchingJobSchema) requires
`content_hash: z.string().min(1)`. Jobs synced with content_hash=NULL get a
400 "Invalid job sync payload" and (post commit 41a010f) are silently SKIPPED —
so they never reach the live matcher. ~328 active jobs (vc_board, firecrawl,
some ashby/greenhouse paths) were upserted without a content_hash and are
otherwise fully MATCHABLE. This recomputes the canonical hash so they sync.

content_hash = sha256(f"{company_name.strip()}|{role_title.strip()}") — computed
in Python via the SAME ``id_utils.compute_content_hash`` the scrapers use, so a
backfilled hash is byte-identical to a freshly-scraped one (no DB extension
dependency; pgcrypto's digest() is not installed).

SAFE: only touches active rows with NULL/empty content_hash and non-empty
company_name+role_title; idempotent; reversible (ids -> data/content_hash_backfilled_ids.txt).
The durable fix (scrapers/paths that don't set it) is separate — this drains the backlog.

    uv run python scripts/backfill_content_hash.py --dry-run
    uv run python scripts/backfill_content_hash.py
"""
from __future__ import annotations

import argparse
from pathlib import Path

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.scraper.id_utils import compute_content_hash

_REVERT = Path("data/content_hash_backfilled_ids.txt")

_SELECT = """
    SELECT job_id, company_name, role_title
    FROM jobs
    WHERE status = 'active'
      AND (content_hash IS NULL OR content_hash = '')
      AND company_name IS NOT NULL AND company_name <> ''
      AND role_title  IS NOT NULL AND role_title  <> ''
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with get_connection() as conn:
        rows = conn.execute(_SELECT).fetchall()
        logger.info(f"active null/empty content_hash with usable company+role: {len(rows)}")
        bad = conn.execute(
            """SELECT count(*) AS c FROM jobs WHERE status='active'
               AND (content_hash IS NULL OR content_hash='')
               AND (company_name IS NULL OR company_name='' OR role_title IS NULL OR role_title='')"""
        ).fetchall()[0]
        bad_c = bad["c"] if isinstance(bad, dict) else bad[0]
        if bad_c:
            logger.warning(f"{bad_c} active null-hash rows lack company/role — cannot backfill (left as-is)")

        if args.dry_run:
            logger.info("DRY-RUN: no writes.")
            print(f"WOULD_FIX={len(rows)}")
            print("BACKFILL_CH_DRY_DONE")
            return 0

        if not rows:
            print("FIXED=0")
            print("BACKFILL_CH_DONE")
            return 0

        fixed = 0
        _REVERT.parent.mkdir(parents=True, exist_ok=True)
        with _REVERT.open("w") as fh, conn.cursor() as cur:
            for r in rows:
                jid = r["job_id"] if isinstance(r, dict) else r[0]
                company = r["company_name"] if isinstance(r, dict) else r[1]
                role = r["role_title"] if isinstance(r, dict) else r[2]
                ch = compute_content_hash(company, role)
                cur.execute(
                    "UPDATE jobs SET content_hash = %(h)s "
                    "WHERE job_id = %(j)s AND (content_hash IS NULL OR content_hash = '')",
                    {"h": ch, "j": jid},
                )
                fh.write(jid + "\n")
                fixed += 1
                if fixed % 500 == 0:
                    logger.info(f"  updated {fixed}/{len(rows)}")
        conn.commit()
        logger.info(f"DONE: set content_hash on {fixed} rows. Revert ids -> {_REVERT}")
        print(f"FIXED={fixed}")
        print("BACKFILL_CH_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
