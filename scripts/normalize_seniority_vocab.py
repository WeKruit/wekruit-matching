"""Normalize off-vocab seniority_level values to canonical intern|entry|mid|senior.

WHY: the live CF matcher (matchingJobRepository.ts:101) filters
`where('seniorityLevel','==', userValue.toLowerCase())` — an EXACT match. Jobs
stored as 'mid_level','manager','Entry Level','New Grad, Entry Level' etc. can
NEVER match a user filtering 'entry'/'mid'/'senior'. Same silent-hide bug class
as the sponsorship one. Collapsing to the 4 canonical values the matcher + the
seniority backfill use restores those jobs to the filter.

Mapping (job-LEVEL семantics; manager/director/staff/principal/vp/c_level are all
above 'senior' in the 4-bucket model, so → senior):
  mid_level                       -> mid
  junior, entry_level, Entry Level, "New Grad, Entry Level" -> entry
  manager, director, principal, vp, c_level, staff          -> senior

REVERSIBLE: writes job_id,old_value to data/seniority_normalize_backup.tsv before
any UPDATE. IDEMPOTENT: re-running maps 0 (canonical values are left alone).
Status-scoped to active (what users filter). Only touches seniority_level.

    uv run python scripts/normalize_seniority_vocab.py           # DRY RUN
    uv run python scripts/normalize_seniority_vocab.py --apply
"""
import sys

from wekruit_matching.db.connection import get_connection

_MAP = {
    "mid_level": "mid",
    "junior": "entry",
    "entry_level": "entry",
    "entry level": "entry",
    "new grad, entry level": "entry",
    "manager": "senior",
    "director": "senior",
    "principal": "senior",
    "vp": "senior",
    "c_level": "senior",
    "staff": "senior",
}
_CANON = {"intern", "entry", "mid", "senior"}
_BACKUP = "data/seniority_normalize_backup.tsv"


def main() -> int:
    apply = "--apply" in sys.argv
    with get_connection() as c:
        rows = c.execute(
            """
            SELECT job_id, seniority_level
            FROM jobs
            WHERE status='active' AND seniority_level IS NOT NULL
              AND lower(seniority_level) NOT IN ('intern','entry','mid','senior')
            ORDER BY job_id
            """
        ).fetchall()
        print(f"off-vocab active rows = {len(rows)}")

        planned = []
        unmapped = {}
        for r in rows:
            old = r["seniority_level"]
            new = _MAP.get(old.strip().lower())
            if new is None:
                unmapped[old] = unmapped.get(old, 0) + 1
                continue
            planned.append((r["job_id"], old, new))

        print(f"mappable = {len(planned)}")
        if unmapped:
            print(f"UNMAPPED (left untouched, add to _MAP if needed): {unmapped}")

        import os
        os.makedirs("data", exist_ok=True)
        with open(_BACKUP, "w") as f:
            f.write("job_id\told_seniority\tnew_seniority\n")
            for jid, old, new in planned:
                f.write(f"{jid}\t{old}\t{new}\n")
        print(f"reversibility backup -> {_BACKUP} ({len(planned)} rows)")

        if not apply:
            print(f"DRY_RUN — no writes. SELFCHECK={len(planned)*2 + 7}")
            return 0

        updated = 0
        for jid, old, new in planned:
            c.execute(
                "UPDATE jobs SET seniority_level=%(s)s "
                "WHERE job_id=%(j)s AND seniority_level=%(o)s",
                {"s": new, "j": jid, "o": old},
            )
            updated += 1
        c.commit()
        print(f"rows_updated = {updated}")

        remaining = c.execute(
            "SELECT count(*) n FROM jobs WHERE status='active' AND seniority_level IS NOT NULL "
            "AND lower(seniority_level) NOT IN ('intern','entry','mid','senior')"
        ).fetchone()["n"]
        print(f"off-vocab remaining = {remaining} (= UNMAPPED count)")
        print(f"SELFCHECK={updated*3 + remaining}")
    print("NORMALIZE_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
