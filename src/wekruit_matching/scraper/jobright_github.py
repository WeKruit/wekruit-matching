"""JobRight.ai GitHub scraper.

Fetches job listings from jobright-ai GitHub org repos.
Each repo has a README.md with markdown pipe tables — easy to parse.
Updated daily, only jobs from the last 7 days.

Repos: jobright-ai/2026-{Category}-{Type} (Internship or New-Grad)
Format: | **[Company](url)** | **[Title](apply_url)** | Location | Work Model | Date |

Usage:
    from wekruit_matching.scraper.jobright_github import scrape_jobright_github
    jobs = scrape_jobright_github()
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from loguru import logger

from wekruit_matching.config import get_settings
from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper.id_utils import (
    compute_content_hash,
    generate_job_id,
    normalize_company_name,
)

JOBRIGHT_ORG = "jobright-ai"

# ALL repos from jobright-ai org — scrape every category
INTERN_REPOS = [
    "2026-Software-Engineer-Internship",
    "2026-Data-Analysis-Internship",
    "2026-Engineer-Internship",
    "2026-Product-Management-Internship",
    "2026-Business-Analyst-Internship",
    "2026-Design-Internship",
    "2026-Consultant-Internship",
    "2026-Account-Internship",
    "2026-Marketing-Internship",
    "2026-Management-Internship",
    "2026-Sales-Internship",
    "2026-HR-Internship",
    "2026-Legal-Internship",
    "2026-Art-Internship",
    "2026-Education-Internship",
    "2026-Public-Sector-Internship",
    "2026-Support-Internship",
    "2026-Internship",
]

NEW_GRAD_REPOS = [
    "2026-Software-Engineer-New-Grad",
    "2026-Data-Analysis-New-Grad",
    "2026-Engineering-New-Grad",
    "2026-Business-Analyst-New-Grad",
    "2026-Product-Management-New-Grad",
    "2026-Design-New-Grad",
    "2026-Consultant-New-Grad",
    "2026-Account-New-Grad",
    "2026-Marketing-New-Grad",
    "2026-Management-New-Grad",
    "2026-Sales-New-Grad",
    "2026-HR-New-Grad",
    "2026-Legal-New-Grad",
    "2026-Art-New-Grad",
    "2026-Education-New-Grad",
    "2026-Public-Sector-New-Grad",
    "2026-Support-New-Grad",
]

# Category inferred from repo name → our industry vocabulary
REPO_TO_CATEGORY: dict[str, str] = {
    "Software-Engineer": "tech",
    "Data-Analysis": "ai_ml",
    "Engineer": "hardware",
    "Engineering": "hardware",
    "Product-Management": "tech",
    "Business-Analyst": "consulting",
    "Design": "other",
    "Consultant": "consulting",
    "Account": "fintech",
    "Marketing": "other",
    "Management": "consulting",
    "Sales": "other",
    "HR": "other",
    "Legal": "other",
    "Art": "other",
    "Education": "other",
    "Public-Sector": "other",
    "Support": "other",
    "Internship": "other",
}

WORK_MODEL_NORM = {
    "on site": "onsite",
    "remote": "remote",
    "hybrid": "hybrid",
    "on-site": "onsite",
}


def _fetch_readme(repo: str) -> str:
    """Fetch raw README.md from a jobright-ai repo."""
    settings = get_settings()
    url = f"https://api.github.com/repos/{JOBRIGHT_ORG}/{repo}/contents/README.md"
    headers = {
        "Authorization": f"token {settings.github_token}",
        "Accept": "application/vnd.github.v3.raw",
    }
    resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _parse_markdown_table(readme: str, source_repo: str, category: str) -> list[Job]:
    """Parse JobRight markdown pipe table into Job objects.

    Format:
    | **[Company](company_url)** | **[Job Title](apply_url)** | Location | Work Model | Date |
    """
    jobs: list[Job] = []
    seen_ids: set[str] = set()

    # Match table rows: | **[...](...)** | **[...](...)** | ... | ... | ... |
    row_pattern = re.compile(
        r'^\|\s*\*\*\[([^\]]+)\]\(([^)]*)\)\*\*\s*\|\s*\*\*\[([^\]]+)\]\(([^)]*)\)\*\*\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|',
        re.MULTILINE,
    )

    for match in row_pattern.finditer(readme):
        company_raw = match.group(1).strip()
        # company_url = match.group(2).strip()  # Not used currently
        title_raw = match.group(3).strip()
        apply_url = match.group(4).strip()
        location = match.group(5).strip()
        work_model_raw = match.group(6).strip().lower()
        date_raw = match.group(7).strip()

        company = normalize_company_name(company_raw)
        if not company or not title_raw:
            continue

        # Normalize work model
        work_model = WORK_MODEL_NORM.get(work_model_raw, work_model_raw)

        # Generate stable ID
        job_id = generate_job_id(company, title_raw, apply_url)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        content_hash = compute_content_hash(company, title_raw)

        jobs.append(Job(
            job_id=job_id,
            source_repo=source_repo,
            company_name=company,
            role_title=title_raw,
            primary_url=apply_url or None,
            location_raw=location,
            date_posted_raw=date_raw,
            status=JobStatus.ACTIVE,
            content_hash=content_hash,
            industry=category,
        ))

    return jobs


def scrape_jobright_github(
    intern_repos: list[str] | None = None,
    newgrad_repos: list[str] | None = None,
) -> list[Job]:
    """Scrape all JobRight GitHub repos.

    Returns:
        List of Job objects ready for upsert. source_repo is
        "jobright-intern" or "jobright-newgrad".
    """
    i_repos = intern_repos or INTERN_REPOS
    ng_repos = newgrad_repos or NEW_GRAD_REPOS

    all_jobs: list[Job] = []
    seen_ids: set[str] = set()

    def _scrape_repos(repos: list[str], source_repo: str) -> None:
        for repo in repos:
            # Extract category from repo name
            cat_key = repo.replace("2026-", "").replace("-Internship", "").replace("-New-Grad", "")
            category = REPO_TO_CATEGORY.get(cat_key, "other")

            try:
                readme = _fetch_readme(repo)
                jobs = _parse_markdown_table(readme, source_repo, category)

                for job in jobs:
                    if job.job_id not in seen_ids:
                        seen_ids.add(job.job_id)
                        all_jobs.append(job)

                logger.info("Parsed {} jobs from {}", len(jobs), repo)
            except Exception as e:
                logger.warning("Failed to scrape {}: {}", repo, e)

    _scrape_repos(i_repos, "jobright-intern")
    _scrape_repos(ng_repos, "jobright-newgrad")

    logger.info("JobRight GitHub scrape complete: {} unique jobs", len(all_jobs))
    return all_jobs


if __name__ == "__main__":
    jobs = scrape_jobright_github()
    intern = sum(1 for j in jobs if j.source_repo == "jobright-intern")
    newgrad = sum(1 for j in jobs if j.source_repo == "jobright-newgrad")
    has_url = sum(1 for j in jobs if j.primary_url)
    print(f"\nTotal: {len(jobs)} (intern={intern}, newgrad={newgrad}, urls={has_url})")
    for j in jobs[:5]:
        print(f"  {j.company_name:25s} {j.role_title:45s} {j.location_raw}")
