"""Unit tests for ATS URL classification."""
from wekruit_matching.pipeline.url_classifier import (
    FetchRoute,
    classify_job_url,
    normalize_job_url,
)


def test_normalize_job_url_strips_query_and_fragment() -> None:
    """Tracking params and fragments should not affect routing."""
    assert (
        normalize_job_url(
            "https://jobs.lever.co/acme/123?utm_source=Simplify&ref=abc#apply"
        )
        == "https://jobs.lever.co/acme/123"
    )


def test_classify_greenhouse_variants() -> None:
    """Greenhouse hosts should route to the Greenhouse tier."""
    cases = [
        "https://boards.greenhouse.io/acme/jobs/12345",
        "https://job-boards.greenhouse.io/acme/jobs/12345?gh_src=abc",
    ]
    for url in cases:
        assert classify_job_url(url).route == FetchRoute.GREENHOUSE


def test_classify_lever_variants() -> None:
    """Lever hosts should route to the Lever tier."""
    cases = [
        "https://jobs.lever.co/acme/123",
        "https://jobs.eu.lever.co/acme/123?lever_source=Simplify",
    ]
    for url in cases:
        assert classify_job_url(url).route == FetchRoute.LEVER


def test_classify_ashby_variants() -> None:
    """Ashby hosts should route to the Ashby tier."""
    cases = [
        "https://jobs.ashbyhq.com/acme/7d2f3",
        "https://jobs.ashbyhq.com/acme/7d2f3?utm_campaign=x",
    ]
    for url in cases:
        assert classify_job_url(url).route == FetchRoute.ASHBY


def test_classify_workday_variants() -> None:
    """Known Workday domains should route to the Workday tier."""
    cases = [
        "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Austin-TX/Role_123",
        "https://acme.wd5.myworkdaysite.com/recruiting/acme/external/job/Remote/Role_123",
    ]
    for url in cases:
        assert classify_job_url(url).route == FetchRoute.WORKDAY


def test_classify_unknown_domain_falls_back_to_firecrawl() -> None:
    """Unknown hosts should route to the Firecrawl fallback tier."""
    result = classify_job_url("https://careers.example.com/jobs/software-engineer")
    assert result.route == FetchRoute.FIRECRAWL
    assert result.hostname == "careers.example.com"


def test_classify_blank_url_falls_back_to_firecrawl() -> None:
    """Empty or missing URLs should still produce a deterministic fallback."""
    result = classify_job_url("")
    assert result.route == FetchRoute.FIRECRAWL
    assert result.normalized_url == ""
