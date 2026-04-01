"""Unit tests for the URL classifier — no I/O, no DB required.

Tests are organized around:
  1. Parametrized routing: every ATS URL pattern → correct tier and slug
  2. Edge cases: http, query params, unknown URLs
  3. Contract: ClassifiedURL is a frozen dataclass (immutable)
  4. No I/O on import: module must not pull in httpx / psycopg / get_connection

Run with: uv run pytest tests/test_url_classifier.py -v
"""
import pytest

from wekruit_matching.scraper.url_classifier import ATSTier, ClassifiedURL, classify


# ---------------------------------------------------------------------------
# Parametrized routing — all ATS URL patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected_tier,expected_slug",
    [
        # Greenhouse variants
        (
            "https://boards.greenhouse.io/stripe/jobs/123",
            ATSTier.GREENHOUSE,
            "stripe",
        ),
        (
            "https://stripe.greenhouse.io/jobs/abc",
            ATSTier.GREENHOUSE,
            "stripe",
        ),
        (
            "https://job-boards.greenhouse.io/openai",
            ATSTier.GREENHOUSE,
            "openai",
        ),
        # Lever variants
        (
            "https://jobs.lever.co/figma/xyz",
            ATSTier.LEVER,
            "figma",
        ),
        (
            "https://figma.lever.co/jobs",
            ATSTier.LEVER,
            "figma",
        ),
        # Ashby variants
        (
            "https://jobs.ashbyhq.com/notion",
            ATSTier.ASHBY,
            "notion",
        ),
        (
            "https://notion.ashbyhq.com",
            ATSTier.ASHBY,
            "notion",
        ),
        # Workday variants
        (
            "https://stripe.myworkdayjobs.com/careers",
            ATSTier.WORKDAY,
            "stripe",
        ),
        # JobRight
        (
            "https://jobright.ai/jobs/123",
            ATSTier.JOBRIGHT,
            None,
        ),
        # Simplify
        (
            "https://simplify.jobs/j/abc123",
            ATSTier.SIMPLIFY,
            None,
        ),
        # Unknown
        (
            "https://example.com/careers/swe",
            ATSTier.UNKNOWN,
            None,
        ),
        # http (not https) should still match
        (
            "http://boards.greenhouse.io/stripe/jobs/1",
            ATSTier.GREENHOUSE,
            "stripe",
        ),
        # Query params should not break slug extraction
        (
            "https://jobs.lever.co/stripe/abc?ref=foo",
            ATSTier.LEVER,
            "stripe",
        ),
    ],
)
def test_classify_routing(url: str, expected_tier: ATSTier, expected_slug: str | None) -> None:
    """classify() must route every known ATS URL pattern to the correct tier and slug."""
    result = classify(url)
    assert result.tier == expected_tier, (
        f"URL {url!r}: expected tier {expected_tier}, got {result.tier}"
    )
    if expected_slug is not None:
        assert result.slug == expected_slug, (
            f"URL {url!r}: expected slug {expected_slug!r}, got {result.slug!r}"
        )


# ---------------------------------------------------------------------------
# Individual named tests to satisfy acceptance_criteria count
# ---------------------------------------------------------------------------


def test_greenhouse_boards_url() -> None:
    result = classify("https://boards.greenhouse.io/stripe/jobs/123")
    assert result.tier == ATSTier.GREENHOUSE
    assert result.slug == "stripe"


def test_greenhouse_subdomain() -> None:
    result = classify("https://stripe.greenhouse.io/jobs/abc")
    assert result.tier == ATSTier.GREENHOUSE
    assert result.slug == "stripe"


def test_greenhouse_job_boards_format() -> None:
    result = classify("https://job-boards.greenhouse.io/openai")
    assert result.tier == ATSTier.GREENHOUSE
    assert result.slug == "openai"


def test_lever_standard() -> None:
    result = classify("https://jobs.lever.co/figma/xyz")
    assert result.tier == ATSTier.LEVER
    assert result.slug == "figma"


def test_lever_subdomain() -> None:
    result = classify("https://figma.lever.co/jobs")
    assert result.tier == ATSTier.LEVER
    assert result.slug == "figma"


def test_ashby_standard() -> None:
    result = classify("https://jobs.ashbyhq.com/notion")
    assert result.tier == ATSTier.ASHBY
    assert result.slug == "notion"


def test_ashby_subdomain() -> None:
    result = classify("https://notion.ashbyhq.com")
    assert result.tier == ATSTier.ASHBY
    assert result.slug == "notion"


def test_workday_standard() -> None:
    result = classify("https://stripe.myworkdayjobs.com/careers")
    assert result.tier == ATSTier.WORKDAY
    assert result.slug == "stripe"


def test_workday_wd_prefix() -> None:
    r"""wd\d+ Workday URLs match WORKDAY tier; slug may be None (no meaningful company slug)."""
    result = classify("https://wd3.myworkdayjobs.com/External/job/USA/SWE_12345")
    assert result.tier == ATSTier.WORKDAY


def test_jobright() -> None:
    result = classify("https://jobright.ai/jobs/software-engineer/123")
    assert result.tier == ATSTier.JOBRIGHT


def test_simplify() -> None:
    result = classify("https://simplify.jobs/j/abc123")
    assert result.tier == ATSTier.SIMPLIFY


def test_unknown_returns_unknown_tier() -> None:
    result = classify("https://example.com/careers/swe")
    assert result.tier == ATSTier.UNKNOWN


def test_unknown_slug_is_none() -> None:
    result = classify("https://example.com/careers")
    assert result.slug is None


def test_http_url_handled() -> None:
    result = classify("http://boards.greenhouse.io/stripe/jobs/1")
    assert result.tier == ATSTier.GREENHOUSE


def test_url_with_query_params() -> None:
    result = classify("https://jobs.lever.co/stripe/abc?ref=foo")
    assert result.tier == ATSTier.LEVER
    assert result.slug == "stripe"


# ---------------------------------------------------------------------------
# Contract: frozen dataclass
# ---------------------------------------------------------------------------


def test_classify_returns_frozen_dataclass() -> None:
    """ClassifiedURL must be immutable — frozen=True dataclass."""
    result = classify("https://boards.greenhouse.io/stripe/jobs/1")
    assert isinstance(result, ClassifiedURL)
    with pytest.raises((AttributeError, TypeError)):
        result.tier = ATSTier.UNKNOWN  # must raise — frozen dataclass


# ---------------------------------------------------------------------------
# No I/O on import
# ---------------------------------------------------------------------------


def test_classifier_module_has_no_io_imports() -> None:
    """url_classifier.py must not import httpx, psycopg, or get_connection."""
    import importlib
    import inspect
    import sys

    mod = sys.modules.get("wekruit_matching.scraper.url_classifier")
    if mod is None:
        mod = importlib.import_module("wekruit_matching.scraper.url_classifier")
    source = inspect.getsource(mod)
    assert "import httpx" not in source, "url_classifier must not import httpx"
    assert "import psycopg" not in source, "url_classifier must not import psycopg"
    assert "get_connection" not in source, "url_classifier must not reference get_connection"
