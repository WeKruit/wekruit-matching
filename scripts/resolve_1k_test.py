"""
Resolve 1K unresolved jobs to real employer apply URLs.
Outputs: url-resolution-validation.csv

Resolution chain:
1. Slug registry → ATS API (Greenhouse/Lever/Ashby)
2. Firecrawl careers page scrape → title match
3. (Serper fallback — skipped if no SERPER_API_KEY)

Cost: $0 (Firecrawl self-hosted, ATS APIs free)
"""

import csv
import hashlib
import json
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
from wekruit_matching.scraper.url_classifier import classify, ATSTier
from wekruit_matching.scraper.slug_registry import SlugRegistry

# ─── Config ───────────────────────────────────────────
FIRECRAWL_URL = "http://localhost:3002"
FIRECRAWL_KEY = "wekruit-local"
SAMPLE_SIZE = 1000
TITLE_MATCH_THRESHOLD = 0.4
OUTPUT_CSV = Path(__file__).parent.parent / "url-resolution-validation.csv"

# ─── Helpers ──────────────────────────────────────────

def title_tokens(title: str) -> set[str]:
    """Normalize and tokenize a job title for matching."""
    stop = {"the", "a", "an", "and", "or", "in", "at", "for", "of", "to", "-", "–", "|", "/"}
    tokens = set(re.split(r"[\s\-–/|,()]+", title.lower().strip()))
    return tokens - stop - {""}


def title_match(a: str, b: str) -> float:
    """Jaccard similarity between two job titles."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def guess_careers_urls(company_name: str, company_url: str | None) -> list[str]:
    """Generate candidate careers page URLs from company name/URL."""
    urls = []

    if company_url:
        # Clean the URL
        base = company_url.rstrip("/")
        if not base.startswith("http"):
            base = f"https://{base}"
        urls.extend([
            f"{base}/careers",
            f"{base}/jobs",
            f"{base}/careers/jobs",
            f"{base}/en/jobs",
        ])
        # Try careers subdomain
        from urllib.parse import urlparse
        parsed = urlparse(base)
        domain = parsed.hostname or ""
        if domain.startswith("www."):
            domain = domain[4:]
        urls.append(f"https://careers.{domain}")
        urls.append(f"https://careers.{domain}/en/jobs")

    # Fallback: guess from company name
    slug = re.sub(r"[^a-z0-9]", "", company_name.lower())
    urls.extend([
        f"https://careers.{slug}.com",
        f"https://{slug}.com/careers",
        f"https://www.{slug}.com/careers",
    ])

    # Dedupe preserving order
    seen = set()
    unique = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def fetch_jobright_company_url(jobright_url: str) -> str | None:
    """Fetch companyURL from a JobRight page's __NEXT_DATA__."""
    try:
        resp = httpx.get(jobright_url.split("?")[0], follow_redirects=True, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text)
        if not match:
            return None
        data = json.loads(match.group(1))
        company = data.get("props", {}).get("pageProps", {}).get("dataSource", {}).get("companyResult", {})
        return company.get("companyURL") or None
    except Exception:
        return None


def firecrawl_scrape(url: str) -> str | None:
    """Scrape a URL via Firecrawl, return markdown or None."""
    try:
        resp = httpx.post(
            f"{FIRECRAWL_URL}/v1/scrape",
            json={"url": url, "formats": ["markdown"]},
            headers={"Authorization": f"Bearer {FIRECRAWL_KEY}"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("success"):
            return data.get("data", {}).get("markdown", "")
        return None
    except Exception:
        return None


def extract_job_links(markdown: str, base_url: str) -> list[tuple[str, str]]:
    """Extract (title, url) pairs from careers page markdown."""
    results = []
    # Match markdown links: [Title](url)
    for match in re.finditer(r'\[([^\]]+)\]\((https?://[^\)]+)\)', markdown):
        link_title = match.group(1).strip()
        link_url = match.group(2).strip()
        # Filter: must look like a job link (has /jobs/ or /positions/ or /jr or similar)
        if any(p in link_url.lower() for p in ["/jobs/", "/positions/", "/job/", "/jr", "/posting", "/opening"]):
            if len(link_title) > 5 and len(link_title) < 200:
                results.append((link_title, link_url))

    # Also match relative links converted by Firecrawl
    from urllib.parse import urlparse, urljoin
    for match in re.finditer(r'\[([^\]]+)\]\((/[^\)]+)\)', markdown):
        link_title = match.group(1).strip()
        link_path = match.group(2).strip()
        if any(p in link_path.lower() for p in ["/jobs/", "/positions/", "/job/", "/jr", "/posting"]):
            if len(link_title) > 5 and len(link_title) < 200:
                full_url = urljoin(base_url, link_path)
                results.append((link_title, full_url))

    return results


def resolve_via_ats_api(company: str, title: str, registry: SlugRegistry) -> tuple[str | None, str]:
    """Try slug registry → ATS API → title match."""
    slugs = registry.lookup_all_ats(company)
    if not slugs:
        return None, "no_slug"

    for ats, slug in slugs.items():
        try:
            if ats == "greenhouse":
                resp = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=10)
                if resp.status_code == 200:
                    for job in resp.json().get("jobs", []):
                        if title_match(title, job.get("title", "")) >= TITLE_MATCH_THRESHOLD:
                            url = job.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{job['id']}"
                            return url, "greenhouse_api"
            elif ats == "lever":
                resp = httpx.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=10)
                if resp.status_code == 200:
                    for job in resp.json():
                        if title_match(title, job.get("text", "")) >= TITLE_MATCH_THRESHOLD:
                            url = job.get("hostedUrl") or job.get("applyUrl", "")
                            return url, "lever_api"
            elif ats == "ashby":
                resp = httpx.post(
                    f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                    json={"includeCompensation": True},
                    timeout=10,
                )
                if resp.status_code == 200:
                    for job in resp.json().get("jobs", []):
                        if title_match(title, job.get("title", "")) >= TITLE_MATCH_THRESHOLD:
                            url = job.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{job.get('id', '')}"
                            return url, "ashby_api"
        except Exception:
            continue

    return None, "no_title_match"


def resolve_via_careers_page(company: str, title: str, company_url: str | None) -> tuple[str | None, str]:
    """Try Firecrawl scrape on careers page → title match."""
    candidate_urls = guess_careers_urls(company, company_url)

    for careers_url in candidate_urls[:4]:  # Try up to 4 patterns
        markdown = firecrawl_scrape(careers_url)
        if not markdown or len(markdown) < 200:
            continue

        links = extract_job_links(markdown, careers_url)
        if not links:
            continue

        # Title match
        best_score = 0.0
        best_url = None
        for link_title, link_url in links:
            score = title_match(title, link_title)
            if score > best_score:
                best_score = score
                best_url = link_url

        if best_score >= TITLE_MATCH_THRESHOLD and best_url:
            return best_url, "firecrawl_careers"

    return None, "no_careers_match"


# ─── Main ─────────────────────────────────────────────

def main():
    logger.info("=== 1K URL Resolution Test ===")
    registry = SlugRegistry()
    logger.info(f"Slug registry loaded")

    # Verify Firecrawl is up
    try:
        r = httpx.get(f"{FIRECRAWL_URL}/", timeout=5)
        logger.info(f"Firecrawl: OK (status {r.status_code})")
    except Exception as e:
        logger.error(f"Firecrawl not reachable at {FIRECRAWL_URL}: {e}")
        logger.error("Start Docker Desktop and Firecrawl first!")
        sys.exit(1)

    # Pull 1K unresolved jobs
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

    # Group by company for efficiency
    by_company: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_company[row["company_name"].lower()].append(dict(row))

    logger.info(f"Unique companies: {len(by_company)}")

    # ── Phase 1: Batch-fetch companyURL from JobRight (fast, 1 per company) ──
    logger.info("Phase 1: Fetching companyURL from JobRight pages...")
    company_urls: dict[str, str] = {}
    fetched = 0
    for i, (company_key, jobs) in enumerate(by_company.items()):
        jobright_job = next((j for j in jobs if "jobright" in j["primary_url"]), None)
        if jobright_job:
            url = fetch_jobright_company_url(jobright_job["primary_url"])
            if url:
                company_urls[company_key] = url
            fetched += 1
            if fetched % 50 == 0:
                logger.info(f"  Fetched {fetched}/{len(by_company)} | found URLs: {len(company_urls)}")
            time.sleep(0.2)

    logger.info(f"Phase 1 done: {len(company_urls)}/{len(by_company)} companies have companyURL")

    # ── Phase 2: Resolve — ATS API first, then Firecrawl on real careers pages ──
    logger.info("Phase 2: Resolving URLs...")
    results = []
    stats = defaultdict(int)
    firecrawl_calls = 0
    ats_api_calls = 0
    # Cache company-level job listings
    company_listings_cache: dict[str, tuple[list[tuple[str, str]], str]] = {}

    for i, (company_key, jobs) in enumerate(by_company.items()):
        company = jobs[0]["company_name"]

        if (i + 1) % 50 == 0:
            resolved_n = sum(1 for r in results if r["status"] == "resolved")
            logger.info(f"  {i+1}/{len(by_company)} companies ({len(results)} jobs) | resolved: {resolved_n} | fc: {firecrawl_calls} | ats: {ats_api_calls}")

        # Check cache first
        if company_key in company_listings_cache:
            available, source = company_listings_cache[company_key]
        else:
            available = []
            source = ""

            # Step 1: Try slug registry → ATS API
            slugs = registry.lookup_all_ats(company)
            if slugs:
                for ats, slug in slugs.items():
                    ats_api_calls += 1
                    try:
                        if ats == "greenhouse":
                            resp = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10)
                            if resp.status_code == 200:
                                for j in resp.json().get("jobs", []):
                                    url = j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j['id']}"
                                    available.append((j.get("title", ""), url))
                        elif ats == "lever":
                            resp = httpx.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=10)
                            if resp.status_code == 200:
                                for j in resp.json():
                                    url = j.get("hostedUrl") or j.get("applyUrl", "")
                                    available.append((j.get("text", ""), url))
                        elif ats == "ashby":
                            resp = httpx.post(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                                              json={"includeCompensation": True}, timeout=10)
                            if resp.status_code == 200:
                                for j in resp.json().get("jobs", []):
                                    url = j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id', '')}"
                                    available.append((j.get("title", ""), url))
                        if available:
                            source = f"{ats}_api"
                            break
                    except Exception:
                        continue

            # Step 2: Firecrawl on real company careers page
            if not available and company_key in company_urls:
                company_url = company_urls[company_key]
                candidate_urls = guess_careers_urls(company, company_url)
                for careers_url in candidate_urls[:3]:
                    firecrawl_calls += 1
                    markdown = firecrawl_scrape(careers_url)
                    if markdown and len(markdown) > 500:
                        links = extract_job_links(markdown, careers_url)
                        if links:
                            available = links
                            source = "firecrawl_careers"
                            break

            company_listings_cache[company_key] = (available, source)

        # Match each job against available listings
        comp_url = company_urls.get(company_key, "")
        for job in jobs:
            resolved_url = None
            resolution_method = "unresolved"

            if available:
                best_score = 0.0
                best_url = None
                for listing_title, listing_url in available:
                    score = title_match(job["role_title"], listing_title)
                    if score > best_score:
                        best_score = score
                        best_url = listing_url
                if best_score >= TITLE_MATCH_THRESHOLD and best_url:
                    resolved_url = best_url
                    resolution_method = source

            stats[resolution_method] += 1
            results.append({
                "job_id": job["job_id"][:12],
                "company_name": company,
                "role_title": job["role_title"],
                "primary_url": job["primary_url"],
                "company_url": comp_url,
                "ats_apply_url": resolved_url or "",
                "resolution_method": resolution_method,
                "status": "resolved" if resolved_url else "unresolved",
            })

    # Write CSV
    logger.info(f"Writing {len(results)} rows to {OUTPUT_CSV}")
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "job_id", "company_name", "role_title", "primary_url",
            "company_url", "ats_apply_url", "resolution_method", "status"
        ])
        writer.writeheader()
        writer.writerows(results)

    # Summary
    total = len(results)
    resolved = total - stats["unresolved"]

    logger.info("=" * 60)
    logger.info(f"RESULTS: {resolved}/{total} resolved ({resolved*100//total}%)")
    logger.info(f"  slug_registry/ATS API: {stats.get('greenhouse_api', 0) + stats.get('lever_api', 0) + stats.get('ashby_api', 0)}")
    logger.info(f"  firecrawl_careers:     {stats.get('firecrawl_careers', 0)}")
    logger.info(f"  unresolved:            {stats.get('unresolved', 0)}")
    logger.info(f"")
    logger.info(f"COST:")
    logger.info(f"  Firecrawl calls: {firecrawl_calls} (self-hosted = $0)")
    logger.info(f"  ATS API calls:   {ats_api_calls} (free public APIs = $0)")
    logger.info(f"  Serper calls:    0 (not configured)")
    logger.info(f"  TOTAL COST:      $0.00")
    logger.info(f"")
    logger.info(f"CSV: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
