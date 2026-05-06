"""Unit tests for Phase 73 — Ashby direct API scraper."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from wekruit_matching.scraper.ashby_direct import (
    ASHBY_COMPANIES,
    SOURCE_NAME,
    _extract_company_display,
    _extract_jobs_array,
    _infer_seniority,
    _to_job,
    scrape_ashby_company,
    scrape_ashby_direct,
)


# ---------------------------------------------------------------------------
# _infer_seniority
# ---------------------------------------------------------------------------


def test_infer_seniority_basic():
    assert _infer_seniority("Senior Software Engineer") == "senior"
    assert _infer_seniority("Staff ML Engineer") == "staff"
    assert _infer_seniority("Principal SWE") == "principal"
    assert _infer_seniority("Software Engineer") == "mid_level"


# ---------------------------------------------------------------------------
# _extract_jobs_array — probes both shapes
# ---------------------------------------------------------------------------


def test_extract_jobs_flat_shape():
    data = {"jobs": [{"title": "a"}, {"title": "b"}]}
    assert _extract_jobs_array(data) == [{"title": "a"}, {"title": "b"}]


def test_extract_jobs_nested_jobboard():
    data = {"jobBoard": {"jobs": [{"title": "x"}]}}
    assert _extract_jobs_array(data) == [{"title": "x"}]


def test_extract_jobs_nested_data():
    data = {"data": {"jobs": [{"title": "y"}]}}
    assert _extract_jobs_array(data) == [{"title": "y"}]


def test_extract_jobs_empty():
    assert _extract_jobs_array({}) == []
    assert _extract_jobs_array({"foo": "bar"}) == []
    assert _extract_jobs_array(None) == []  # type: ignore[arg-type]


def test_extract_jobs_filters_non_dict_items():
    data = {"jobs": [{"title": "a"}, "stringy", 123]}
    assert _extract_jobs_array(data) == [{"title": "a"}]


# ---------------------------------------------------------------------------
# _extract_company_display
# ---------------------------------------------------------------------------


def test_extract_company_display_top_level_name():
    assert _extract_company_display({"name": "Ramp"}, fallback="x") == "Ramp"


def test_extract_company_display_nested_jobboard():
    data = {"jobBoard": {"name": "Linear"}}
    assert _extract_company_display(data, fallback="x") == "Linear"


def test_extract_company_display_falls_back_to_slug():
    assert _extract_company_display({}, fallback="my-slug") == "my-slug"


# ---------------------------------------------------------------------------
# _to_job
# ---------------------------------------------------------------------------


def test_to_job_basic():
    raw = {
        "id": "post-1",
        "title": "Senior Backend Engineer",
        "jobUrl": "https://jobs.ashbyhq.com/ramp/abc",
        "locationName": "New York, NY",
        "publishedDate": "2026-05-01T00:00:00Z",
    }
    job = _to_job(raw, slug="ramp", company_display="Ramp")
    assert job is not None
    assert job.role_title == "Senior Backend Engineer"
    assert job.seniority_level == "senior"
    assert job.primary_url == raw["jobUrl"]
    assert job.location_raw == "New York, NY"
    assert job.source_repo == "ashby:ramp"
    assert job.sources == ["ashby"]
    assert job.date_posted_raw == raw["publishedDate"]


def test_to_job_falls_back_url_keys():
    raw = {
        "title": "SWE",
        "descriptionUrl": "https://jobs.ashbyhq.com/x/desc",
    }
    job = _to_job(raw, slug="x", company_display="X")
    assert job is not None
    assert job.primary_url == raw["descriptionUrl"]


def test_to_job_handles_epoch_published_date():
    raw = {
        "title": "Engineer",
        "jobUrl": "https://jobs.ashbyhq.com/x/1",
        "publishedDate": 1714521600000,
    }
    job = _to_job(raw, slug="x", company_display="X")
    assert job is not None
    assert job.date_posted_raw is not None


def test_to_job_skips_missing_url():
    assert _to_job({"title": "SWE"}, slug="x", company_display="X") is None


def test_to_job_skips_missing_title():
    raw = {"jobUrl": "https://jobs.ashbyhq.com/x/1"}
    assert _to_job(raw, slug="x", company_display="X") is None


def test_to_job_returns_none_for_non_dict():
    assert _to_job("oops", slug="x", company_display="X") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scrape_ashby_company — full flow
# ---------------------------------------------------------------------------


def _mock_client(*, status: int = 200, payload=None,
                 raise_exc: Exception | None = None) -> httpx.Client:
    cli = MagicMock(spec=httpx.Client)
    if raise_exc is not None:
        cli.get.side_effect = raise_exc
    else:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        resp.json.return_value = payload if payload is not None else {}
        cli.get.return_value = resp
    return cli


def test_scrape_ashby_company_flat_shape():
    payload = {
        "name": "Ramp",
        "jobs": [
            {
                "title": "Senior SWE",
                "jobUrl": "https://jobs.ashbyhq.com/ramp/1",
                "locationName": "NYC",
            },
            {
                "title": "Staff Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/ramp/2",
                "locationName": "Remote",
            },
        ],
    }
    cli = _mock_client(payload=payload)
    jobs = scrape_ashby_company("ramp", client=cli)
    assert len(jobs) == 2
    titles = {j.role_title for j in jobs}
    assert "Senior SWE" in titles
    assert "Staff Engineer" in titles


def test_scrape_ashby_company_nested_shape():
    payload = {
        "jobBoard": {
            "name": "Linear",
            "jobs": [
                {"title": "Engineer", "jobUrl": "https://jobs.ashbyhq.com/linear/1"},
            ],
        }
    }
    cli = _mock_client(payload=payload)
    jobs = scrape_ashby_company("linear", client=cli)
    assert len(jobs) == 1


def test_scrape_ashby_company_returns_empty_on_404():
    cli = _mock_client(status=404)
    assert scrape_ashby_company("x", client=cli) == []


def test_scrape_ashby_company_returns_empty_on_http_error():
    cli = _mock_client(raise_exc=httpx.ConnectError("boom"))
    assert scrape_ashby_company("x", client=cli) == []


def test_scrape_ashby_company_returns_empty_on_bad_json():
    cli = MagicMock(spec=httpx.Client)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.side_effect = ValueError("nope")
    cli.get.return_value = resp
    assert scrape_ashby_company("x", client=cli) == []


def test_scrape_ashby_company_dedups_within_response():
    raw = {"title": "SWE", "jobUrl": "https://jobs.ashbyhq.com/x/1"}
    payload = {"name": "X", "jobs": [raw, dict(raw)]}
    cli = _mock_client(payload=payload)
    jobs = scrape_ashby_company("x", client=cli)
    assert len(jobs) == 1


def test_scrape_ashby_company_respects_max_jobs():
    payload = {
        "name": "X",
        "jobs": [
            {"title": f"E{i}", "jobUrl": f"https://jobs.ashbyhq.com/x/{i}"}
            for i in range(15)
        ],
    }
    cli = _mock_client(payload=payload)
    jobs = scrape_ashby_company("x", client=cli, max_jobs=3)
    assert len(jobs) == 3


# ---------------------------------------------------------------------------
# scrape_ashby_direct — sweep
# ---------------------------------------------------------------------------


def test_scrape_ashby_direct_iterates():
    payload = {
        "name": "X",
        "jobs": [{"title": "SWE", "jobUrl": "https://jobs.ashbyhq.com/x/1"}],
    }
    cli = _mock_client(payload=payload)
    jobs = scrape_ashby_direct(
        client=cli, companies=["a", "b"], inter_company_delay=0
    )
    assert len(jobs) >= 1
    assert all(j.sources == ["ashby"] for j in jobs)


def test_ashby_companies_list_is_nonempty():
    assert len(ASHBY_COMPANIES) >= 15
    assert "ramp" in ASHBY_COMPANIES
    assert SOURCE_NAME == "ashby"
