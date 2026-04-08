"""
Resolve 200 unresolved jobs using Serper.dev Google search.
Outputs: url-resolution-validation.csv

Resolution chain:
1. Slug registry → ATS API (Greenhouse/Lever/Ashby) — free
2. Serper.dev Google search — free (2,500/mo)

Cost: $0
"""

import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

from wekruit_matching.db.connection import get_connection
from wekruit_matching.scraper.slug_registry import SlugRegistry

# ─── Config ───────────────────────────────────────────
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "dd073459f57bbcf0d44f05235603a01aac02ece7")
SAMPLE_SIZE = 200
TITLE_MATCH_THRESHOLD = 0.35
OUTPUT_CSV = Path(__file__).parent.parent / "url-resolution-validation.csv"


def title_tokens(title: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "in", "at", "for", "of", "to", "-", "–", "|", "/", "&"}
    tokens = set(re.split(r"[\s\-–/|,()]+", title.lower().strip()))
    return tokens - stop - {""}


def title_match(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def serper_search(query: str) -> list[dict]:
    """Search Google via Serper.dev API."""
    try:
        resp = httpx.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": 5},
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Serper {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        return data.get("organic", [])
    except Exception as e:
        logger.warning(f"Serper error: {e}")
        return []


def is_real_apply_url(url: str) -> bool:
    """Check if URL looks like a real employer job posting (not an aggregator)."""
    aggregators = ["jobright.ai", "simplify.jobs", "linkedin.com/jobs", "indeed.com",
                   "glassdoor.com", "ziprecruiter.com", "monster.com", "dice.com",
                   "wellfound.com", "builtin.com", "ycombinator.com/companies"]
    return not any(agg in url.lower() for agg in aggregators)


def resolve_via_serper(company: str, title: str) -> tuple[str | None, str]:
    """Use Serper Google search to find the real job posting URL."""
    # Strategy 1: Search for exact title + company + "careers" or "jobs"
    query = f'"{title}" "{company}" careers apply'
    results = serper_search(query)

    for r in results:
        url = r.get("link", "")
        result_title = r.get("title", "")

        # Skip aggregator results
        if not is_real_apply_url(url):
            continue

        # Check if the result title matches our job
        if title_match(title, result_title) >= TITLE_MATCH_THRESHOLD:
            return url, "serper_exact"

        # Also accept if company name is in the URL and it looks like a job page
        if company.lower().replace(" ", "") in url.lower().replace("-", "").replace(".", "") and \
           any(p in url.lower() for p in ["/jobs/", "/careers/", "/positions/", "/job/", "/jr", "/posting"]):
            return url, "serper_company_match"

    # Strategy 2: Broader search without quotes
    query2 = f'{title} {company} apply careers'
    results2 = serper_search(query2)

    for r in results2:
        url = r.get("link", "")
        result_title = r.get("title", "")
        if not is_real_apply_url(url):
            continue
        if title_match(title, result_title) >= TITLE_MATCH_THRESHOLD:
            return url, "serper_broad"

    return None, "unresolved"


def resolve_via_ats_api(company: str, title: str, registry: SlugRegistry) -> tuple[str | None, str]:
    """Try slug registry → ATS API → title match."""
    slugs = registry.lookup_all_ats(company)
    if not slugs:
        return None, "no_slug"

    for ats, slug in slugs.items():
        try:
            if ats == "greenhouse":
                resp = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10)
                if resp.status_code == 200:
                    for j in resp.json().get("jobs", []):
                        if title_match(title, j.get("title", "")) >= TITLE_MATCH_THRESHOLD:
                            url = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j['id']}"
                            return url, "greenhouse_api"
            elif ats == "lever":
                resp = httpx.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=10)
                if resp.status_code == 200:
                    for j in resp.json():
                        if title_match(title, j.get("text", "")) >= TITLE_MATCH_THRESHOLD:
                            return j.get("hostedUrl") or j.get("applyUrl", ""), "lever_api"
            elif ats == "ashby":
                resp = httpx.post(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                                  json={"includeCompensation": True}, timeout=10)
                if resp.status_code == 200:
                    for j in resp.json().get("jobs", []):
                        if title_match(title, j.get("title", "")) >= TITLE_MATCH_THRESHOLD:
                            return j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id','')}", "ashby_api"
            if ats in ("greenhouse", "lever", "ashby"):
                break  # Only try first matching ATS
        except Exception:
            continue

    return None, "no_ats_match"


def main():
    logger.info(f"=== {SAMPLE_SIZE} Job URL Resolution Test (Serper.dev) ===")

    # Verify Serper key
    test = serper_search("test")
    if not test:
        logger.error("Serper.dev API not working — check key")
        sys.exit(1)
    logger.info("Serper.dev: OK")

    registry = SlugRegistry()
    logger.info("Slug registry loaded")

    # Pull unresolved jobs
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT job_id, company_name, role_title, primary_url, ats_apply_url
            FROM jobs
            WHERE status = 'active'
              AND ats_apply_url IS NULL
            ORDER BY first_seen_at DESC
            LIMIT %s
        """, (SAMPLE_SIZE,)).fetchall()

    logger.info(f"Loaded {len(rows)} unresolved jobs")

    # Group by company
    by_company: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_company[row["company_name"].lower()].append(dict(row))
    logger.info(f"Unique companies: {len(by_company)}")

    results = []
    stats = defaultdict(int)
    serper_calls = 0
    ats_calls = 0

    for i, (company_key, jobs) in enumerate(by_company.items()):
        company = jobs[0]["company_name"]

        if (i + 1) % 20 == 0:
            resolved_n = sum(1 for r in results if r["status"] == "resolved")
            logger.info(f"Progress: {i+1}/{len(by_company)} companies ({len(results)} jobs) | "
                        f"resolved: {resolved_n} | serper: {serper_calls} | ats: {ats_calls}")

        # Step 1: Try ATS API (free, no quota)
        slugs = registry.lookup_all_ats(company)
        ats_listings: dict[str, str] = {}  # title -> url
        if slugs:
            ats_calls += 1
            for ats, slug in slugs.items():
                try:
                    if ats == "greenhouse":
                        resp = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10)
                        if resp.status_code == 200:
                            for j in resp.json().get("jobs", []):
                                url = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j['id']}"
                                ats_listings[j.get("title", "")] = url
                    elif ats == "lever":
                        resp = httpx.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=10)
                        if resp.status_code == 200:
                            for j in resp.json():
                                ats_listings[j.get("text", "")] = j.get("hostedUrl") or j.get("applyUrl", "")
                    elif ats == "ashby":
                        resp = httpx.post(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                                          json={"includeCompensation": True}, timeout=10)
                        if resp.status_code == 200:
                            for j in resp.json().get("jobs", []):
                                ats_listings[j.get("title", "")] = j.get("jobUrl") or ""
                    if ats_listings:
                        break
                except Exception:
                    continue

        # Resolve each job
        for job in jobs:
            resolved_url = None
            resolution_method = "unresolved"

            # Try ATS listings first
            if ats_listings:
                best_score = 0.0
                for listing_title, listing_url in ats_listings.items():
                    score = title_match(job["role_title"], listing_title)
                    if score > best_score:
                        best_score = score
                        if score >= TITLE_MATCH_THRESHOLD:
                            resolved_url = listing_url
                            resolution_method = "ats_api"

            # Serper fallback (1 call per job, not per company — titles differ)
            if not resolved_url:
                serper_calls += 1
                resolved_url, resolution_method = resolve_via_serper(company, job["role_title"])
                time.sleep(0.3)  # Gentle rate limit

            stats[resolution_method] += 1
            results.append({
                "job_id": job["job_id"][:12],
                "company_name": company,
                "role_title": job["role_title"],
                "primary_url": job["primary_url"],
                "ats_apply_url": resolved_url or "",
                "resolution_method": resolution_method,
                "status": "resolved" if resolved_url else "unresolved",
            })

    # Write CSV
    logger.info(f"Writing {len(results)} rows to {OUTPUT_CSV}")
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "job_id", "company_name", "role_title", "primary_url",
            "ats_apply_url", "resolution_method", "status"
        ])
        writer.writeheader()
        writer.writerows(results)

    # Summary
    total = len(results)
    resolved = total - stats.get("unresolved", 0)

    logger.info("=" * 60)
    logger.info(f"RESULTS: {resolved}/{total} resolved ({resolved*100//max(total,1)}%)")
    for method, count in sorted(stats.items(), key=lambda x: -x[1]):
        logger.info(f"  {method}: {count}")
    logger.info("")
    logger.info(f"COST:")
    logger.info(f"  Serper calls: {serper_calls} (free tier: 2500/mo)")
    logger.info(f"  ATS API calls: {ats_calls} (free)")
    logger.info(f"  TOTAL: $0.00")
    logger.info(f"  Remaining Serper quota: ~{2500 - serper_calls}")
    logger.info(f"")
    logger.info(f"CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
