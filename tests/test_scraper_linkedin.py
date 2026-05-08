"""Unit tests for Phase 63 — LinkedIn scraper (API + HTML paths)."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from wekruit_matching.scraper.linkedin import (
    SOURCE_REPO_SLUG,
    _fetch_via_api,
    _fetch_via_html,
    _normalize_api_items,
    _parse_html_jobs,
    _to_job,
    scrape_linkedin,
)
from wekruit_matching.scraper.title_inference import infer_seniority as _infer_seniority


# ---------------------------------------------------------------------------
# _infer_seniority
# ---------------------------------------------------------------------------


def test_infer_seniority_senior():
    assert _infer_seniority("Senior Software Engineer") == "senior"


def test_infer_seniority_staff_principal():
    assert _infer_seniority("Staff Engineer") == "staff"
    assert _infer_seniority("Principal SWE") == "principal"


def test_infer_seniority_director_vp():
    assert _infer_seniority("Director of Engineering") == "director"
    assert _infer_seniority("VP of Product") == "vp"


# ---------------------------------------------------------------------------
# _normalize_api_items
# ---------------------------------------------------------------------------


def test_normalize_api_items_basic():
    items = [
        {
            "title": "Senior SWE",
            "companyDetails": {"company": {"name": "Acme"}},
            "formattedLocation": "San Francisco, CA",
            "applyUrl": "https://linkedin.com/jobs/1",
            "listedAt": 1714521600000,  # 2024-05-01
        }
    ]
    out = _normalize_api_items(items)
    assert len(out) == 1
    assert out[0]["title"] == "Senior SWE"
    assert out[0]["company"] == "Acme"
    assert out[0]["posted_date"] is not None


def test_normalize_api_items_skips_missing():
    items = [
        {"title": "x"},  # no company
        {"companyDetails": {"company": {"name": "Acme"}}},  # no title
    ]
    assert _normalize_api_items(items) == []


# ---------------------------------------------------------------------------
# _parse_html_jobs
# ---------------------------------------------------------------------------


def test_parse_html_jobs_extracts_card():
    html = """
    <li>
        <div class="base-card">
            <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/12345">
                <h3 class="base-search-card__title">Senior Software Engineer</h3>
                <h4 class="base-search-card__subtitle"><a>Acme Corp</a></h4>
                <span class="job-search-card__location">San Francisco, CA</span>
            </a>
        </div>
    </li>
    """
    jobs = _parse_html_jobs(html)
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Software Engineer"
    assert jobs[0]["company"] == "Acme Corp"
    assert "/jobs/view/12345" in jobs[0]["apply_url"]


def test_parse_html_jobs_empty_on_unrelated_html():
    assert _parse_html_jobs("<html>nothing</html>") == []


# ---------------------------------------------------------------------------
# _to_job
# ---------------------------------------------------------------------------


def test_to_job_sources_linkedin():
    raw = {
        "title": "Senior SWE",
        "company": "Acme",
        "apply_url": "https://linkedin.com/jobs/1",
        "location": "Remote",
    }
    job = _to_job(raw)
    assert job is not None
    assert job.sources == [SOURCE_REPO_SLUG]
    assert job.source_repo == SOURCE_REPO_SLUG
    assert job.seniority_level == "senior"


# ---------------------------------------------------------------------------
# scrape_linkedin — fallback to HTML when no token
# ---------------------------------------------------------------------------


def _mock_resp(status: int, text: str = "", json_data: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = json_data or {}
    return r


def test_scrape_linkedin_falls_back_to_html_without_token(monkeypatch):
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("time.sleep", lambda _: None)

    html = """
    <li>
        <div class="base-card">
            <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/1">
                <h3 class="base-search-card__title">Senior Software Engineer</h3>
                <h4 class="base-search-card__subtitle"><a>Acme</a></h4>
                <span class="job-search-card__location">Remote</span>
            </a>
        </div>
    </li>
    """
    cli = MagicMock(spec=httpx.Client)
    cli.get.return_value = _mock_resp(200, text=html)

    jobs = scrape_linkedin(client=cli, access_token=None)
    assert len(jobs) == 1
    assert jobs[0].sources == ["linkedin"]
    assert jobs[0].seniority_level == "senior"


def test_scrape_linkedin_uses_api_when_token_present(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)

    api_data = {
        "elements": [
            {
                "title": "Staff Engineer",
                "companyDetails": {"company": {"name": "Globex"}},
                "formattedLocation": "Remote",
                "applyUrl": "https://linkedin.com/jobs/2",
                "listedAt": 1714521600000,
            }
        ]
    }
    cli = MagicMock(spec=httpx.Client)
    cli.get.return_value = _mock_resp(200, json_data=api_data)

    jobs = scrape_linkedin(client=cli, access_token="fake-token-xyz")
    assert len(jobs) == 1
    assert jobs[0].sources == ["linkedin"]
    assert jobs[0].seniority_level == "staff"


def test_scrape_linkedin_returns_empty_on_429(monkeypatch):
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("time.sleep", lambda _: None)

    cli = MagicMock(spec=httpx.Client)
    # All retries return 429
    cli.get.return_value = _mock_resp(429)

    jobs = scrape_linkedin(client=cli, access_token=None)
    # _get_with_backoff retries 4 times; all 429 → returns None → 0 jobs
    assert jobs == []


def test_scrape_linkedin_respects_max_jobs(monkeypatch):
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("time.sleep", lambda _: None)

    # Build 5 cards in HTML
    cards = "".join(
        f"""
        <li>
            <div class="base-card">
                <a class="base-card__full-link" href="https://linkedin.com/jobs/view/{i}">
                    <h3 class="base-search-card__title">Senior Eng {i}</h3>
                    <h4 class="base-search-card__subtitle"><a>Co{i}</a></h4>
                    <span class="job-search-card__location">Remote</span>
                </a>
            </div>
        </li>
        """
        for i in range(5)
    )
    cli = MagicMock(spec=httpx.Client)
    cli.get.return_value = _mock_resp(200, text=cards)

    jobs = scrape_linkedin(client=cli, max_jobs=3, access_token=None)
    assert len(jobs) == 3
