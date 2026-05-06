"""Unit tests for Phase 63 — Wellfound scraper."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from wekruit_matching.scraper.wellfound import (
    SOURCE_REPO_SLUG,
    _normalize_json_items,
    _to_job,
    scrape_wellfound,
)
from wekruit_matching.scraper.title_inference import infer_seniority as _infer_seniority


# ---------------------------------------------------------------------------
# _infer_seniority
# ---------------------------------------------------------------------------


def test_infer_seniority_senior():
    assert _infer_seniority("Senior Software Engineer") == "senior"
    # Pattern matches "Sr. <engineer|developer|analyst|...>" directly,
    # mirroring Phase 57 backfill behavior.
    assert _infer_seniority("Sr. Engineer") == "senior"


def test_infer_seniority_staff_principal():
    assert _infer_seniority("Staff Software Engineer") == "staff"
    assert _infer_seniority("Principal Architect") == "principal"


def test_infer_seniority_default_mid_level():
    assert _infer_seniority("Software Engineer") == "mid_level"


def test_infer_seniority_empty():
    assert _infer_seniority("") == "mid_level"
    assert _infer_seniority(None) == "mid_level"


# ---------------------------------------------------------------------------
# _normalize_json_items
# ---------------------------------------------------------------------------


def test_normalize_json_items_extracts_fields():
    items = [
        {
            "title": "Senior Software Engineer",
            "startup": {"name": "Acme Corp"},
            "location": "San Francisco, CA",
            "apply_url": "/jobs/12345",
            "posted_date": "2026-05-01",
        }
    ]
    out = _normalize_json_items(items)
    assert len(out) == 1
    assert out[0]["title"] == "Senior Software Engineer"
    assert out[0]["company"] == "Acme Corp"
    assert out[0]["apply_url"].endswith("/jobs/12345")


def test_normalize_json_items_skips_missing_fields():
    items = [
        {"title": "Engineer"},  # no company
        {"company": "Acme"},  # no title
        {},  # empty
    ]
    out = _normalize_json_items(items)
    assert out == []


def test_normalize_json_items_handles_list_location():
    items = [
        {
            "title": "Engineer",
            "company": "Acme",
            "location": ["NYC", "Remote"],
            "apply_url": "/jobs/1",
        }
    ]
    out = _normalize_json_items(items)
    assert "NYC" in out[0]["location"]


# ---------------------------------------------------------------------------
# _to_job
# ---------------------------------------------------------------------------


def test_to_job_returns_job_with_sources_wellfound():
    raw = {
        "title": "Senior Software Engineer",
        "company": "Acme Corp",
        "apply_url": "https://wellfound.com/jobs/12345",
        "location": "Remote",
        "posted_date": None,
    }
    job = _to_job(raw)
    assert job is not None
    assert job.sources == [SOURCE_REPO_SLUG]
    assert job.source_repo == SOURCE_REPO_SLUG
    assert job.role_title == "Senior Software Engineer"
    assert job.seniority_level == "senior"


def test_to_job_skips_when_title_missing():
    raw = {"title": "", "company": "Acme", "apply_url": "https://x.com"}
    assert _to_job(raw) is None


def test_to_job_skips_when_company_missing():
    raw = {"title": "Senior Engineer", "company": "", "apply_url": "https://x.com"}
    assert _to_job(raw) is None


# ---------------------------------------------------------------------------
# scrape_wellfound — end-to-end with mocked httpx
# ---------------------------------------------------------------------------


def _make_mock_client(json_response: dict | None = None, html_response: str = ""):
    cli = MagicMock(spec=httpx.Client)
    json_resp = MagicMock()
    json_resp.status_code = 200 if json_response is not None else 404
    json_resp.json.return_value = json_response or {}
    json_resp.text = json.dumps(json_response) if json_response else ""

    html_resp = MagicMock()
    html_resp.status_code = 200
    html_resp.text = html_response

    # Two GET calls expected: JSON then HTML fallback
    cli.get.side_effect = [json_resp, html_resp]
    return cli


def test_scrape_wellfound_uses_json_when_available(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    json_resp = {
        "jobs": [
            {
                "title": "Senior Software Engineer",
                "startup": {"name": "Acme"},
                "location": "Remote",
                "apply_url": "/jobs/abc",
                "posted_date": None,
            },
            {
                "title": "Staff Backend Engineer",
                "startup": {"name": "Globex"},
                "location": "NYC",
                "apply_url": "/jobs/xyz",
                "posted_date": None,
            },
        ]
    }
    cli = _make_mock_client(json_response=json_resp)
    jobs = scrape_wellfound(client=cli)
    assert len(jobs) == 2
    assert all(j.sources == ["wellfound"] for j in jobs)
    assert jobs[0].seniority_level == "senior"
    assert jobs[1].seniority_level == "staff"


def test_scrape_wellfound_respects_max_jobs(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    json_resp = {
        "jobs": [
            {
                "title": f"Senior Engineer {i}",
                "startup": {"name": f"Co{i}"},
                "location": "",
                "apply_url": f"/jobs/{i}",
            }
            for i in range(20)
        ]
    }
    cli = _make_mock_client(json_response=json_resp)
    jobs = scrape_wellfound(client=cli, max_jobs=5)
    assert len(jobs) == 5


def test_scrape_wellfound_returns_empty_on_total_failure(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    cli = MagicMock(spec=httpx.Client)
    cli.get.side_effect = httpx.HTTPError("network down")
    jobs = scrape_wellfound(client=cli)
    assert jobs == []


def test_scrape_wellfound_json_handles_no_jobs_key(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    cli = _make_mock_client(json_response={"otherKey": []}, html_response="")
    jobs = scrape_wellfound(client=cli)
    assert jobs == []
