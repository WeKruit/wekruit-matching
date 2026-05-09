"""Unit tests for free ATS parsers and normalization."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from wekruit_matching.pipeline.ats_enricher import (
    calculate_data_quality_score,
    fetch_ashby_job,
    fetch_greenhouse_job,
    fetch_lever_job,
    normalize_text,
)


def _client_with_payloads(payloads: dict[str, dict]) -> httpx.Client:
    """Build an HTTPX client backed by deterministic JSON payloads."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = payloads.get(str(request.url))
        if payload is None:
            return httpx.Response(404, json={"error": f"unexpected url {request.url}"})
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_normalize_text_removes_html_entities_zero_width_and_normalizes_unicode() -> None:
    """ATS text should be plain, whitespace-normalized, and unicode-normalized."""
    raw = "Hello&nbsp;\u200bworld <b>team</b>  \n Ａ"
    assert normalize_text(raw) == "Hello world team A"


def test_fetch_greenhouse_job_maps_content_department_location_and_salary() -> None:
    """Greenhouse content API responses should map onto the canonical JD fields."""
    client = _client_with_payloads(
        {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123?content=true": {
                "title": "Software Engineer",
                "content": (
                    "<p>Build APIs &amp; tooling.</p>"
                    "<ul><li>Ship backend features</li></ul>"
                ),
                "updated_at": "2026-03-20T12:00:00Z",
                "departments": [{"name": "Engineering"}],
                "offices": [{"name": "Austin, TX"}],
                "metadata": [
                    {"name": "Salary Range", "value": "$120,000 - $150,000"},
                ],
            }
        }
    )

    result = fetch_greenhouse_job(
        "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123",
        client=client,
    )

    assert result.description_plain == "Build APIs & tooling. Ship backend features"
    assert result.department == "Engineering"
    assert result.location == "Austin, TX"
    assert result.salary_range == "$120,000 - $150,000"
    assert result.source == "greenhouse"
    assert result.data_quality_score > 0


def test_fetch_lever_job_maps_lists_salary_and_workplace_type() -> None:
    """Lever hosted postings should preserve description lists and compensation."""
    client = _client_with_payloads(
        {
            "https://api.lever.co/v0/postings/acme/abc123": {
                "text": "Backend Engineer",
                "descriptionPlain": "Build APIs for new grads.\n",
                "categories": {
                    "team": "Platform",
                    "location": "Remote (US)",
                    "commitment": "Full-time",
                },
                "workplaceType": "remote",
                "salaryRange": {
                    "currency": "USD",
                    "interval": "yearly",
                    "min": 140000,
                    "max": 160000,
                },
                "createdAt": 1761955200000,
                "lists": [
                    {
                        "text": "Responsibilities",
                        "content": ["Own backend systems", "Ship APIs"],
                    },
                    {
                        "text": "Requirements",
                        "content": ["Python", "Postgres"],
                    },
                    {
                        "text": "Benefits",
                        "content": ["Medical", "401k"],
                    },
                ],
            }
        }
    )

    result = fetch_lever_job("https://jobs.lever.co/acme/abc123", client=client)

    assert result.description_plain == "Build APIs for new grads."
    assert result.department == "Platform"
    assert result.location == "Remote (US)"
    assert result.workplace_type == "remote"
    assert result.employment_type == "Full-time"
    assert result.salary_range == "USD 140000-160000 yearly"
    assert result.core_responsibilities == ["Own backend systems", "Ship APIs"]
    assert result.qualifications == ["Python", "Postgres"]
    assert result.benefits == ["Medical", "401k"]
    assert result.source == "lever"


def test_fetch_ashby_job_maps_compensation_and_employment_type() -> None:
    """Ashby job board API responses should match by hosted job URL and map compensation."""
    client = _client_with_payloads(
        {
            "https://api.ashbyhq.com/posting-api/job-board/Acme?includeCompensation=true": {
                "jobs": [
                    {
                        "title": "Machine Learning Intern",
                        "location": "San Francisco, CA",
                        "department": "Applied AI",
                        "team": "Research",
                        "workplaceType": "Hybrid",
                        "descriptionPlain": "Train and evaluate ranking models.",
                        "publishedAt": "2026-03-25T15:00:00Z",
                        "employmentType": "Intern",
                        "jobUrl": "https://jobs.ashbyhq.com/Acme/role-123",
                        "applyUrl": "https://jobs.ashbyhq.com/Acme/application/role-123",
                        "compensation": {
                            "scrapeableCompensationSalarySummary": "$45/hr - $55/hr"
                        },
                    }
                ]
            }
        }
    )

    result = fetch_ashby_job("https://jobs.ashbyhq.com/Acme/role-123", client=client)

    assert result.description_plain == "Train and evaluate ranking models."
    assert result.department == "Applied AI"
    assert result.location == "San Francisco, CA"
    assert result.employment_type == "Intern"
    assert result.workplace_type == "Hybrid"
    assert result.salary_range == "$45/hr - $55/hr"
    assert result.source == "ashby"


def test_calculate_data_quality_score_uses_defined_weight_buckets() -> None:
    """Quality score should reflect completeness, recency, description length, and salary."""
    published_at = datetime.now(UTC) - timedelta(days=10)

    score = calculate_data_quality_score(
        description_plain="x" * 500,
        department="Engineering",
        location="Remote",
        employment_type="Intern",
        workplace_type="Hybrid",
        salary_range="$120k-$140k",
        published_at=published_at,
    )

    assert score == 100


# ---------------------------------------------------------------------------
# P7-L (2026-05-08) — Greenhouse embed/job_app?token=N support
#
# Boards in this shape (commonly emitted by Simplify and similar aggregators)
# carry only the job id in the query string. boards.greenhouse.io 301-
# redirects to job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>.
# We follow once to recover the board slug, then call the public boards-api.
# ---------------------------------------------------------------------------


def test_fetch_greenhouse_job_supports_embed_pattern_with_for_token():
    """When the URL already carries ``for=<board>&token=<id>``, no redirect
    follow is needed — we go straight to the boards-api.
    """
    import httpx
    from wekruit_matching.pipeline.ats_enricher import fetch_greenhouse_job

    seen_urls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "boards-api.greenhouse.io" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "content": "<p>Build the future of the embed widget.</p>",
                    "departments": [{"name": "Engineering"}],
                    "offices": [{"name": "Remote"}],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, follow_redirects=True)

    result = fetch_greenhouse_job(
        "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=12345",
        client=client,
    )

    assert result.source == "greenhouse"
    assert "embed widget" in result.description_plain
    # No redirect follow — direct boards-api call
    assert any(
        "boards-api.greenhouse.io/v1/boards/acme/jobs/12345" in u for u in seen_urls
    ), seen_urls


def test_fetch_greenhouse_job_resolves_embed_via_redirect():
    """When the URL only has ``token=<id>``, follow the 301 redirect to
    discover the ``for=<board>`` parameter, then call the boards-api.
    """
    import httpx
    from wekruit_matching.pipeline.ats_enricher import fetch_greenhouse_job

    seen_urls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        url = str(request.url)
        # Step 1: initial GET hits boards.greenhouse.io — return 301
        if (
            url.startswith("https://boards.greenhouse.io/embed/job_app")
            and "for=" not in url
        ):
            return httpx.Response(
                301,
                headers={
                    "Location": "https://job-boards.greenhouse.io/embed/job_app?for=unity3d&token=7902260"
                },
            )
        # Step 2: redirected GET — return a tiny HTML body so client doesn't error
        if "job-boards.greenhouse.io/embed/job_app" in url and "for=unity3d" in url:
            return httpx.Response(200, text="<html></html>")
        # Step 3: boards-api JSON
        if "boards-api.greenhouse.io/v1/boards/unity3d/jobs/7902260" in url:
            return httpx.Response(
                200,
                json={
                    "content": "<p>Resolved via embed redirect.</p>",
                    "departments": [],
                    "offices": [],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, follow_redirects=True)

    result = fetch_greenhouse_job(
        "https://boards.greenhouse.io/embed/job_app?token=7902260&utm_source=Simplify",
        client=client,
    )

    assert result.source == "greenhouse"
    assert "embed redirect" in result.description_plain
    # Must have followed the redirect AND called the boards-api with unity3d
    assert any(
        "boards-api.greenhouse.io/v1/boards/unity3d/jobs/7902260" in u for u in seen_urls
    ), seen_urls


def test_fetch_greenhouse_job_embed_without_token_raises_value_error():
    """An embed URL with no ``token`` param at all is malformed — raise the
    same ValueError as before so the caller marks it failed (recoverable).
    """
    import httpx
    import pytest as _pytest
    from wekruit_matching.pipeline.ats_enricher import fetch_greenhouse_job

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html></html>")
    )
    client = httpx.Client(transport=transport, follow_redirects=True)

    with _pytest.raises(ValueError, match="Unsupported Greenhouse job URL"):
        fetch_greenhouse_job(
            "https://boards.greenhouse.io/embed/job_app?ref=Simplify",
            client=client,
        )


def test_fetch_greenhouse_job_standard_pattern_unchanged():
    """The classic ``<board>/jobs/<id>`` shape must keep working — no
    regression from the embed support.
    """
    import httpx
    from wekruit_matching.pipeline.ats_enricher import fetch_greenhouse_job

    seen_urls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "boards-api.greenhouse.io/v1/boards/acme/jobs/123" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "content": "<p>Classic board page.</p>",
                    "departments": [],
                    "offices": [],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport, follow_redirects=True)

    result = fetch_greenhouse_job(
        "https://boards.greenhouse.io/acme/jobs/123",
        client=client,
    )

    assert result.source == "greenhouse"
    assert "Classic board page" in result.description_plain
    # No redirect lookups — straight to boards-api
    assert seen_urls == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123?content=true"
    ]
