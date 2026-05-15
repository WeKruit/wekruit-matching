"""Unit tests for Phase A1 — YC scraper (jobs + companies directory).

Fixture-based: no network, no DB. Mirrors the shape of
``tests/test_scraper_wellfound.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from wekruit_matching.scraper.yc import (
    SOURCE_REPO_BARE,
    _DATA_PAGE_REGEX,
    _extract_job_postings,
    _resolve_primary_url,
    _resolve_source_repo,
    _to_job,
    fetch_yc_companies,
    scrape_yc,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures — keep small + canonical. We deliberately mirror the
# real YC Inertia.js payload shape but trimmed to the fields the scraper
# reads, so tests stay decoupled from cosmetic YC payload churn.
# ---------------------------------------------------------------------------


def _build_inertia_html(postings: list[dict]) -> str:
    """Wrap the postings list in YC's Inertia.js root div, html-escaping
    the JSON exactly the way the real page does."""
    page_obj = {
        "component": "WaasLandingPage",
        "props": {"jobPostings": postings, "jobsCount": "thousands"},
    }
    raw = json.dumps(page_obj)
    # YC escapes ", &, <, > — minimal subset we need is " → &quot;
    escaped = (
        raw.replace("&", "&amp;")
           .replace('"', "&quot;")
           .replace("<", "&lt;")
           .replace(">", "&gt;")
    )
    return f'<!DOCTYPE html><html><body><div id="app" data-page="{escaped}"></div></body></html>'


SAMPLE_POSTINGS: list[dict] = [
    {
        "id": 94332,
        "title": "Mobile Engineer (Android)",
        "url": "/companies/circle-medical/jobs/onMKAG9-mobile-engineer-android",
        "applyUrl": "https://account.ycombinator.com/authenticate?continue=foo",
        "location": "Montreal, QC, CA / Remote (US; CA)",
        "companyName": "Circle Medical",
        "companyBatchName": "S15",
        "createdAt": "9 days",
    },
    {
        "id": 94311,
        "title": "Senior Software Engineer",
        "url": "/companies/nova-credit/jobs/I1dQrYC-senior-software-engineer",
        "applyUrl": "https://account.ycombinator.com/authenticate?continue=bar",
        "location": "Remote / New York, NY, US",
        "companyName": "Nova Credit",
        "companyBatchName": "W16",
        "createdAt": "3 days",
    },
    {
        "id": 94999,
        "title": "Founding Engineer",
        "url": "/companies/example-co/jobs/ABCdef-founding-engineer",
        "applyUrl": "https://account.ycombinator.com/authenticate?continue=baz",
        "location": "San Francisco, CA, US",
        "companyName": "🚀 Example Co",
        "companyBatchName": "",  # un-batched → bare 'yc'
        "createdAt": "1 day",
    },
]


# ---------------------------------------------------------------------------
# _extract_job_postings
# ---------------------------------------------------------------------------


def test_extract_job_postings_parses_inertia_data_page():
    html = _build_inertia_html(SAMPLE_POSTINGS)
    postings = _extract_job_postings(html)
    assert len(postings) == 3
    assert postings[0]["companyName"] == "Circle Medical"
    assert postings[1]["companyBatchName"] == "W16"


def test_extract_job_postings_returns_empty_when_marker_absent():
    assert _extract_job_postings("<html><body>no data-page here</body></html>") == []


def test_extract_job_postings_returns_empty_on_malformed_json():
    bad = '<div data-page="{not valid json"></div>'
    assert _extract_job_postings(bad) == []


def test_extract_job_postings_returns_empty_when_jobpostings_missing():
    html = '<div data-page="{&quot;props&quot;:{}}"></div>'
    assert _extract_job_postings(html) == []


def test_extract_job_postings_skips_non_dict_entries():
    html = _build_inertia_html([{"title": "ok", "companyName": "Co"}, "bogus", None])  # type: ignore[list-item]
    postings = _extract_job_postings(html)
    assert len(postings) == 1


def test_data_page_regex_matches_real_yc_shape():
    # Sanity: the regex tolerates escaped quotes (\\\") inside the JSON.
    sample = r'<div data-page="{&quot;a&quot;:&quot;b\&quot;c&quot;}"></div>'
    assert _DATA_PAGE_REGEX.search(sample) is not None


# ---------------------------------------------------------------------------
# _resolve_source_repo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch,expected", [
    ("W25", "yc:W25"),
    ("S24", "yc:S24"),
    ("IK12", "yc:IK12"),
    ("  w25  ", "yc:W25"),   # trim + uppercase
    ("", "yc"),
    (None, "yc"),
    ("???", "yc"),           # all-punct → fall back to bare
])
def test_resolve_source_repo(batch, expected):
    assert _resolve_source_repo({"companyBatchName": batch}) == expected


# ---------------------------------------------------------------------------
# _resolve_primary_url
# ---------------------------------------------------------------------------


def test_resolve_primary_url_prefers_canonical_path():
    raw = {
        "url": "/companies/foo/jobs/x-engineer",
        "applyUrl": "https://account.ycombinator.com/authenticate?x=1",
    }
    assert _resolve_primary_url(raw) == "https://www.ycombinator.com/companies/foo/jobs/x-engineer"


def test_resolve_primary_url_falls_back_to_apply_url():
    raw = {"applyUrl": "https://account.ycombinator.com/authenticate?x=1"}
    assert _resolve_primary_url(raw) == "https://account.ycombinator.com/authenticate?x=1"


def test_resolve_primary_url_none_when_nothing_usable():
    assert _resolve_primary_url({"applyUrl": "not-a-url"}) is None
    assert _resolve_primary_url({}) is None


# ---------------------------------------------------------------------------
# _to_job
# ---------------------------------------------------------------------------


def test_to_job_emits_yc_source_repo_with_batch():
    job = _to_job(SAMPLE_POSTINGS[0])
    assert job is not None
    assert job.source_repo == "yc:S15"
    assert job.sources == [SOURCE_REPO_BARE]
    assert job.company_name == "circle medical"
    assert job.role_title == "Mobile Engineer (Android)"
    assert job.primary_url and job.primary_url.startswith("https://www.ycombinator.com")
    assert job.content_hash and len(job.content_hash) == 64
    assert job.location_raw.startswith("Montreal")


def test_to_job_falls_back_to_bare_yc_when_no_batch():
    job = _to_job(SAMPLE_POSTINGS[2])
    assert job is not None
    # un-batched listing → bare yc, with normalized company (emoji stripped)
    assert job.source_repo == "yc"
    assert job.company_name == "example co"


def test_to_job_returns_none_when_title_missing():
    raw = dict(SAMPLE_POSTINGS[0]); raw["title"] = ""
    assert _to_job(raw) is None


def test_to_job_returns_none_when_company_missing():
    raw = dict(SAMPLE_POSTINGS[0]); raw["companyName"] = ""
    assert _to_job(raw) is None


def test_to_job_id_stable_across_calls():
    a = _to_job(SAMPLE_POSTINGS[0])
    b = _to_job(SAMPLE_POSTINGS[0])
    assert a and b and a.job_id == b.job_id


def test_to_job_id_changes_with_batch():
    raw = dict(SAMPLE_POSTINGS[0])
    a = _to_job(raw)
    raw["companyBatchName"] = "W26"
    b = _to_job(raw)
    assert a and b and a.job_id != b.job_id


# ---------------------------------------------------------------------------
# scrape_yc — end-to-end with mocked httpx
# ---------------------------------------------------------------------------


def _mock_jobs_client(html_body: str, status: int = 200) -> MagicMock:
    cli = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = status
    resp.text = html_body
    cli.get.return_value = resp
    return cli


def test_scrape_yc_parses_jobs_from_inertia_html(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    cli = _mock_jobs_client(_build_inertia_html(SAMPLE_POSTINGS))
    jobs = scrape_yc(client=cli)
    assert len(jobs) == 3
    assert {j.source_repo for j in jobs} == {"yc:S15", "yc:W16", "yc"}
    assert all(j.sources == [SOURCE_REPO_BARE] for j in jobs)


def test_scrape_yc_dedups_by_job_id(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    dupes = [SAMPLE_POSTINGS[0], SAMPLE_POSTINGS[0]]
    cli = _mock_jobs_client(_build_inertia_html(dupes))
    jobs = scrape_yc(client=cli)
    assert len(jobs) == 1


def test_scrape_yc_returns_empty_on_non_200(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    cli = _mock_jobs_client("", status=503)
    assert scrape_yc(client=cli) == []


def test_scrape_yc_returns_empty_on_network_failure(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    cli = MagicMock(spec=httpx.Client)
    cli.get.side_effect = httpx.HTTPError("network down")
    assert scrape_yc(client=cli) == []


def test_scrape_yc_returns_empty_when_postings_absent(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    cli = _mock_jobs_client('<html><body>no data-page</body></html>')
    assert scrape_yc(client=cli) == []


# ---------------------------------------------------------------------------
# fetch_yc_companies — fixture-based pagination
# ---------------------------------------------------------------------------


def _mock_companies_client(pages: list[dict]) -> MagicMock:
    """Mock httpx.Client.get whose successive calls return the given JSON pages."""
    cli = MagicMock(spec=httpx.Client)
    responses = []
    for p in pages:
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = p
        responses.append(r)
    cli.get.side_effect = responses
    return cli


def test_fetch_yc_companies_walks_all_pages(monkeypatch, tmp_path):
    monkeypatch.setattr("time.sleep", lambda _: None)
    pages = [
        {"companies": [{"id": 1, "name": "A", "batch": "W25"}], "totalPages": 2,
         "page": 1, "nextPage": "https://api.ycombinator.com/v0.1/companies?page=2"},
        {"companies": [{"id": 2, "name": "B", "batch": "S24"}], "totalPages": 2,
         "page": 2, "nextPage": None},
    ]
    cli = _mock_companies_client(pages)
    cache = tmp_path / "yc-companies-cache.json"
    out = fetch_yc_companies(client=cli, cache_path=cache, max_pages=10)
    assert len(out) == 2
    assert {c["name"] for c in out} == {"A", "B"}

    snap = json.loads(cache.read_text())
    assert snap["company_count"] == 2
    assert snap["total_pages_seen"] == 2
    assert snap["last_ok_page"] == 2
    assert [c["name"] for c in snap["companies"]] == ["A", "B"]


def test_fetch_yc_companies_stops_at_max_pages(monkeypatch, tmp_path):
    monkeypatch.setattr("time.sleep", lambda _: None)
    pages = [
        {"companies": [{"id": i, "name": f"C{i}", "batch": "W25"}], "totalPages": 5,
         "page": i, "nextPage": f"https://api.ycombinator.com/v0.1/companies?page={i+1}"}
        for i in range(1, 6)
    ]
    cli = _mock_companies_client(pages)
    cache = tmp_path / "yc-cap.json"
    out = fetch_yc_companies(client=cli, cache_path=cache, max_pages=2)
    assert len(out) == 2


def test_fetch_yc_companies_handles_partial_failure(monkeypatch, tmp_path):
    """If page 2 errors, we keep page 1 and persist what we have."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    good = MagicMock(); good.status_code = 200
    good.json.return_value = {
        "companies": [{"id": 1, "name": "A", "batch": "W25"}],
        "totalPages": 3, "page": 1,
        "nextPage": "https://api.ycombinator.com/v0.1/companies?page=2",
    }
    bad = MagicMock(); bad.status_code = 500
    cli = MagicMock(spec=httpx.Client)
    cli.get.side_effect = [good, bad]
    cache = tmp_path / "partial.json"
    out = fetch_yc_companies(client=cli, cache_path=cache, max_pages=10)
    assert len(out) == 1
    snap = json.loads(cache.read_text())
    assert snap["last_ok_page"] == 1
    assert snap["company_count"] == 1


def test_fetch_yc_companies_empty_when_first_page_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("time.sleep", lambda _: None)
    cli = MagicMock(spec=httpx.Client)
    cli.get.side_effect = httpx.HTTPError("boom")
    cache = tmp_path / "empty.json"
    out = fetch_yc_companies(client=cli, cache_path=cache, max_pages=3)
    assert out == []
    # We still write an empty cache so the consumer doesn't read a stale file
    snap = json.loads(cache.read_text())
    assert snap["company_count"] == 0


# ---------------------------------------------------------------------------
# Real-shape spot check — load the trimmed real Inertia fixture from disk
# and confirm the scraper still parses it cleanly. Belt-and-suspenders for
# the synthetic _build_inertia_html() helper above.
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
YC_FIXTURE = FIXTURE_DIR / "yc_jobs_page.html"


@pytest.mark.skipif(not YC_FIXTURE.exists(), reason="real-shape fixture missing")
def test_real_shape_yc_jobs_fixture_parses(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    html = YC_FIXTURE.read_text()
    cli = _mock_jobs_client(html)
    jobs = scrape_yc(client=cli)
    assert len(jobs) >= 1
    for j in jobs:
        assert j.source_repo.startswith("yc")
        assert j.sources == [SOURCE_REPO_BARE]
        assert j.role_title and j.company_name


YC_COMPANIES_FIXTURE = FIXTURE_DIR / "yc_companies_page.json"


@pytest.mark.skipif(not YC_COMPANIES_FIXTURE.exists(), reason="companies fixture missing")
def test_real_shape_yc_companies_fixture_parses(monkeypatch, tmp_path):
    monkeypatch.setattr("time.sleep", lambda _: None)
    payload = json.loads(YC_COMPANIES_FIXTURE.read_text())
    # Force totalPages=1 + nextPage=None on whatever the fixture is so we
    # exit cleanly after one mock call.
    payload["totalPages"] = 1
    payload["nextPage"] = None
    r = MagicMock(); r.status_code = 200; r.json.return_value = payload
    cli = MagicMock(spec=httpx.Client); cli.get.return_value = r
    cache = tmp_path / "real.json"
    out = fetch_yc_companies(client=cli, cache_path=cache, max_pages=1)
    assert len(out) == len(payload["companies"])
