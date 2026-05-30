"""Flip Firestore matching-jobs docs that are status=active but whose Postgres
job is status=inactive (or absent) → inactive. Fixes stale-active served as
live matches (same class as the dead-jobs bug, ~5.6k docs as of 2026-05-30).

WHY this exists: scripts/sync_chunked.py only sends inactive rows on its final
window (offset>=120000), which the ~24.6k-active corpus never reaches — so
stale-active are never flipped. This is a targeted, direct Firestore reconcile.

SAFE:
- Read-only --dry-run prints the count, zero writes.
- Only flips docs that are CURRENTLY active in FS AND inactive/absent in PG.
- merge:true status-only write — touches nothing else (embedding, tags, etc).
- Reversible: every flipped id saved to data/firestore_stale_flipped_ids.txt;
  to revert, set those back to active (they were active before).
- Point-reads + batched writes (<=400/commit), no full-collection stream.

    uv run python scripts/reconcile_firestore_stale_active.py --dry-run
    uv run python scripts/reconcile_firestore_stale_active.py            # live
    uv run python scripts/reconcile_firestore_stale_active.py --limit 500
"""
from __future__ import annotations

import argparse
from pathlib import Path

from google.cloud import firestore
from google.oauth2 import service_account
from loguru import logger

from wekruit_matching.db.connection import get_connection

_SA = "/Users/adam/wekruit-matching/.firebase-sa.json"
_PROJECT = "wekruit-5f89b"
_COLLECTION = "matching-jobs"
_REVERT = Path("data/firestore_stale_flipped_ids.txt")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    creds = service_account.Credentials.from_service_account_file(_SA)
    db = firestore.Client(project=_PROJECT, credentials=creds)
    col = db.collection(_COLLECTION)
    print(f"TARGET={db.project}/{_COLLECTION}")
    if db.project != _PROJECT:
        print("ABORT: wrong project")
        return 2

    # PG job_id -> status (only need non-active ids to check)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE status='inactive'"
        ).fetchall()
    inactive_ids = [(r["job_id"] if isinstance(r, dict) else r[0]) for r in rows]
    if args.limit:
        inactive_ids = inactive_ids[: args.limit]
    logger.info(f"PG inactive ids to check: {len(inactive_ids)}")

    # Point-read each; collect those FS-active.
    to_flip: list[str] = []
    checked = 0
    for jid in inactive_ids:
        d = col.document(jid).get()
        checked += 1
        if d.exists and (d.to_dict() or {}).get("status") == "active":
            to_flip.append(jid)
        if checked % 1000 == 0:
            logger.info(f"  checked {checked}/{len(inactive_ids)}, stale so far {len(to_flip)}")

    logger.info(f"stale-active to flip: {len(to_flip)} of {checked} checked")

    if args.dry_run:
        logger.info("DRY-RUN: no writes.")
        print(f"WOULD_FLIP={len(to_flip)}")
        print("RECONCILE_DRY_DONE")
        return 0

    if not to_flip:
        print("FLIPPED=0")
        print("RECONCILE_DONE")
        return 0

    _REVERT.parent.mkdir(parents=True, exist_ok=True)
    flipped = 0
    with _REVERT.open("w") as fh:
        batch = db.batch()
        n_in_batch = 0
        for jid in to_flip:
            batch.set(col.document(jid), {"status": "inactive"}, merge=True)
            fh.write(jid + "\n")
            n_in_batch += 1
            flipped += 1
            if n_in_batch >= 400:
                batch.commit()
                batch = db.batch()
                n_in_batch = 0
                logger.info(f"  committed {flipped}/{len(to_flip)}")
        if n_in_batch:
            batch.commit()
    logger.info(f"DONE flipped {flipped} -> inactive. Revert ids: {_REVERT}")
    print(f"FLIPPED={flipped}")
    print("RECONCILE_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
