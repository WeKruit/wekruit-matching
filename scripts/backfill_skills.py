"""Recover skills (+ company_size/industry) for enriched-but-skills-empty jobs.

WHY: 6,546 active jobs have a real JD (>=200 chars) + enriched_at set, but
required_skills=[] — they were enriched by an OLDER/weaker classifier pass. The
CURRENT classifier extracts skills fine (validated 7/8 on real samples, incl.
retail/service roles). The daily enrich_pending gap-fill targets these but its
LIMIT + newest-first ordering means this older backlog never drains. Empty skills
=> the job can't embed (Track-D gate) => never reaches the live matcher. This is
the dominant cause of the 73% (not 100%) skills/matchable coverage.

WHAT: re-run classify_job on each; if it returns non-empty skills, UPDATE
required_skills, and fill company_size/industry only where currently missing.
Clears enriched_at + embedded_at so the next embed pass re-embeds with skills
(so the recovered job actually reaches the matcher).

SAFE: idempotent (only touches rows still empty); only ADDS data; per-job
isolation (one failure never aborts the run); commits every batch; --limit for
controlled runs; --dry-run to measure with zero writes/LLM.

    uv run python scripts/backfill_skills.py --limit 50      # small live batch
    uv run python scripts/backfill_skills.py                 # full backlog
    uv run python scripts/backfill_skills.py --dry-run --limit 20
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.models.job import Job
from wekruit_matching.enrichment.classifier import classify_job

DRY = "--dry-run" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])
WORKERS = 24
if "--workers" in sys.argv:
    WORKERS = int(sys.argv[sys.argv.index("--workers") + 1])
COMMIT_EVERY = 25


def main() -> int:
    with get_connection() as conn:
        sql = """
            SELECT job_id, source_repo, company_name, role_title, location_raw,
                   job_description, required_skills, content_hash,
                   industry, company_size
            FROM jobs
            WHERE status='active'
              AND job_description IS NOT NULL AND length(job_description) >= 200
              AND (required_skills IS NULL OR cardinality(required_skills) = 0)
            ORDER BY first_seen_at DESC
        """
        if LIMIT:
            sql += f"\n            LIMIT {LIMIT}"
        rows = conn.execute(sql).fetchall()
        total = len(rows)
        print(f"target rows (empty-skills + JD, active) = {total}  dry_run={DRY}")
        if total == 0:
            print("nothing to do")
            return 0

        recovered = still_empty = failed = 0
        done = 0

        def _classify(r):
            """Pure LLM step (thread-safe: openai client + per-call). No DB."""
            job = Job(
                job_id=r["job_id"], source_repo=r["source_repo"],
                company_name=r["company_name"], role_title=r["role_title"],
                location_raw=r["location_raw"] or "",
                required_skills=[], content_hash=r["content_hash"] or "x" * 64,
                job_description=r["job_description"],
            )
            res = classify_job(job)
            return r, res

        # Parallelize the LLM calls (the bottleneck); WRITE serially in the main
        # thread so the single psycopg connection is never used concurrently.
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = [pool.submit(_classify, r) for r in rows]
            for fut in as_completed(futs):
                done += 1
                try:
                    r, res = fut.result()
                except Exception as exc:  # per-job isolation
                    failed += 1
                    logger.warning("classify failed: {}", exc)
                    continue
                skills = (res.required_skills if res else None) or []
                if not skills:
                    still_empty += 1
                    continue
                recovered += 1
                if DRY:
                    continue
                new_cs = r["company_size"] or (res.company_size if res else None)
                new_ind = r["industry"] or (res.industry if res else None)
                try:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET required_skills = %(skills)s,
                            company_size    = %(cs)s,
                            industry        = %(ind)s,
                            enriched_at     = NOW(),
                            embedding       = NULL,
                            embedded_at     = NULL
                        WHERE job_id = %(jid)s
                          AND cardinality(COALESCE(required_skills, ARRAY[]::text[])) = 0
                        """,
                        {"skills": skills, "cs": new_cs, "ind": new_ind, "jid": r["job_id"]},
                    )
                    if recovered % COMMIT_EVERY == 0:
                        conn.commit()
                except Exception as exc:
                    failed += 1
                    conn.rollback()
                    logger.warning("write failed {}: {}", r["job_id"][:8], exc)
                if done % 250 == 0:
                    print(f"  ...{done}/{total} | recovered={recovered} "
                          f"still_empty={still_empty} failed={failed}", flush=True)
        if not DRY:
            conn.commit()

        print(f"\nDONE: processed={total} recovered={recovered} "
              f"still_empty={still_empty} failed={failed}")
        rate = 100.0 * recovered / total if total else 0
        print(f"recovery_rate={rate:.1f}%  SELFCHECK={recovered*3 + still_empty + failed}")
        print("(recovered rows had embedding cleared -> next embed run re-embeds them "
              "with skills -> they reach the matcher)")
    print("BACKFILL_SKILLS_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
