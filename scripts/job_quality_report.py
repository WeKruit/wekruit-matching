"""Job-quality + coverage report for the WeKruit corpus.

ONE place to answer: are we at 100% on direct-ATS-link / company-enrichment /
tagging / job-description / skills — and where are the gaps + mismatches?

Read-only (every statement is a SELECT). Re-runnable:
    uv run python scripts/job_quality_report.py            # summary
    uv run python scripts/job_quality_report.py --samples  # + 10 example job_ids per gap
    uv run python scripts/job_quality_report.py --scope all # include inactive

Scope defaults to status='active' (what users can be served). The MATCHABLE
subset (what actually reaches the live matcher via job_sync) is reported too.
"""
import sys

from wekruit_matching.db.connection import get_connection

SHOW_SAMPLES = "--samples" in sys.argv
SCOPE_ALL = "--scope" in sys.argv and "all" in sys.argv
SCOPE_SQL = "TRUE" if SCOPE_ALL else "status = 'active'"
SCOPE_LABEL = "ALL jobs" if SCOPE_ALL else "ACTIVE jobs"


def main() -> None:
    with get_connection() as c:
        total = c.execute(f"SELECT count(*) n FROM jobs WHERE {SCOPE_SQL}").fetchone()["n"]
        if total == 0:
            print("No jobs in scope.")
            return

        # Each metric: (label, "good" predicate). pct = good / total.
        # "direct ATS link" = a real apply URL that is NOT a jobright redirect.
        metrics = [
            ("direct_ats_link",
             "ats_apply_url IS NOT NULL AND ats_apply_url <> '' "
             "AND ats_apply_url NOT ILIKE '%jobright%'"),
            ("any_apply_url (primary or ats)",
             "(primary_url IS NOT NULL AND primary_url <> '') "
             "OR (ats_apply_url IS NOT NULL AND ats_apply_url <> '')"),
            ("job_description (>=200 chars)",
             "job_description IS NOT NULL AND length(job_description) >= 200"),
            ("required_skills (non-empty)",
             "required_skills IS NOT NULL AND cardinality(required_skills) > 0"),
            ("industry (company enrich)",
             "industry IS NOT NULL AND industry <> '' AND industry <> 'unknown'"),
            ("company_size (company enrich)",
             "company_size IS NOT NULL AND company_size <> ''"),
            ("seniority_level (tag)",
             "seniority_level IS NOT NULL AND seniority_level <> ''"),
            ("role_function (tag)",
             "role_function IS NOT NULL AND cardinality(role_function) > 0"),
            ("enriched_at stamped",
             "enriched_at IS NOT NULL"),
            ("embedded",
             "embedding IS NOT NULL"),
        ]

        print(f"=== JOB QUALITY REPORT — {SCOPE_LABEL} (total={total}) ===\n")
        print(f"{'field':<34}{'have':>8}{'missing':>9}{'cov%':>8}")
        print("-" * 59)
        gaps = {}
        for label, pred in metrics:
            have = c.execute(
                f"SELECT count(*) n FROM jobs WHERE {SCOPE_SQL} AND ({pred})"
            ).fetchone()["n"]
            miss = total - have
            pct = 100.0 * have / total
            flag = "  <-- 100%" if miss == 0 else ("  <-- GAP" if pct < 90 else "")
            print(f"{label:<34}{have:>8}{miss:>9}{pct:>7.1f}%{flag}")
            gaps[label] = (pred, miss)

        # Matchable corpus = exact job_sync gate (what the live matcher receives).
        matchable = c.execute(
            f"""SELECT count(*) n FROM jobs WHERE {SCOPE_SQL}
                AND COALESCE(dead,FALSE)=FALSE AND COALESCE(permanent_404,FALSE)=FALSE
                AND embedding IS NOT NULL AND embedded_at IS NOT NULL
                AND job_description IS NOT NULL AND length(job_description)>=200
                AND required_skills IS NOT NULL AND cardinality(required_skills)>0"""
        ).fetchone()["n"]
        print("-" * 59)
        print(f"{'MATCHABLE (reaches live matcher)':<34}{matchable:>8}{total-matchable:>9}"
              f"{100.0*matchable/total:>7.1f}%")

        # ---- MISMATCHES / integrity flags (things that are wrong, not just absent) ----
        print("\n=== MISMATCHES / integrity flags ===")
        checks = [
            ("embedded but JD thin/missing (title-only vector)",
             "embedding IS NOT NULL AND (job_description IS NULL OR length(job_description) < 200)"),
            ("embedded but skills empty",
             "embedding IS NOT NULL AND (required_skills IS NULL OR cardinality(required_skills)=0)"),
            ("enriched but industry NULL (enrich miss)",
             "enriched_at IS NOT NULL AND (industry IS NULL OR industry='')"),
            ("enriched but skills empty (extract miss)",
             "enriched_at IS NOT NULL AND (required_skills IS NULL OR cardinality(required_skills)=0)"),
            ("active+dead (should be inactive)",
             "COALESCE(dead,FALSE)=TRUE" if not SCOPE_ALL else "FALSE"),
            ("active+permanent_404",
             "COALESCE(permanent_404,FALSE)=TRUE" if not SCOPE_ALL else "FALSE"),
            ("ats_apply_url is a jobright redirect (not direct)",
             "ats_apply_url ILIKE '%jobright%'"),
            ("seniority_level off-vocab (not intern|entry|mid|senior)",
             "seniority_level IS NOT NULL AND lower(seniority_level) NOT IN "
             "('intern','entry','mid','senior')"),
        ]
        for label, pred in checks:
            n = c.execute(
                f"SELECT count(*) n FROM jobs WHERE {SCOPE_SQL} AND ({pred})"
            ).fetchone()["n"]
            mark = "" if n == 0 else "  <-- FLAG"
            print(f"  {label:<52}{n:>7}{mark}")

        if SHOW_SAMPLES:
            print("\n=== SAMPLE job_ids per gap (up to 10) ===")
            for label, (pred, miss) in gaps.items():
                if miss == 0:
                    continue
                rows = c.execute(
                    f"""SELECT job_id, company_name, role_title, source_repo
                        FROM jobs WHERE {SCOPE_SQL} AND NOT ({pred})
                        ORDER BY first_seen_at DESC LIMIT 10"""
                ).fetchall()
                print(f"\n-- missing {label} ({miss}) --")
                for r in rows:
                    print(f"   {r['job_id'][:12]} {str(r['company_name'])[:24]:24} "
                          f"{str(r['role_title'])[:34]:34} [{r['source_repo']}]")

    print("\nQUALITY_REPORT_DONE")


if __name__ == "__main__":
    main()
