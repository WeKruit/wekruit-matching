"""Unit tests for Phase 73 — Greenhouse direct API scraper."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from wekruit_matching.scraper.greenhouse_direct import (
    GREENHOUSE_COMPANIES,
    SOURCE_NAME,
    _strip_html,
    _to_job,
    scrape_greenhouse_company,
    scrape_greenhouse_direct,
)
from wekruit_matching.scraper.title_inference import infer_seniority as _infer_seniority


# ---------------------------------------------------------------------------
# _infer_seniority
# ---------------------------------------------------------------------------


def test_infer_seniority_senior_variants():
    assert _infer_seniority("Senior Software Engineer") == "senior"
    assert _infer_seniority("Sr. Engineer") == "senior"


def test_infer_seniority_staff_principal():
    assert _infer_seniority("Staff Engineer") == "staff"
    assert _infer_seniority("Principal SWE") == "principal"


def test_infer_seniority_director_vp():
    assert _infer_seniority("Director of Engineering") == "director"
    assert _infer_seniority("VP of Product") == "vp"


def test_infer_seniority_intern():
    assert _infer_seniority("Software Engineering Intern") == "intern"
    assert _infer_seniority("Co-op Student") == "intern"


def test_infer_seniority_default_mid_level():
    assert _infer_seniority("Software Engineer") == "mid_level"
    assert _infer_seniority("") == "mid_level"
    assert _infer_seniority(None) == "mid_level"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------


def test_strip_html_basic():
    assert _strip_html("<p>Hello</p> <b>world</b>") == "Hello world"


def test_strip_html_empty_or_none():
    assert _strip_html(None) == ""
    assert _strip_html("") == ""


def test_strip_html_collapses_whitespace():
    assert _strip_html("a\n\n\nb \t  c") == "a b c"


# ---------------------------------------------------------------------------
# _to_job
# ---------------------------------------------------------------------------


def test_to_job_basic():
    raw = {
        "title": "Senior Software Engineer",
        "absolute_url": "https://boards.greenhouse.io/anthropic/jobs/123",
        "location": {"name": "San Francisco, CA"},
        "updated_at": "2026-05-01T12:00:00Z",
        "content": "<p>Build the future</p>",
    }
    job = _to_job(raw, slug="anthropic", company_display="Anthropic")
    assert job is not None
    assert job.role_title == "Senior Software Engineer"
    assert job.seniority_level == "senior"
    assert job.primary_url == raw["absolute_url"]
    assert "anthropic" in job.company_name.lower()
    assert job.source_repo == "greenhouse:anthropic"
    assert job.sources == ["greenhouse"]
    assert job.location_raw == "San Francisco, CA"
    assert job.job_description == "Build the future"


def test_to_job_skips_when_missing_title():
    raw = {"absolute_url": "https://boards.greenhouse.io/x/jobs/1"}
    assert _to_job(raw, slug="x", company_display="X") is None


def test_to_job_skips_when_missing_url():
    raw = {"title": "SWE"}
    assert _to_job(raw, slug="x", company_display="X") is None


def test_to_job_handles_string_location():
    raw = {
        "title": "Engineer",
        "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
        "location": "Remote",
    }
    job = _to_job(raw, slug="x", company_display="X")
    assert job is not None
    assert job.location_raw == "Remote"


def test_to_job_returns_none_for_non_dict():
    assert _to_job("not-a-dict", slug="x", company_display="X") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scrape_greenhouse_company — full flow with mocked httpx
# ---------------------------------------------------------------------------


def _mock_client(*, status: int = 200, payload: dict | None = None,
                 raise_exc: Exception | None = None) -> httpx.Client:
    """Return a httpx.Client whose .get() is patched to return one canned response."""
    cli = MagicMock(spec=httpx.Client)
    if raise_exc is not None:
        cli.get.side_effect = raise_exc
    else:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        resp.json.return_value = payload or {}
        cli.get.return_value = resp
    return cli


def test_scrape_greenhouse_company_returns_jobs():
    payload = {
        "name": "Anthropic",
        "jobs": [
            {
                "title": "Staff Engineer",
                "absolute_url": "https://boards.greenhouse.io/anthropic/jobs/1",
                "location": {"name": "Remote"},
                "updated_at": "2026-05-01T00:00:00Z",
                "content": "<p>cool</p>",
            },
            {
                "title": "Software Engineer",
                "absolute_url": "https://boards.greenhouse.io/anthropic/jobs/2",
                "location": {"name": "SF"},
            },
        ],
    }
    cli = _mock_client(payload=payload)
    jobs = scrape_greenhouse_company("anthropic", client=cli)
    assert len(jobs) == 2
    titles = {j.role_title for j in jobs}
    assert "Staff Engineer" in titles
    assert "Software Engineer" in titles
    assert all(j.sources == ["greenhouse"] for j in jobs)


def test_scrape_greenhouse_company_returns_empty_on_404():
    cli = _mock_client(status=404)
    assert scrape_greenhouse_company("nonexistent", client=cli) == []


def test_scrape_greenhouse_company_returns_empty_on_http_error():
    cli = _mock_client(raise_exc=httpx.ConnectError("boom"))
    assert scrape_greenhouse_company("x", client=cli) == []


def test_scrape_greenhouse_company_returns_empty_on_bad_json():
    cli = MagicMock(spec=httpx.Client)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.side_effect = ValueError("not json")
    cli.get.return_value = resp
    assert scrape_greenhouse_company("x", client=cli) == []


def test_scrape_greenhouse_company_dedups_within_response():
    """Same job_id appearing twice in one company's payload is only kept once."""
    raw = {
        "title": "SWE",
        "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
        "location": {"name": "X"},
    }
    payload = {"name": "X", "jobs": [raw, dict(raw)]}
    cli = _mock_client(payload=payload)
    jobs = scrape_greenhouse_company("x", client=cli)
    assert len(jobs) == 1


def test_scrape_greenhouse_company_respects_max_jobs():
    payload = {
        "name": "X",
        "jobs": [
            {
                "title": f"Engineer {i}",
                "absolute_url": f"https://boards.greenhouse.io/x/jobs/{i}",
            }
            for i in range(20)
        ],
    }
    cli = _mock_client(payload=payload)
    jobs = scrape_greenhouse_company("x", client=cli, max_jobs=5)
    assert len(jobs) == 5


# ---------------------------------------------------------------------------
# scrape_greenhouse_direct — sweep across companies
# ---------------------------------------------------------------------------


def test_scrape_greenhouse_direct_iterates_companies():
    payload = {
        "name": "X",
        "jobs": [
            {"title": "SWE", "absolute_url": "https://boards.greenhouse.io/x/jobs/1"},
        ],
    }
    cli = _mock_client(payload=payload)
    jobs = scrape_greenhouse_direct(
        client=cli, companies=["a", "b", "c"], inter_company_delay=0
    )
    # Each company yields the same payload, but different slug → still
    # the same canonical company_name+title+url, so dedup at this layer is
    # NOT done (multi-source dedup happens at Stage 1.6 dedup_multi_source).
    # Within one company we keep one. Across companies, may produce 1-3.
    assert len(jobs) >= 1
    assert all(j.sources == ["greenhouse"] for j in jobs)


def test_scrape_greenhouse_direct_handles_one_dead_company():
    """A 404 on one slug must not stop the rest."""
    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        resp = MagicMock(spec=httpx.Response)
        if "dead" in url:
            resp.status_code = 404
        else:
            resp.status_code = 200
            resp.json.return_value = {
                "name": "X",
                "jobs": [{"title": "SWE", "absolute_url": "https://x/jobs/1"}],
            }
        return resp

    cli = MagicMock(spec=httpx.Client)
    cli.get.side_effect = fake_get
    jobs = scrape_greenhouse_direct(
        client=cli, companies=["alive", "dead", "alive2"], inter_company_delay=0
    )
    assert call_count["n"] == 3
    assert len(jobs) >= 1


def test_greenhouse_companies_list_is_nonempty():
    assert len(GREENHOUSE_COMPANIES) >= 30
    assert "anthropic" in GREENHOUSE_COMPANIES
    assert SOURCE_NAME == "greenhouse"
