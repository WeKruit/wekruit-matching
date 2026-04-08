"""JobRight.ai scraper for intern-list.com and newgrad-jobs.com.

Fetches job listings from JobRight's Next.js SSR data (no API key needed).
Each page returns 50 jobs — the freshest in that category.

Data source: https://jobright.ai/minisites-jobs/{type}/{country}/{category}
Structured JSON extracted from __NEXT_DATA__ server-side props.

Fields per job:
  id, title, company, location, salary, postedDate, applyUrl,
  workModel, companySize, industry, qualifications, h1bSponsored
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import httpx
from loguru import logger

from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper.id_utils import (
    compute_content_hash,
    generate_job_id,
    normalize_company_name,
)

BASE_URL = "https://jobright.ai/minisites-jobs"

# Categories to scrape — focused on tech/business roles relevant to students
CATEGORIES = [
    "swe",
    "data_analysis",
    "ml_ai",
    "product_management",
    "engineering_development",
    "business_analyst",
    "cyber_security",
    "consulting",
    "marketing_gen",
    "accounting_finance",
]

JOB_TYPES = ["intern", "newgrad"]
COUNTRIES = ["us"]  # Start with US only; add "ca" later if needed

# Map JobRight work model to our format
WORK_MODEL_MAP = {
    "On Site": "onsite",
    "Remote": "remote",
    "Hybrid": "hybrid",
}

# Map JobRight company size to our enrichment vocabulary
COMPANY_SIZE_MAP = {
    "1-50": "startup",
    "51-200": "startup",
    "201-500": "midsize",
    "501-1000": "midsize",
    "1001-5000": "large",
    "5001-10000": "large",
    "10000+": "large",
}


def _fetch_page(job_type: str, country: str, category: str) -> list[dict]:
    """Fetch one page (50 jobs) from JobRight's Next.js SSR data."""
    url = f"{BASE_URL}/{job_type}/{country}/{category}?embed=true"
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={
            "User-Agent": "WeKruit-Matching/0.1 (job-aggregator)"
        })
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch {}: {}", url, e)
        return []

    # Extract __NEXT_DATA__ JSON from the HTML
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text,
    )
    if not match:
        logger.warning("No __NEXT_DATA__ found in response from {}", url)
        return []

    try:
        data = json.loads(match.group(1))
        jobs = data["props"]["pageProps"].get("initialJobs", [])
        total = data["props"]["pageProps"].get("initialTotal", 0)
        logger.debug(
            "Fetched {}/{} jobs from {}/{}/{}",
            len(jobs), total, job_type, country, category,
        )
        return jobs
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to parse __NEXT_DATA__ from {}: {}", url, e)
        return []


def _map_industry(raw_industries: list | None) -> str | None:
    """Map JobRight industry list to our controlled vocabulary."""
    if not raw_industries:
        return None

    # Map common JobRight industries to our vocabulary
    mapping = {
        "artificial intelligence (ai)": "ai_ml",
        "machine learning": "ai_ml",
        "software development": "tech",
        "information technology": "tech",
        "internet": "tech",
        "financial services": "fintech",
        "banking": "fintech",
        "insurance": "fintech",
        "e-commerce": "ecommerce",
        "retail": "ecommerce",
        "healthcare": "healthtech",
        "pharmaceutical": "healthtech",
        "biotechnology": "healthtech",
        "semiconductor": "hardware",
        "electronics": "hardware",
        "manufacturing": "hardware",
        "cyber security": "cybersecurity",
        "consulting": "consulting",
        "gaming": "gaming",
        "social media": "social_media",
        "saas": "enterprise_saas",
        "enterprise software": "enterprise_saas",
    }

    for raw in raw_industries:
        key = raw.lower().strip()
        if key in mapping:
            return mapping[key]

    return "other"


def _extract_skills_from_qualifications(quals: str | None) -> list[str]:
    """Extract skill keywords from qualifications text.

    Word-boundary regex matching — no LLM, catches common tech skills
    mentioned in job requirements without false positives from substrings.
    """
    if not quals:
        return []

    text = quals.lower()

    # Skills that need word-boundary matching to avoid false positives.
    # Format: (display_name, regex_pattern)
    # Short/ambiguous terms like "r", "go", "rest", "css" use stricter patterns.
    _SKILL_PATTERNS: list[tuple[str, str]] = [
        # Languages — safe (long enough)
        ("python", r"\bpython\b"),
        ("java", r"\bjava\b(?!script)"),
        ("javascript", r"\bjavascript\b"),
        ("typescript", r"\btypescript\b"),
        ("c++", r"\bc\+\+\b"),
        ("c#", r"\bc#\b"),
        ("go", r"\b(?:golang|go\s+(?:lang|programming|developer|engineer))\b"),
        ("rust", r"\brust\b(?:\s+(?:lang|programming)|\b)"),
        ("ruby", r"\bruby\b"),
        ("swift", r"\bswift\b(?:\s+(?:programming|ios|ui)|\b)"),
        ("kotlin", r"\bkotlin\b"),
        ("scala", r"\bscala\b"),
        ("r", r"\b(?:r\s+(?:programming|studio|language)|(?:python|sql|sas)[,/\s]+r\b|r[,/\s]+(?:python|sql|sas))\b"),
        ("sql", r"\bsql\b"),
        ("nosql", r"\bnosql\b"),
        # Frontend
        ("react", r"\breact(?:\.?js)?\b"),
        ("angular", r"\bangular\b"),
        ("vue", r"\bvue(?:\.?js)?\b"),
        ("node.js", r"\bnode\.?js\b"),
        ("next.js", r"\bnext\.?js\b"),
        ("express", r"\bexpress\.?js\b|\bexpress\s+(?:framework|server)\b"),
        # Backend
        ("django", r"\bdjango\b"),
        ("flask", r"\bflask\b"),
        ("spring boot", r"\bspring\s*boot\b"),
        ("spring", r"\bspring\s+(?:framework|mvc|boot|cloud|data)\b"),
        # Cloud / DevOps
        ("aws", r"\baws\b"),
        ("gcp", r"\bgcp\b|\bgoogle\s+cloud\b"),
        ("azure", r"\bazure\b"),
        ("docker", r"\bdocker\b"),
        ("kubernetes", r"\bkubernetes\b|\bk8s\b"),
        ("terraform", r"\bterraform\b"),
        ("git", r"\bgit(?:hub|lab)?\b"),
        ("linux", r"\blinux\b"),
        ("bash", r"\bbash\b"),
        # ML / AI
        ("pytorch", r"\bpytorch\b"),
        ("tensorflow", r"\btensorflow\b"),
        ("pandas", r"\bpandas\b"),
        ("numpy", r"\bnumpy\b"),
        ("scikit-learn", r"\bscikit-learn\b|\bsklearn\b"),
        ("machine learning", r"\bmachine\s+learning\b"),
        ("deep learning", r"\bdeep\s+learning\b"),
        ("nlp", r"\bnlp\b|\bnatural\s+language\s+processing\b"),
        ("computer vision", r"\bcomputer\s+vision\b"),
        # Databases
        ("postgresql", r"\bpostgre(?:sql|s)\b"),
        ("mysql", r"\bmysql\b"),
        ("mongodb", r"\bmongodb\b|\bmongo\b"),
        ("redis", r"\bredis\b"),
        ("elasticsearch", r"\belasticsearch\b|\belastic\s+search\b"),
        # APIs
        ("graphql", r"\bgraphql\b"),
        ("rest api", r"\brest(?:ful)?\s+api\b"),
        # Frontend tech
        ("html", r"\bhtml5?\b"),
        ("css", r"\bcss3?\b"),
        ("tailwind", r"\btailwind\b"),
        # Design
        ("figma", r"\bfigma\b"),
        # Process
        ("agile", r"\bagile\b"),
        ("scrum", r"\bscrum\b"),
        ("jira", r"\bjira\b"),
        # Analytics / BI
        ("excel", r"\bexcel\b"),
        ("tableau", r"\btableau\b"),
        ("power bi", r"\bpower\s*bi\b"),
        ("looker", r"\blooker\b"),
        # Business / Finance / Accounting
        ("financial modeling", r"\bfinancial\s+model"),
        ("accounting", r"\baccounting\b|\bgaap\b|\bifrs\b"),
        ("quickbooks", r"\bquickbooks\b"),
        ("sap", r"\bsap\b"),
        ("salesforce", r"\bsalesforce\b"),
        ("crm", r"\bcrm\b"),
        ("erp", r"\berp\b"),
        ("bloomberg", r"\bbloomberg\b"),
        # Marketing / Sales
        ("google analytics", r"\bgoogle\s+analytics\b"),
        ("seo", r"\bseo\b"),
        ("sem", r"\bsem\b"),
        ("hubspot", r"\bhubspot\b"),
        ("social media marketing", r"\bsocial\s+media\s+marketing\b"),
        ("content marketing", r"\bcontent\s+(?:marketing|strategy)\b"),
        ("copywriting", r"\bcopywriting\b|\bcopy\s+writing\b"),
        # Design
        ("adobe creative suite", r"\badobe\s+(?:creative|cc|photoshop|illustrator|indesign)\b"),
        ("photoshop", r"\bphotoshop\b"),
        ("illustrator", r"\billustrator\b"),
        ("sketch", r"\bsketch\s+(?:app|design|tool)\b"),
        ("invision", r"\binvision\b"),
        # HR / Legal
        ("hris", r"\bhris\b"),
        ("workday", r"\bworkday\b"),
        ("compliance", r"\bcompliance\b"),
        ("contract management", r"\bcontract\s+(?:management|drafting|review)\b"),
        # Communication / General
        ("public speaking", r"\bpublic\s+speaking\b|\bpresentation\s+skills\b"),
        ("project management", r"\bproject\s+management\b|\bpmp\b"),
        ("six sigma", r"\bsix\s+sigma\b|\blean\s+six\b"),
        ("autocad", r"\bautocad\b"),
        ("solidworks", r"\bsolidworks\b"),
        ("matlab", r"\bmatlab\b"),
    ]

    found = []
    for display_name, pattern in _SKILL_PATTERNS:
        if re.search(pattern, text):
            found.append(display_name)

    return found[:10]  # Cap at 10 skills


def _to_job(raw: dict, source_repo: str) -> Job | None:
    """Convert a JobRight job dict to our Job model."""
    title = raw.get("title", "").strip()
    company = normalize_company_name(raw.get("company", ""))

    if not title or not company:
        return None

    apply_url = raw.get("applyUrl", "")
    location = raw.get("location", "")
    salary = raw.get("salary", "")

    # Parse posted date (epoch ms)
    posted_ts = raw.get("postedDate")
    date_posted_raw = None
    if posted_ts and isinstance(posted_ts, (int, float)):
        try:
            dt = datetime.fromtimestamp(posted_ts / 1000, tz=timezone.utc)
            date_posted_raw = dt.strftime("%Y-%m-%d")
        except (ValueError, OSError):
            pass

    # Map enrichment fields directly from JobRight data
    industry = _map_industry(raw.get("industry"))
    company_size = COMPANY_SIZE_MAP.get(raw.get("companySize", ""), None)
    skills = _extract_skills_from_qualifications(raw.get("qualifications"))
    sponsorship = {
        "Yes": True,
        "No": False,
    }.get(raw.get("h1bSponsored"), None)

    job_id = generate_job_id(company, title, apply_url)

    content_hash = compute_content_hash(
        company_name=company,
        role_title=title,
    )

    return Job(
        job_id=job_id,
        source_repo=source_repo,
        company_name=company,
        role_title=title,
        primary_url=apply_url or None,
        location_raw=location,
        date_posted_raw=date_posted_raw,
        status=JobStatus.ACTIVE,
        content_hash=content_hash,
        # Pre-enriched fields from JobRight
        industry=industry,
        company_size=company_size,
        required_skills=skills,
        sponsorship=sponsorship,
    )


def scrape_jobright(
    job_types: list[str] | None = None,
    countries: list[str] | None = None,
    categories: list[str] | None = None,
) -> list[Job]:
    """Scrape job listings from JobRight.ai.

    Args:
        job_types: Override JOB_TYPES (default: ["intern", "newgrad"])
        countries: Override COUNTRIES (default: ["us"])
        categories: Override CATEGORIES (default: 10 tech/business categories)

    Returns:
        List of Job objects ready for upsert.
    """
    types = job_types or JOB_TYPES
    ctries = countries or COUNTRIES
    cats = categories or CATEGORIES

    all_jobs: list[Job] = []
    seen_ids: set[str] = set()

    for jtype in types:
        source_repo = f"jobright-{jtype}"
        for country in ctries:
            for category in cats:
                raw_jobs = _fetch_page(jtype, country, category)

                for raw in raw_jobs:
                    job = _to_job(raw, source_repo)
                    if job and job.job_id not in seen_ids:
                        seen_ids.add(job.job_id)
                        all_jobs.append(job)

                # Be polite — 0.5s between requests
                time.sleep(0.5)

    logger.info("JobRight scrape complete: {} unique jobs from {} pages",
                len(all_jobs), len(types) * len(ctries) * len(cats))
    return all_jobs


if __name__ == "__main__":
    jobs = scrape_jobright()
    print(f"\nTotal: {len(jobs)} unique jobs")
    for j in jobs[:5]:
        print(f"  {j.company_name:25s} {j.role_title:45s} {j.location_raw}")
