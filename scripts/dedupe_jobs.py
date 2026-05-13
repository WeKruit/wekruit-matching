"""Postgres jobs.job_id v2 dedupe + re-hash migration.

Background
----------
Pre-v2, ``generate_job_id(company, role, primary_url)`` included the
volatile primary_url. jobright-ai rotates its redirect-URL hex IDs every
time it rewrites its public README. Result: same (sourceRepo, company,
role) tuple landed multiple times in `jobs` with different job_ids
(Walgreens "Shift Lead" had 13 dupes in Firestore alone).

This migration:
  1. Computes the v2 ``new_job_id = sha256(sourceRepo|normalized_co|role)``
     for every existing row.
  2. Groups rows by (source_repo, new_job_id). For each group:
       - Keeps the row with the OLDEST ``first_seen_at`` (preserves the
         original ``enriched_at`` / ``embedded_at`` / ``embedding`` data
         so the migration is *free* — no re-LLM, no re-embed).
       - For the survivor, takes the most-recent ``last_seen_at``,
         merges ``sources`` arrays, OR-merges ``status`` (active wins
         over inactive).
       - DELETEs all non-survivor dupes.
  3. Updates the survivor row: ``job_id = new_job_id`` (PK swap). Safe
     because step 2 already collapsed all collisions.
  4. Runs in a single TX. Two modes:
       --dry-run    (default) — prints stats, no writes.
       --apply      — writes the changes.

FK note: as of alembic 0007 there is no FK referencing jobs.job_id (the
feedback table uses Text but no constraint), so the PK swap is safe.
We still wrap in a transaction so a constraint surprise rolls back.

Usage
-----
    .venv/bin/python scripts/dedupe_jobs.py --dry-run
    .venv/bin/python scripts/dedupe_jobs.py --apply
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from typing import Any

import psycopg

from wekruit_matching.config import settings
from wekruit_matching.scraper.id_utils import generate_job_id


def _v2_id(source_repo: str, company: str, role: str) -> str:
    return generate_job_id(source_repo, company, role)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Report only; no writes.")
    g.add_argument("--apply", action="store_true", help="Apply changes in a single transaction.")
    ap.add_argument("--limit", type=int, default=None, help="Cap rows scanned (debug).")
    args = ap.parse_args()

    dsn = settings.database_url
    print(f"[dedupe_jobs] connecting to {dsn.split('@')[-1] if '@' in dsn else dsn}")
    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            sql = """
                SELECT job_id, source_repo, company_name, role_title, first_seen_at,
                       last_seen_at, status, sources, enriched_at, embedded_at,
                       embedding IS NOT NULL AS has_embedding
                FROM jobs
            """
            if args.limit:
                sql += f" LIMIT {int(args.limit)}"
            cur.execute(sql)
            rows = cur.fetchall()
            print(f"[dedupe_jobs] scanned {len(rows)} rows from `jobs`")

            # Group by (source_repo, v2_new_id)
            groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for r in rows:
                (
                    old_job_id, source_repo, company_name, role_title,
                    first_seen_at, last_seen_at, status, sources,
                    enriched_at, embedded_at, has_embedding,
                ) = r
                new_id = _v2_id(source_repo, company_name, role_title)
                groups[(source_repo, new_id)].append({
                    "old_job_id": old_job_id,
                    "new_job_id": new_id,
                    "source_repo": source_repo,
                    "company_name": company_name,
                    "role_title": role_title,
                    "first_seen_at": first_seen_at,
                    "last_seen_at": last_seen_at,
                    "status": status,
                    "sources": sources or [],
                    "has_embedding": has_embedding,
                })

            dupe_groups = [g for g in groups.values() if len(g) > 1]
            singletons = [g for g in groups.values() if len(g) == 1]
            id_swaps_needed = sum(
                1 for g in singletons if g[0]["old_job_id"] != g[0]["new_job_id"]
            )
            print(f"[dedupe_jobs] unique (source_repo, v2_id) groups: {len(groups)}")
            print(f"[dedupe_jobs] dupe groups (count > 1):           {len(dupe_groups)}")
            print(f"[dedupe_jobs] singletons needing PK swap:        {id_swaps_needed}")
            phantom_rows = sum(len(g) - 1 for g in dupe_groups)
            print(f"[dedupe_jobs] phantom dupe rows (to delete):     {phantom_rows}")
            print(f"[dedupe_jobs] survivors after dedupe:            {len(groups)}")

            # Worst offenders
            worst = sorted(dupe_groups, key=lambda g: len(g), reverse=True)[:10]
            print("\n[dedupe_jobs] top 10 dupe groups:")
            for g in worst:
                survivor = min(g, key=lambda r: r["first_seen_at"])
                print(f"  {len(g):4d}x  {survivor['source_repo']:24s}  {survivor['company_name']:30s}  {survivor['role_title']}")

            if args.dry_run:
                print("\n[dedupe_jobs] DRY-RUN — no writes. Re-run with --apply to commit.")
                return 0

            # APPLY MODE
            print("\n[dedupe_jobs] APPLY: writing changes …")
            delete_count = 0
            swap_count = 0
            merge_count = 0
            for grp in groups.values():
                survivor = min(grp, key=lambda r: r["first_seen_at"])
                losers = [r for r in grp if r["old_job_id"] != survivor["old_job_id"]]

                if losers:
                    # Merge before delete: take latest last_seen_at + union sources + active>inactive status
                    latest_seen = max(r["last_seen_at"] for r in grp)
                    union_sources = sorted({s for r in grp for s in (r["sources"] or [])})
                    any_active = any(r["status"] == "active" for r in grp)
                    new_status = "active" if any_active else "inactive"
                    cur.execute(
                        """
                        UPDATE jobs
                        SET last_seen_at = %s, sources = %s, status = %s
                        WHERE job_id = %s
                        """,
                        (latest_seen, union_sources, new_status, survivor["old_job_id"]),
                    )
                    merge_count += 1
                    for loser in losers:
                        cur.execute("DELETE FROM jobs WHERE job_id = %s", (loser["old_job_id"],))
                        delete_count += 1

                # PK swap for survivor
                if survivor["old_job_id"] != survivor["new_job_id"]:
                    cur.execute(
                        "UPDATE jobs SET job_id = %s WHERE job_id = %s",
                        (survivor["new_job_id"], survivor["old_job_id"]),
                    )
                    swap_count += 1

            print(f"[dedupe_jobs] deleted phantom rows:  {delete_count}")
            print(f"[dedupe_jobs] merged survivors:      {merge_count}")
            print(f"[dedupe_jobs] PK swaps applied:      {swap_count}")

            # Final row count sanity
            cur.execute("SELECT COUNT(*) FROM jobs")
            (final,) = cur.fetchone()
            print(f"[dedupe_jobs] final jobs count:      {final}")
            conn.commit()
            print("[dedupe_jobs] COMMIT ok")

    return 0


if __name__ == "__main__":
    sys.exit(main())
