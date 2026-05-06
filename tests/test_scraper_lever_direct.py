"""Unit tests for Phase 73 — Lever direct API scraper."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from wekruit_matching.scraper.lever_direct import (
    LEVER_COMPANIES,
    SOURCE_NAME,
    _infer_seniority,
    _to_job,
    scrape_lever_company,
    scrape_lever_direct,
)


# ---------------------------------------------------------------------------
# _infer_seniority
# ---------------------------------------------------------------------------


def test_infer_seniority_basic():
    assert _infer_seniority("Senior Backend Engineer") == "senior"
    assert _infer_seniority("Staff Software Engineer") == "staff"
    assert _infer_seniority("VP Engineering") == "vp"
    assert _infer_seniority("Engineering Intern") == "intern"
    assert _infer_seniority("Software Engineer") == "mid_level"


# ---------------------------------------------------------------------------
# _to_job
# ---------------------------------------------------------------------------


def test_to_job_basic_with_hosted_url():
    raw = {
        "id": "abc123",
        "text": "Senior Software Engineer",
        "hostedUrl": "https://jobs.lever.co/netflix/abc123",
        "categories": {"location": "Los Gatos, CA", "team": "Streaming"},
        "createdAt": 1714521600000,  # epoch ms
        "descriptionPlain": "Build the streaming experience",
        "additionalPlain": "We offer competitive comp",
    }
    job = _to_job(raw, slug="netflix")
    assert job is not None
    assert job.role_title == "Senior Software Engineer"
    assert job.seniority_level == "senior"
    assert job.primary_url == raw["hostedUrl"]
    assert job.location_raw == "Los Gatos, CA"
    assert job.source_repo == "lever:netflix"
    assert job.sources == ["lever"]
    assert job.job_description is not None
    assert "streaming" in job.job_description.lower()
    assert "competitive" in job.job_description.lower()


def test_to_job_falls_back_to_apply_url():
    raw = {
        "text": "SWE",
        "applyUrl": "https://jobs.lever.co/x/apply/1",
    }
    job = _to_job(raw, slug="x")
    assert job is not None
    assert job.primary_url == raw["applyUrl"]


def test_to_job_skips_when_no_url():
    raw = {"text": "SWE"}
    assert _to_job(raw, slug="x") is None


def test_to_job_skips_when_no_title():
    raw = {"hostedUrl": "https://jobs.lever.co/x/1"}
    assert _to_job(raw, slug="x") is None


def test_to_job_handles_missing_categories():
    raw = {
        "text": "Engineer",
        "hostedUrl": "https://jobs.lever.co/x/1",
    }
    job = _to_job(raw, slug="x")
    assert job is not None
    assert job.location_raw == ""


def test_to_job_handles_invalid_created_at():
    raw = {
        "text": "Engineer",
        "hostedUrl": "https://jobs.lever.co/x/1",
        "createdAt": "not-an-epoch",
    }
    job = _to_job(raw, slug="x")
    assert job is not None
    assert job.date_posted_raw is None


def test_to_job_returns_none_for_non_dict():
    assert _to_job("oops", slug="x") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scrape_lever_company — full flow with mocked httpx
# ---------------------------------------------------------------------------


def _mock_client(*, status: int = 200, payload=None,
                 raise_exc: Exception | None = None) -> httpx.Client:
    cli = MagicMock(spec=httpx.Client)
    if raise_exc is not None:
        cli.get.side_effect = raise_exc
    else:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        resp.json.return_value = payload if payload is not None else []
        cli.get.return_value = resp
    return cli


def test_scrape_lever_company_returns_jobs():
    payload = [
        {
            "text": "Staff Engineer",
            "hostedUrl": "https://jobs.lever.co/netflix/1",
            "categories": {"location": "Remote"},
        },
        {
            "text": "Senior SWE",
            "hostedUrl": "https://jobs.lever.co/netflix/2",
            "categories": {"location": "Los Gatos"},
        },
    ]
    cli = _mock_client(payload=payload)
    jobs = scrape_lever_company("netflix", client=cli)
    assert len(jobs) == 2
    seniorities = {j.seniority_level for j in jobs}
    assert "staff" in seniorities
    assert "senior" in seniorities


def test_scrape_lever_company_returns_empty_on_non_list():
    cli = _mock_client(payload={"error": "not a list"})
    assert scrape_lever_company("x", client=cli) == []


def test_scrape_lever_company_returns_empty_on_404():
    cli = _mock_client(status=404)
    assert scrape_lever_company("x", client=cli) == []


def test_scrape_lever_company_returns_empty_on_bad_json():
    cli = MagicMock(spec=httpx.Client)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.side_effect = ValueError("nope")
    cli.get.return_value = resp
    assert scrape_lever_company("x", client=cli) == []


def test_scrape_lever_company_returns_empty_on_http_error():
    cli = _mock_client(raise_exc=httpx.ReadTimeout("timeout"))
    assert scrape_lever_company("x", client=cli) == []


def test_scrape_lever_company_dedups_within_response():
    raw = {"text": "SWE", "hostedUrl": "https://jobs.lever.co/x/1"}
    cli = _mock_client(payload=[raw, dict(raw)])
    jobs = scrape_lever_company("x", client=cli)
    assert len(jobs) == 1


def test_scrape_lever_company_respects_max_jobs():
    payload = [
        {"text": f"Engineer {i}", "hostedUrl": f"https://jobs.lever.co/x/{i}"}
        for i in range(20)
    ]
    cli = _mock_client(payload=payload)
    jobs = scrape_lever_company("x", client=cli, max_jobs=4)
    assert len(jobs) == 4


# ---------------------------------------------------------------------------
# scrape_lever_direct — sweep
# ---------------------------------------------------------------------------


def test_scrape_lever_direct_iterates():
    payload = [
        {"text": "SWE", "hostedUrl": "https://jobs.lever.co/x/1"},
    ]
    cli = _mock_client(payload=payload)
    jobs = scrape_lever_direct(
        client=cli, companies=["a", "b"], inter_company_delay=0
    )
    assert len(jobs) >= 1
    assert all(j.sources == ["lever"] for j in jobs)


def test_lever_companies_list_is_nonempty():
    assert len(LEVER_COMPANIES) >= 5
    assert "spotify" in LEVER_COMPANIES
    assert SOURCE_NAME == "lever"
