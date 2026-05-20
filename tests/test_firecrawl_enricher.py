"""Unit tests for Workday and Firecrawl enrichment paths."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from wekruit_matching.pipeline.firecrawl_enricher import (
    _has_jd_content,
    fetch_firecrawl_job,
    fetch_workday_job,
    run_with_timeout,
    search_canonical_job_url,
)


def _async_client(handler) -> httpx.AsyncClient:
    """Return an async HTTPX client backed by a mock transport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_run_with_timeout_cancels_slow_coroutines() -> None:
    """Firecrawl calls must have an asyncio-level timeout independent of the SDK."""

    async def slow() -> None:
        await asyncio.sleep(0.05)

    with pytest.raises(asyncio.TimeoutError):
        await run_with_timeout(slow(), timeout_seconds=0.01)


def test_has_jd_content_requires_enough_length_and_job_keywords() -> None:
    """Markdown should only count as a JD when it is substantive and job-like."""
    assert not _has_jd_content("short text without signal")
    assert _has_jd_content(
        "Responsibilities\n"
        + ("Build backend systems for internships and new grads. " * 12)
    )


@pytest.mark.asyncio
async def test_fetch_workday_job_discovers_cxs_endpoint_and_maps_posting() -> None:
    """Workday pages should be resolved through the CXS endpoint before fallback."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text='<html><body><script>"/wday/cxs/acme/careers/jobs"</script></body></html>',
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "jobPostings": [
                        {
                            "title": "Software Engineer",
                            "externalPath": "/en-US/External/job/Austin-TX/Role_123",
                            "locationsText": "Austin, TX",
                            "postedOn": "2026-03-20T00:00:00Z",
                            "jobDescription": "<p>Build matching systems for students.</p>",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _async_client(handler) as client:
        result = await fetch_workday_job(
            "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Austin-TX/Role_123",
            client=client,
        )

    assert result is not None
    assert result.source == "workday"
    assert result.location == "Austin, TX"
    assert result.description_plain == "Build matching systems for students."


@pytest.mark.asyncio
async def test_fetch_firecrawl_job_uses_scrape_before_extract() -> None:
    """Good markdown should not trigger the expensive extract path."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("/v1/scrape"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": "Responsibilities\n"
                        + ("Build ranking systems and partner with product. " * 10)
                    },
                },
            )
        if str(request.url).endswith("/v1/extract"):
            return httpx.Response(500, json={"error": "extract should not be called"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _async_client(handler) as client:
        result = await fetch_firecrawl_job(
            "https://careers.example.com/jobs/1",
            api_key="fc-test",
            client=client,
        )

    assert result is not None
    assert result.job_data.source == "firecrawl"
    assert result.credits_used == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fetch_firecrawl_job_escalates_to_extract_when_scrape_is_insufficient() -> None:
    """Thin scrape markdown should escalate to extract exactly once."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/scrape"):
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": "Apply now"}},
            )
        if str(request.url).endswith("/v1/extract"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "job_description": "Own backend pipelines and partner with ops.",
                        "responsibilities": ["Own backend pipelines"],
                        "qualifications": ["Python"],
                        "salary_range": "$130k-$150k",
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with _async_client(handler) as client:
        result = await fetch_firecrawl_job(
            "https://careers.example.com/jobs/2",
            api_key="fc-test",
            client=client,
        )

    assert result is not None
    assert result.credits_used == 6
    assert result.job_data.description_plain == "Own backend pipelines and partner with ops."
    assert result.job_data.qualifications == ["Python"]


@pytest.mark.asyncio
async def test_search_canonical_job_url_skips_aggregators() -> None:
    """Search should prefer direct career sites over LinkedIn and aggregators."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"url": "https://www.linkedin.com/jobs/view/123"},
                    {"url": "https://careers.acme.com/jobs/backend-engineer"},
                ],
            },
        )

    async with _async_client(handler) as client:
        url = await search_canonical_job_url(
            company_name="Acme",
            role_title="Backend Engineer",
            api_key="fc-test",
            client=client,
        )

    assert url == "https://careers.acme.com/jobs/backend-engineer"
