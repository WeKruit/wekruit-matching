"""End-to-end assertions for the latest 1K jobs in the JD pipeline."""
from __future__ import annotations

import os

import httpx
import psycopg
import pytest
import pytest_asyncio  # noqa: F401 — ensures pytest-asyncio plugin is registered
from psycopg.rows import dict_row

from wekruit_matching.pipeline.ats_enricher import (
    fetch_ashby_job,
    fetch_greenhouse_job,
    fetch_lever_job,
)
from wekruit_matching.pipeline.firecrawl_enricher import fetch_workday_job


def _connect():
    url = os.environ.get("DATABASE_URL", "").replace("postgresql+psycopg://", "postgresql://", 1)
    if not url or url == "postgresql://":
        pytest.skip("DATABASE_URL not set — skipping JD pipeline e2e checks")
    try:
        return psycopg.connect(url, row_factory=dict_row)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Cannot connect to DB: {exc}")


@pytest.mark.integration
def test_latest_1000_jobs_have_greenhouse_lever_and_ashby_jd_examples():
    """Latest 1K jobs should include at least one enriched example from each free ATS.

    This is a prod-data-quality assertion, not a code test: it can only pass
    against a populated corpus. On a fresh/empty DB (CI's ephemeral *_test) there
    are no greenhouse/lever/ashby rows, so we skip rather than fail — otherwise
    the CI gate would ship permanently red and get ignored.
    """
    with _connect() as conn:
        present = conn.execute(
            """
            SELECT count(*) AS n FROM jobs
            WHERE primary_url LIKE '%greenhouse%'
               OR primary_url LIKE '%lever%'
               OR primary_url LIKE '%ashbyhq%'
            """
        ).fetchone()
        if not present or present["n"] == 0:
            pytest.skip(
                "no greenhouse/lever/ashby jobs in corpus — needs a populated "
                "prod-data DB (empty CI DB has none)"
            )
        row = conn.execute(
            """
            WITH latest AS (
              SELECT *
              FROM jobs
              ORDER BY first_seen_at DESC
              LIMIT 1000
            )
            SELECT
              COUNT(*) FILTER (
                WHERE primary_url LIKE '%greenhouse%'
                  AND job_description IS NOT NULL
                  AND job_description != ''
                  AND data_quality_score IS NOT NULL
              ) AS greenhouse_ready,
              COUNT(*) FILTER (
                WHERE primary_url LIKE '%lever%'
                  AND job_description IS NOT NULL
                  AND job_description != ''
                  AND data_quality_score IS NOT NULL
              ) AS lever_ready,
              COUNT(*) FILTER (
                WHERE primary_url LIKE '%ashbyhq%'
                  AND job_description IS NOT NULL
                  AND job_description != ''
                  AND data_quality_score IS NOT NULL
              ) AS ashby_ready
            FROM latest
            """
        ).fetchone()

    assert row["greenhouse_ready"] > 0
    assert row["lever_ready"] > 0
    assert row["ashby_ready"] > 0


# ---------------------------------------------------------------------------
# PARSE-05: Workday CXS unit test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_workday_job_returns_ats_job_data_with_description() -> None:
    """PARSE-05: Workday CXS fetcher must return AtsJobData with description_plain and source='workday'."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # GET returns HTML page with embedded CXS path
            return httpx.Response(
                200,
                text='<html><body>/wday/cxs/acme/careers/jobs</body></html>',
            )
        if request.method == "POST":
            # POST CXS endpoint returns job postings list
            return httpx.Response(
                200,
                json={
                    "jobPostings": [
                        {
                            "externalPath": "/en-US/job/123",
                            "title": "Intern",
                            "jobDescription": "Build ML systems for new grads.",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_workday_job(
            "https://acme.myworkdayjobs.com/careers/en-US/job/123",
            client=client,
        )

    assert result is not None
    assert len(result.description_plain) > 0
    assert result.source == "workday"


# ---------------------------------------------------------------------------
# PARSE-02 / PARSE-03 / PARSE-04: Parametrized ATS fetcher validation
# ---------------------------------------------------------------------------

def _greenhouse_client() -> httpx.Client:
    """Build a mock httpx.Client for Greenhouse API."""
    payloads = {
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123?content=true": {
            "content": "Build APIs.",
            "departments": [],
            "offices": [],
            "metadata": [],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = payloads.get(str(request.url))
        if payload is None:
            return httpx.Response(404, json={"error": f"unexpected url {request.url}"})
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _lever_client() -> httpx.Client:
    """Build a mock httpx.Client for Lever API."""
    payloads = {
        "https://api.lever.co/v0/postings/acme/abc123": {
            "descriptionPlain": "Build tooling.",
            "categories": {},
            "lists": [],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = payloads.get(str(request.url))
        if payload is None:
            return httpx.Response(404, json={"error": f"unexpected url {request.url}"})
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _ashby_client() -> httpx.Client:
    """Build a mock httpx.Client for Ashby API."""
    payloads = {
        "https://api.ashbyhq.com/posting-api/job-board/Acme?includeCompensation=true": {
            "jobs": [
                {
                    "title": "ML Intern",
                    "location": "Remote",
                    "department": "AI",
                    "workplaceType": "Remote",
                    "descriptionPlain": "Train ranking models.",
                    "employmentType": "Intern",
                    "jobUrl": "https://jobs.ashbyhq.com/Acme/role-456",
                    "applyUrl": "https://jobs.ashbyhq.com/Acme/application/role-456",
                    "compensation": {},
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = payloads.get(str(request.url))
        if payload is None:
            return httpx.Response(404, json={"error": f"unexpected url {request.url}"})
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "fetch_fn,url,client_fn",
    [
        (
            fetch_greenhouse_job,
            "https://boards.greenhouse.io/acme/jobs/123",
            _greenhouse_client,
        ),
        (
            fetch_lever_job,
            "https://jobs.lever.co/acme/abc123",
            _lever_client,
        ),
        (
            fetch_ashby_job,
            "https://jobs.ashbyhq.com/Acme/role-456",
            _ashby_client,
        ),
    ],
    ids=["greenhouse", "lever", "ashby"],
)
def test_all_ats_fetchers_produce_non_empty_description_plain(fetch_fn, url, client_fn) -> None:
    """PARSE-02/03/04: Each free ATS fetcher must return non-empty description_plain and correct source."""
    with client_fn() as client:
        result = fetch_fn(url, client=client)

    assert result.description_plain != "", (
        f"{fetch_fn.__name__} returned empty description_plain"
    )
    assert result.source in {"greenhouse", "lever", "ashby"}, (
        f"Unexpected source: {result.source!r}"
    )
    assert result.data_quality_score >= 0
