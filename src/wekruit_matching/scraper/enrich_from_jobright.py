"""Full JobRight enrichment pipeline — v2.

Flow:
  1. Purge stale jobs older than 20 days (sliding window via last_seen_at)
  2. For ALL active jobs with a jobright.ai URL that lack skills,
     visit the link and parse structured data from __NEXT_DATA__
  3. Extract: skills (jdCoreSkillV2 first, regex fallback), industry,
     sponsorship, salary, seniority, responsibilities, qualifications
  4. Store EVERYTHING in the DB — JD text, skills, salary, benefits, etc.
  5. No LLM, $0 cost

Covers ALL industries — tech, business, finance, marketing, design, etc.
No jobs are filtered or deactivated by title.

Run: uv run python -m wekruit_matching.scraper.enrich_from_jobright
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
from loguru import logger

from wekruit_matching.scraper.jobright import _extract_skills_from_qualifications, _map_industry
from wekruit_matching.scraper.skill_normalize import normalize_skills

# 20-day sliding window
MAX_AGE_DAYS = 20

WORK_MODEL_MAP = {
    "On Site": "onsite",
    "Onsite": "onsite",
    "Remote": "remote",
    "Hybrid": "hybrid",
}


def _clean_html(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    clean = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", clean).strip()


def _fetch_job_detail(job_url: str) -> dict | None:
    """Fetch structured job data from a jobright.ai job page.

    Returns a rich dict with all available fields from __NEXT_DATA__,
    using jdCoreSkillV2 as the primary skill source.
    """
    try:
        resp = httpx.get(
            job_url,
            timeout=12,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (WeKruit-Matching/0.2)"},
        )
        if resp.status_code != 200:
            return None

        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text,
        )
        if not match:
            return None

        data = json.loads(match.group(1))
        ds = data.get("props", {}).get("pageProps", {}).get("dataSource", {})
        jr = ds.get("jobResult", {})
        cr = ds.get("companyResult", {})

        if not jr:
            return None

        # ── Primary skill source: jdCoreSkillV2 (structured, AI-parsed) ──
        core_skills_v2 = jr.get("jdCoreSkillV2", [])
        skills_from_v2 = []
        for s in core_skills_v2:
            if isinstance(s, dict) and s.get("skill"):
                skills_from_v2.append(s["skill"])

        # ── Fallback: jdCoreSkills (older format) ──
        if not skills_from_v2:
            core_skills_v1 = jr.get("jdCoreSkills", [])
            for s in core_skills_v1:
                if isinstance(s, dict) and s.get("skill"):
                    skills_from_v2.append(s["skill"])

        # ── Fallback: regex on skillSummaries + jobSummary ──
        if not skills_from_v2:
            text_parts = []
            for skill_text in jr.get("skillSummaries", []):
                if isinstance(skill_text, str):
                    text_parts.append(skill_text)
            if jr.get("jobSummary"):
                text_parts.append(jr["jobSummary"])
            combined = " ".join(text_parts)
            skills_from_v2 = _extract_skills_from_qualifications(combined)

        # Normalize all skills to our vocabulary
        normalized_skills = normalize_skills(skills_from_v2) if skills_from_v2 else []

        # ── Build full JD text ──
        jd_parts = []
        if jr.get("jobSummary"):
            jd_parts.append(_clean_html(jr["jobSummary"]))
        jd_text = " ".join(jd_parts)

        # ── Responsibilities ──
        responsibilities = []
        for r in jr.get("coreResponsibilities", []):
            if isinstance(r, str):
                responsibilities.append(_clean_html(r))

        # ── Qualifications ──
        qualifications = []
        for q in jr.get("skillSummaries", []):
            if isinstance(q, str):
                qualifications.append(_clean_html(q))

        # ── Industry from company ──
        raw_industries = cr.get("industries") or cr.get("industry") or []
        if isinstance(raw_industries, str):
            raw_industries = [raw_industries]

        # ── Sponsorship from tags + direct field ──
        sponsorship = None
        if jr.get("isH1bSponsor") is True:
            sponsorship = True
        elif jr.get("isCitizenOnly") is True:
            sponsorship = False
        else:
            tags = jr.get("recommendationTags", [])
            if "No H1B" in tags:
                sponsorship = False
            elif any(t in tags for t in ("H1B Sponsor", "Visa Sponsor")):
                sponsorship = True

        # ── Salary ──
        salary = jr.get("salaryDesc", "") or ""

        # ── Seniority ──
        seniority = jr.get("jobSeniority", "") or ""

        # ── Benefits ──
        benefits = []
        for b in jr.get("benefitsSummaries", []):
            if isinstance(b, str):
                benefits.append(b)

        return {
            "skills": normalized_skills,
            "jd_text": jd_text,
            "responsibilities": responsibilities,
            "qualifications": qualifications,
            "industry_list": raw_industries,
            "seniority": seniority,
            "salary": salary,
            "benefits": benefits,
            "work_model": WORK_MODEL_MAP.get(jr.get("workModel", ""), None),
            "sponsorship": sponsorship,
        }

    except Exception as e:
        logger.debug("Failed to fetch {}: {}", job_url, e)
        return None


def purge_stale_jobs(conn: psycopg.Connection) -> int:
    """Mark jobs older than MAX_AGE_DAYS as inactive (sliding window).

    Uses last_seen_at (when we last confirmed the job exists in a scrape)
    as the staleness signal — not first_seen_at.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    result = conn.execute(
        "UPDATE jobs SET status = 'inactive' WHERE status = 'active' AND last_seen_at < %(cutoff)s",
        {"cutoff": cutoff},
    )
    count = result.rowcount
    conn.commit()
    logger.info("Purged {} stale jobs (last_seen_at older than {} days)", count, MAX_AGE_DAYS)
    return count


def enrich_all_jobs(
    conn: psycopg.Connection,
    max_workers: int = 8,
    batch_size: int = 50,
    force_reenrich: bool = False,
) -> dict[str, int]:
    """Enrich ALL active jobs with jobright.ai URLs.

    Uses jdCoreSkillV2 as primary skill source (JobRight's AI-parsed skills),
    falling back to regex on skillSummaries text.

    Stores full JD text, responsibilities, qualifications, salary, benefits.

    Args:
        force_reenrich: If True, re-enrich ALL jobs (even those with existing skills).
                       Use after code changes to re-extract with improved logic.
    """
    if force_reenrich:
        # Reset enriched_at so ALL jobs get re-processed
        reset = conn.execute(
            """
            UPDATE jobs SET required_skills = '{}', enriched_at = NULL
            WHERE status = 'active'
              AND primary_url LIKE 'https://jobright.ai/%%'
            """
        )
        conn.commit()
        logger.info("Force re-enrich: reset {} jobs", reset.rowcount)

    total_row = conn.execute(
        """
        SELECT COUNT(*) as c FROM jobs
        WHERE status = 'active'
          AND primary_url LIKE 'https://jobright.ai/%%'
          AND enriched_at IS NULL
          AND (required_skills IS NULL OR required_skills = '{}')
        """
    ).fetchone()
    total_needing = total_row["c"]
    logger.info("Total jobs needing JD enrichment: {}", total_needing)

    enriched_total = 0
    failed_total = 0
    skills_found = 0

    while True:
        rows = conn.execute(
            """
            SELECT job_id, primary_url, role_title
            FROM jobs
            WHERE status = 'active'
              AND primary_url LIKE 'https://jobright.ai/%%'
              AND enriched_at IS NULL
              AND (required_skills IS NULL OR required_skills = '{}')
            LIMIT %(limit)s
            """,
            {"limit": batch_size},
        ).fetchall()

        if not rows:
            break

        logger.info(
            "Batch: fetching {} jobs ({}/{} done, {} with skills)",
            len(rows), enriched_total, total_needing, skills_found,
        )

        # Parallel fetch
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_fetch_job_detail, r["primary_url"]): r for r in rows}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    detail = future.result()
                except Exception as e:
                    logger.warning("Exception fetching {}: {}", row["primary_url"], e)
                    detail = None

                if not detail:
                    failed_total += 1
                    results.append({
                        "job_id": row["job_id"],
                        "skills": [],
                        "failed": True,
                    })
                    continue

                results.append({
                    "job_id": row["job_id"],
                    "skills": detail["skills"],
                    "jd_text": detail.get("jd_text", ""),
                    "responsibilities": detail.get("responsibilities", []),
                    "qualifications": detail.get("qualifications", []),
                    "industry_list": detail.get("industry_list", []),
                    "seniority": detail.get("seniority", ""),
                    "salary": detail.get("salary", ""),
                    "benefits": detail.get("benefits", []),
                    "sponsorship": detail.get("sponsorship"),
                    "work_model": detail.get("work_model"),
                    "failed": False,
                })

        # Update DB
        for r in results:
            set_parts = ["required_skills = %(skills)s", "enriched_at = NOW()"]
            params: dict = {"job_id": r["job_id"], "skills": r["skills"]}

            if not r["failed"]:
                # Strip NUL bytes — Postgres text columns reject \x00
                def _sanitize(val):
                    if isinstance(val, str):
                        return val.replace("\x00", "")
                    if isinstance(val, list):
                        return [_sanitize(v) for v in val]
                    return val

                # Store all rich data
                if r.get("jd_text"):
                    set_parts.append("job_description = %(jd_text)s")
                    params["jd_text"] = _sanitize(r["jd_text"])
                if r.get("responsibilities"):
                    set_parts.append("core_responsibilities = %(responsibilities)s")
                    params["responsibilities"] = _sanitize(r["responsibilities"])
                if r.get("qualifications"):
                    set_parts.append("qualifications = %(qualifications)s")
                    params["qualifications"] = _sanitize(r["qualifications"])
                if r.get("salary"):
                    set_parts.append("salary_range = %(salary)s")
                    params["salary"] = _sanitize(r["salary"])
                if r.get("seniority"):
                    set_parts.append("seniority_level = %(seniority)s")
                    params["seniority"] = _sanitize(r["seniority"])
                if r.get("benefits"):
                    set_parts.append("benefits = %(benefits)s")
                    params["benefits"] = _sanitize(r["benefits"])

                industry = _map_industry(r.get("industry_list", []))
                if industry and industry != "other":
                    set_parts.append("industry = %(industry)s")
                    params["industry"] = industry
                if r.get("sponsorship") is not None:
                    set_parts.append("sponsorship = %(sponsorship)s")
                    params["sponsorship"] = r["sponsorship"]

            try:
                conn.execute(
                    f"UPDATE jobs SET {', '.join(set_parts)} WHERE job_id = %(job_id)s",
                    params,
                )
                if not r["failed"]:
                    enriched_total += 1
                    if r["skills"]:
                        skills_found += 1
            except Exception as exc:
                logger.warning("DB write failed for {}: {}", r["job_id"][:8], exc)
                conn.rollback()
                failed_total += 1
                continue

        conn.commit()
        logger.info(
            "Batch done: {} enriched, {} with skills, {} failed",
            enriched_total, skills_found, failed_total,
        )
        time.sleep(0.3)

    return {
        "enriched": enriched_total,
        "failed": failed_total,
        "skills_found": skills_found,
        "total_needed": total_needing,
    }


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    from wekruit_matching.config import get_settings
    conn = psycopg.connect(get_settings().database_url, row_factory=psycopg.rows.dict_row)

    # Check for --force flag
    force = "--force" in sys.argv

    # Step 1: Purge stale jobs (>20 day sliding window)
    logger.info("=== Step 1: Purge stale jobs ===")
    purged = purge_stale_jobs(conn)

    # Step 2: Enrich ALL active jobs
    logger.info("=== Step 2: Enrich all jobs from jobright.ai (v2: jdCoreSkillV2) ===")
    stats = enrich_all_jobs(conn, max_workers=8, batch_size=50, force_reenrich=force)
    logger.info("Stats: {}", stats)

    # Final
    total = conn.execute("SELECT COUNT(*) as c FROM jobs WHERE status='active'").fetchone()["c"]
    has_skills = conn.execute(
        "SELECT COUNT(*) as c FROM jobs WHERE status='active' AND required_skills IS NOT NULL AND required_skills != '{}'"
    ).fetchone()["c"]
    embedded = conn.execute("SELECT COUNT(embedding) as c FROM jobs WHERE status='active'").fetchone()["c"]

    logger.info(
        "=== Final: Active={} Skills={}({}%) Embedded={}({}%) ===",
        total, has_skills, round(has_skills * 100 / total, 1) if total else 0,
        embedded, round(embedded * 100 / total, 1) if total else 0,
    )
    conn.close()
