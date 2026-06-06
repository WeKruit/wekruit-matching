"""JobRight.ai GitHub scraper.

Fetches job listings from jobright-ai GitHub org repos.
Each repo has a README.md with markdown pipe tables — easy to parse.
Updated daily, only jobs from the last 7 days.

Repos: jobright-ai/2026-{Category}-{Type} (Internship or New-Grad)
Format: | **[Company](url)** | **[Title](apply_url)** | Location | Work Model | Date |

Usage:
    from wekruit_matching.scraper.jobright_github import scrape_jobright_github
    jobs, _stale_action = scrape_jobright_github()
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from loguru import logger

from wekruit_matching.config import get_settings
from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper import jobright_git_delta
from wekruit_matching.scraper.http_util import call_with_hard_deadline
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
    "Data-Analysis": "data_analytics",
    "Engineer": "engineering",
    "Engineering": "engineering",
    "Product-Management": "product",
    "Business-Analyst": "business",
    "Design": "design",
    "Consultant": "consulting",
    "Account": "accounting_finance",
    "Marketing": "marketing",
    "Management": "management",
    "Sales": "sales",
    "HR": "human_resources",
    "Legal": "legal",
    "Art": "arts_entertainment",
    "Education": "education",
    "Public-Sector": "government",
    "Support": "customer_service",
    "Internship": "general",
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
    # Hard total deadline (on top of timeout=30): a trickling GitHub connection
    # held one fetch open ~48 min on 2026-06-06 because httpx's read timeout only
    # fires on a fully idle socket. The per-repo caller catches + skips on raise,
    # so a stuck repo now costs ~45s, not 48 min, and the rest still scrape.
    resp = call_with_hard_deadline(
        httpx.get, url, headers=headers, timeout=30, follow_redirects=True,
        deadline_s=45.0,
    )
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
        job_id = generate_job_id(source_repo, company, title_raw)
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
) -> tuple[list[Job], dict[str, tuple[str, set[str]]]]:
    """Scrape all JobRight GitHub repos.

    Returns:
        (all_jobs, stale_ids_by_repo)
        - all_jobs            : Job[] to upsert
        - stale_ids_by_repo   : dict[source_repo, set[job_id]] from pure-diff removed
                                rows. EMPTY when delta mode is off OR repo fell
                                back to full-README parse on this run.
    """
    i_repos = intern_repos or INTERN_REPOS
    ng_repos = newgrad_repos or NEW_GRAD_REPOS

    use_delta = jobright_git_delta.is_enabled()
    if use_delta:
        logger.info("JOBRIGHT_USE_GIT_DELTA=1 — pure-diff jobright scrape (HEAD~1..HEAD)")

    all_jobs: list[Job] = []
    seen_ids: set[str] = set()
    # v4 (2026-05-14) — per-repo stale action: ("specific", removed_ids) when
    # the pure-diff path saw a real - row set, ("full_scope", seen_ids_in_full_parse)
    # when the repo fell back to full README parse (bootstrap, force-push, etc).
    # run.py reads the mode to pick mark_specific_ids_inactive vs mark_stale_jobs.
    stale_action_by_repo: dict[str, tuple[str, set[str]]] = {
        "jobright-intern": ("specific", set()),
        "jobright-newgrad": ("specific", set()),
    }
    # Tracks seen_ids per repo across fallback runs so we can run full
    # mark_stale_jobs() at the end (inverse semantics: drop everything not
    # in the set).
    fallback_seen_by_repo: dict[str, set[str]] = {
        "jobright-intern": set(),
        "jobright-newgrad": set(),
    }
    fallback_used_by_repo: dict[str, bool] = {
        "jobright-intern": False,
        "jobright-newgrad": False,
    }

    def _row_to_jobs(rows: list[str], source_repo: str, category: str) -> list[Job]:
        """Parse a list of pre-extracted markdown rows by feeding them as a fake README."""
        if not rows:
            return []
        return _parse_markdown_table("\n".join(rows), source_repo, category)

    def _row_to_ids(rows: list[str], source_repo: str) -> set[str]:
        """Hash removed rows to v2 stable job_ids without building full Job objects."""
        ids: set[str] = set()
        if not rows:
            return ids
        row_pattern = re.compile(
            r"^\|\s*\*\*\[([^\]]+)\]\([^)]*\)\*\*\s*\|\s*\*\*\[([^\]]+)\]\([^)]*\)\*\*\s*\|",
        )
        for raw in rows:
            m = row_pattern.match(raw)
            if not m:
                continue
            company = normalize_company_name(m.group(1).strip())
            title = m.group(2).strip()
            if not company or not title:
                continue
            ids.add(generate_job_id(source_repo, company, title))
        return ids

    def _scrape_repos(repos: list[str], source_repo: str) -> None:
        for repo in repos:
            cat_key = repo.replace("2026-", "").replace("-Internship", "").replace("-New-Grad", "")
            category = REPO_TO_CATEGORY.get(cat_key, "other")

            try:
                if use_delta:
                    snap = jobright_git_delta.fetch_repo(repo)
                    if snap.used_delta:
                        new_jobs = _row_to_jobs(snap.added_rows, source_repo, category)
                        stale_ids = _row_to_ids(snap.removed_rows, source_repo)
                        for job in new_jobs:
                            if job.job_id not in seen_ids:
                                seen_ids.add(job.job_id)
                                all_jobs.append(job)
                        # v4-fix (2026-05-14): pure-diff jobs must also land in the
                        # "seen" set so a sibling repo's full_scope fallback does not
                        # let mark_stale_jobs() flip these jobs to inactive (anything
                        # not in fallback_seen_by_repo would be considered stale).
                        fallback_seen_by_repo[source_repo].update(j.job_id for j in new_jobs)
                        # Merge specific-mode stale ids. Once a repo has gone
                        # through full-parse fallback in the same scrape run we leave
                        # it on full_scope; pure-diff stale_ids are a strict subset.
                        if stale_action_by_repo[source_repo][0] == "specific":
                            stale_action_by_repo[source_repo][1].update(stale_ids)
                        logger.info(
                            "pure-diff {}: +{} new -{} stale (since HEAD~1)",
                            repo, len(new_jobs), len(stale_ids),
                        )
                        continue
                    # used_delta=False fallback path: full local readme.
                    readme = snap.full_readme or ""
                    logger.info("fallback full parse for {} ({}B)", repo, len(readme))
                else:
                    readme = _fetch_readme(repo)

                jobs = _parse_markdown_table(readme, source_repo, category)
                # Mark this repo as "full_scope" so run.py uses mark_stale_jobs.
                fallback_used_by_repo[source_repo] = True
                fallback_seen_by_repo[source_repo].update(j.job_id for j in jobs)
                stale_action_by_repo[source_repo] = ("full_scope", fallback_seen_by_repo[source_repo])
                for job in jobs:
                    if job.job_id not in seen_ids:
                        seen_ids.add(job.job_id)
                        all_jobs.append(job)
                logger.info("Parsed {} jobs from {} (full-parse fallback)", len(jobs), repo)
            except Exception as e:
                logger.warning("Failed to scrape {}: {}", repo, e)

    _scrape_repos(i_repos, "jobright-intern")
    _scrape_repos(ng_repos, "jobright-newgrad")

    logger.info(
        "JobRight GitHub scrape complete: {} unique new jobs; intern={}({}) newgrad={}({})",
        len(all_jobs),
        stale_action_by_repo["jobright-intern"][0],
        len(stale_action_by_repo["jobright-intern"][1]),
        stale_action_by_repo["jobright-newgrad"][0],
        len(stale_action_by_repo["jobright-newgrad"][1]),
    )
    return all_jobs, stale_action_by_repo


if __name__ == "__main__":
    jobs = scrape_jobright_github()
    intern = sum(1 for j in jobs if j.source_repo == "jobright-intern")
    newgrad = sum(1 for j in jobs if j.source_repo == "jobright-newgrad")
    has_url = sum(1 for j in jobs if j.primary_url)
    print(f"\nTotal: {len(jobs)} (intern={intern}, newgrad={newgrad}, urls={has_url})")
    for j in jobs[:5]:
        print(f"  {j.company_name:25s} {j.role_title:45s} {j.location_raw}")
