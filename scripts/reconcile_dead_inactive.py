"""Reconcile dead/permanent_404 jobs that are still status='active' -> inactive.

Data-integrity fix (2026-05-29). dead_backfill (Stage 0) and the JD-enrichment
404 path set dead=true / permanent_404=true WITHOUT flipping status, and
upsert._filter_dead_tombstoned only SKIPS those rows from the upsert input — it
never deactivates an already-active dead row. Result: confirmed-dead postings sit
status='active' and (pre the job_sync dead-filter) were served as live matches.

This reconciler flips them to the correct state. It is:
  * Reversible — every affected job_id is written to a file BEFORE the UPDATE, so
    the exact set can be flipped back to 'active'.
  * Idempotent — re-running flips 0 more (the WHERE clause excludes inactive).
  * Conservative — only touches status; no other column is modified, and the
    90-day dead-retry path (upsert) still works (it keys on the dead flag, any
    status) so a still-listed job is re-activated on schedule.

Usage:
    uv run python scripts/reconcile_dead_inactive.py            # DRY RUN (default)
    uv run python scripts/reconcile_dead_inactive.py --apply    # perform the flip
"""
import sys

from wekruit_matching.db.connection import get_connection

_AFFECTED_IDS_FILE = "data/dead_inactive_reverted_ids.txt"

_PREDICATE = """
    status = 'active'
    AND (COALESCE(dead, FALSE) = TRUE OR COALESCE(permanent_404, FALSE) = TRUE)
"""


def main() -> int:
    apply = "--apply" in sys.argv

    with get_connection() as conn:
        # Capture the affected set FIRST (reversibility), regardless of mode.
        rows = conn.execute(
            f"SELECT job_id, dead, permanent_404 FROM jobs WHERE {_PREDICATE} "
            "ORDER BY job_id"
        ).fetchall()
        n = len(rows)
        dead_n = sum(1 for r in rows if r["dead"])
        p404_n = sum(1 for r in rows if r["permanent_404"])
        print(f"affected_active_rows = {n}  (dead={dead_n} permanent_404={p404_n})")

        import os
        os.makedirs("data", exist_ok=True)
        with open(_AFFECTED_IDS_FILE, "w") as f:
            for r in rows:
                f.write(r["job_id"] + "\n")
        print(f"reversibility_ids_written = {n} -> {_AFFECTED_IDS_FILE}")

        if not apply:
            print("DRY_RUN — no rows changed. Re-run with --apply to flip.")
            print(f"SELFCHECK={n * 2 + 1}")
            return 0

        result = conn.execute(
            f"UPDATE jobs SET status = 'inactive' WHERE {_PREDICATE}"
        )
        conn.commit()
        flipped = result.rowcount
        print(f"rows_flipped_to_inactive = {flipped}")

        # Verify: nothing left in the active+dead/404 state.
        remaining = conn.execute(
            f"SELECT count(*) AS c FROM jobs WHERE {_PREDICATE}"
        ).fetchone()["c"]
        print(f"remaining_active_dead_or_404 = {remaining}  (must be 0)")
        print(f"SELFCHECK={flipped * 3 + remaining}")
    print("RECONCILE_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
