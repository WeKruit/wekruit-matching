"""Read-only data-integrity audit for the WeKruit matching corpus.

Runs a battery of integrity checks against the live Postgres DB and prints a
labelled report. NO writes — every statement is a SELECT. Each check is isolated
in its own try/except so one failing probe cannot abort the rest.

Usage: uv run python scripts/data_integrity_audit.py
"""
from wekruit_matching.db.connection import get_connection


def main() -> None:
    findings: list[tuple[str, str]] = []  # (severity, message)

    def rec(sev: str, msg: str) -> None:
        findings.append((sev, msg))

    with get_connection() as c:
        def scalar(sql, params=None):
            return c.execute(sql, params or {}).fetchone()

        # 1. status distribution -------------------------------------------------
        print("== [1] status distribution ==")
        try:
            for r in c.execute("SELECT status, count(*) n FROM jobs GROUP BY status ORDER BY 2 DESC").fetchall():
                print(f"   {r['status']:12} {r['n']}")
        except Exception as e:
            rec("ERR", f"status dist: {e!r}")

        # 2. embedding coverage of active ---------------------------------------
        print("== [2] embedding coverage (active) ==")
        try:
            r = scalar("""
                SELECT count(*) active,
                       count(*) FILTER (WHERE embedding IS NOT NULL) emb,
                       count(*) FILTER (WHERE embedding IS NULL AND enriched_at IS NOT NULL
                            AND job_description IS NOT NULL AND length(job_description)>=200
                            AND required_skills IS NOT NULL AND cardinality(required_skills)>0) embeddable_backlog
                FROM jobs WHERE status='active'
            """)
            cov = r["emb"]/r["active"] if r["active"] else 0
            print(f"   active={r['active']} embedded={r['emb']} cov={cov:.4f} embeddable_backlog={r['embeddable_backlog']}")
            if cov < 0.97:
                rec("WARN", f"embedded coverage {cov:.4f} < 0.97 (health-gate floor); backlog={r['embeddable_backlog']}")
        except Exception as e:
            rec("ERR", f"emb coverage: {e!r}")

        # 3. embedding model consistency ----------------------------------------
        print("== [3] embedding_model values (non-null embeddings) ==")
        try:
            rows = c.execute("""SELECT embedding_model, count(*) n FROM jobs
                                WHERE embedding IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""").fetchall()
            for r in rows:
                print(f"   {str(r['embedding_model']):28} {r['n']}")
            models = {r["embedding_model"] for r in rows if r["embedding_model"] not in (None, "")}
            if len(models) > 1:
                rec("CRIT", f"MIXED embedding models in one column: {sorted(models)} — vectors incomparable")
        except Exception as e:
            rec("ERR", f"emb model: {e!r}")

        # 4. embedding dimension sanity (pgvector) ------------------------------
        print("== [4] embedding dimensions ==")
        try:
            rows = c.execute("""SELECT vector_dims(embedding) d, count(*) n FROM jobs
                                WHERE embedding IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""").fetchall()
            for r in rows:
                print(f"   dim={r['d']} n={r['n']}")
            bad = [r for r in rows if r["d"] != 1536]
            if bad:
                rec("CRIT", f"non-1536-dim vectors present: {[(r['d'], r['n']) for r in bad]}")
        except Exception as e:
            rec("INFO", f"vector_dims unavailable (skipped): {e!r}")

        # 5. embed/enrich state-machine invariants ------------------------------
        print("== [5] state-machine invariants ==")
        try:
            r = scalar("""
                SELECT
                  count(*) FILTER (WHERE embedded_at IS NOT NULL AND embedding IS NULL) emb_at_no_vec,
                  count(*) FILTER (WHERE embedding IS NOT NULL AND embedded_at IS NULL) vec_no_emb_at,
                  count(*) FILTER (WHERE embedding IS NOT NULL AND enriched_at IS NULL) emb_no_enrich,
                  count(*) FILTER (WHERE embedding IS NOT NULL AND embedding_model IS NULL) vec_no_model
                FROM jobs
            """)
            print(f"   embedded_at&&!embedding={r['emb_at_no_vec']}  embedding&&!embedded_at={r['vec_no_emb_at']}")
            print(f"   embedding&&!enriched_at={r['emb_no_enrich']}  embedding&&!model={r['vec_no_model']}")
            for k, v, sev in [("embedded_at set but embedding NULL", r["emb_at_no_vec"], "WARN"),
                              ("embedding set but embedded_at NULL", r["vec_no_emb_at"], "WARN"),
                              ("embedding set but enriched_at NULL", r["emb_no_enrich"], "WARN"),
                              ("embedding set but embedding_model NULL", r["vec_no_model"], "WARN")]:
                if v:
                    rec(sev, f"{k}: {v} rows")
        except Exception as e:
            rec("ERR", f"state machine: {e!r}")

        # 6. zombie active: enriched but no usable signal -----------------------
        print("== [6] zombie active (enriched, no signal) ==")
        try:
            r = scalar("""
                SELECT
                  count(*) FILTER (WHERE enriched_at IS NOT NULL AND (job_description IS NULL OR length(job_description)<200)) thin_jd,
                  count(*) FILTER (WHERE enriched_at IS NOT NULL AND (required_skills IS NULL OR cardinality(required_skills)=0)) no_skills,
                  count(*) FILTER (WHERE embedding IS NOT NULL AND (job_description IS NULL OR length(job_description)<200)) embedded_thin
                FROM jobs WHERE status='active'
            """)
            print(f"   enriched_thin_jd={r['thin_jd']} enriched_no_skills={r['no_skills']} EMBEDDED_thin_jd={r['embedded_thin']}")
            if r["embedded_thin"]:
                rec("WARN", f"{r['embedded_thin']} active EMBEDDED jobs have thin/NULL JD — title-only vectors in matcher")
        except Exception as e:
            rec("ERR", f"zombie: {e!r}")

        # 7. field coverage (active) --------------------------------------------
        print("== [7] field coverage (active) ==")
        try:
            r = scalar("""
                SELECT count(*) a,
                  count(*) FILTER (WHERE sponsorship IS NULL) sp_null,
                  count(*) FILTER (WHERE seniority_level IS NULL) sen_null,
                  count(*) FILTER (WHERE industry IS NULL) ind_null,
                  count(*) FILTER (WHERE company_size IS NULL) cs_null,
                  count(*) FILTER (WHERE primary_url IS NULL OR primary_url='') url_null,
                  count(*) FILTER (WHERE job_description IS NULL) jd_null
                FROM jobs WHERE status='active'
            """)
            a = r["a"] or 1
            print(f"   active={r['a']}")
            print(f"   sponsorship_NULL={r['sp_null']} ({r['sp_null']/a:.1%})  seniority_NULL={r['sen_null']} ({r['sen_null']/a:.1%})")
            print(f"   industry_NULL={r['ind_null']} ({r['ind_null']/a:.1%})  company_size_NULL={r['cs_null']} ({r['cs_null']/a:.1%})")
            print(f"   primary_url_NULL={r['url_null']}  job_description_NULL={r['jd_null']} ({r['jd_null']/a:.1%})")
            if r["sen_null"]/a > 0.20:
                rec("WARN", f"seniority_level NULL on {r['sen_null']/a:.1%} of active (W2 backfill target)")
            if r["url_null"]:
                rec("WARN", f"{r['url_null']} active jobs have NULL primary_url (unclickable in matcher)")
        except Exception as e:
            rec("ERR", f"field coverage: {e!r}")

        # 8. first_seen integrity (W1) ------------------------------------------
        print("== [8] first_seen_at integrity (W1) ==")
        try:
            from wekruit_matching.scraper.upsert import check_first_seen_integrity
            offenders = check_first_seen_integrity(c)
            print(f"   first_seen offenders={offenders}")
            if offenders:
                rec("WARN", f"{offenders} active rows have first_seen_at newer than an older sibling — recency signal degraded; backfill_first_seen() fixes")
        except Exception as e:
            rec("ERR", f"first_seen: {e!r}")

        # 9. duplicates ----------------------------------------------------------
        print("== [9] duplicate active jobs ==")
        try:
            r = scalar("""
                SELECT
                  (SELECT count(*) FROM (SELECT 1 FROM jobs WHERE status='active'
                       GROUP BY company_name, role_title HAVING count(*)>1) x) ct_groups,
                  (SELECT COALESCE(sum(c-1),0) FROM (SELECT count(*) c FROM jobs WHERE status='active'
                       AND primary_url IS NOT NULL AND primary_url<>''
                       GROUP BY company_name, role_title, primary_url HAVING count(*)>1) y) true_dup_rows
            """)
            print(f"   (company,title) dup_groups={r['ct_groups']}  TRUE_dup_rows(company,title,primary_url)={r['true_dup_rows']}")
            if r["true_dup_rows"]:
                rec("INFO", f"{r['true_dup_rows']} exact-duplicate active rows (same company+title+primary_url)")
        except Exception as e:
            rec("ERR", f"dups: {e!r}")

        # 10. dead / hygiene consistency ----------------------------------------
        print("== [10] dead/hygiene consistency ==")
        try:
            r = scalar("""
                SELECT
                  count(*) FILTER (WHERE status='active' AND dead IS TRUE) active_dead,
                  count(*) FILTER (WHERE status='active' AND permanent_404 IS TRUE) active_404,
                  count(*) FILTER (WHERE status='active' AND hygiene_flipped IS TRUE) active_hygiene
                FROM jobs
            """)
            print(f"   active&dead={r['active_dead']}  active&permanent_404={r['active_404']}  active&hygiene_flipped={r['active_hygiene']}")
            if r["active_dead"]:
                rec("WARN", f"{r['active_dead']} active jobs flagged dead=true — should be inactive (dead URL in matcher)")
            if r["active_404"]:
                rec("WARN", f"{r['active_404']} active jobs flagged permanent_404 — dead apply link in matcher")
        except Exception as e:
            rec("ERR", f"dead/hygiene: {e!r}")

        # 11. feedback integrity -------------------------------------------------
        print("== [11] feedback integrity ==")
        try:
            r = scalar("""
                SELECT
                  (SELECT count(*) FROM feedback) total,
                  (SELECT count(*) FROM feedback f LEFT JOIN user_profiles u USING(user_id) WHERE u.user_id IS NULL) orphan_user,
                  (SELECT count(*) FROM feedback f LEFT JOIN jobs j USING(job_id) WHERE j.job_id IS NULL) orphan_job,
                  (SELECT COALESCE(sum(c-1),0) FROM (SELECT count(*) c FROM feedback GROUP BY user_id, job_id HAVING count(*)>1) z) dup_pairs
            """)
            print(f"   total={r['total']} orphan_user={r['orphan_user']} orphan_job={r['orphan_job']} dup_(user,job)_pairs={r['dup_pairs']}")
            if r["orphan_job"]:
                rec("WARN", f"{r['orphan_job']} feedback rows reference a missing job_id")
            if r["dup_pairs"]:
                rec("CRIT", f"{r['dup_pairs']} duplicate (user,job) feedback pairs despite unique constraint")
        except Exception as e:
            rec("ERR", f"feedback: {e!r}")

        # 12. user_profiles / affinity ------------------------------------------
        print("== [12] user_profiles affinity ==")
        try:
            r = scalar("""
                SELECT count(*) total,
                  count(*) FILTER (WHERE affinity_embedding IS NOT NULL) with_aff
                FROM user_profiles
            """)
            print(f"   profiles={r['total']} with_affinity={r['with_aff']}")
            try:
                dims = c.execute("""SELECT DISTINCT vector_dims(affinity_embedding) d FROM user_profiles
                                    WHERE affinity_embedding IS NOT NULL""").fetchall()
                ds = [x["d"] for x in dims]
                print(f"   affinity dims={ds}")
                if any(d != 1536 for d in ds):
                    rec("CRIT", f"affinity_embedding non-1536 dims: {ds}")
            except Exception:
                pass
        except Exception as e:
            rec("ERR", f"profiles: {e!r}")

        # 13. pipeline_sync_state (W4 watermark) --------------------------------
        print("== [13] pipeline_sync_state (W4 watermark) ==")
        try:
            cols = [r["column_name"] for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='pipeline_sync_state' ORDER BY ordinal_position").fetchall()]
            print(f"   columns: {cols}")
            for r in c.execute("SELECT * FROM pipeline_sync_state").fetchall():
                print(f"   row: {dict(r)}")
        except Exception as e:
            rec("ERR", f"sync_state: {e!r}")

    # summary -------------------------------------------------------------------
    print("\n== SUMMARY (findings) ==")
    order = {"CRIT": 0, "WARN": 1, "INFO": 2, "ERR": 3}
    for sev, msg in sorted(findings, key=lambda x: order.get(x[0], 9)):
        print(f"   [{sev}] {msg}")
    crit = sum(1 for s, _ in findings if s == "CRIT")
    warn = sum(1 for s, _ in findings if s == "WARN")
    err = sum(1 for s, _ in findings if s == "ERR")
    print(f"\n   TOT:  CRIT={crit}  WARN={warn}  ERR={err}  INFO={sum(1 for s,_ in findings if s=='INFO')}")
    print("AUDIT_COMPLETE")


if __name__ == "__main__":
    main()
