"""Unit tests for Phase 63 — multi-source dedup logic.

Tests dedup_multi_source() — the in-memory pre-upsert dedupe that collapses
the same job appearing in multiple scrapers (jobright, linkedin, wellfound)
into one Job with a merged sources array.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper.dedup import (
    canonicalize_url,
    dedup_multi_source,
    _build_key,
    _normalize_company,
    _normalize_title,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _job(
    *,
    job_id: str = "abc",
    company: str = "Acme",
    title: str = "Senior Software Engineer",
    url: str = "https://example.com/jobs/123",
    source: str = "jobright-newgrad",
    sources: list[str] | None = None,
    first_seen: datetime | None = None,
) -> Job:
    return Job(
        job_id=job_id,
        source_repo=source,
        sources=sources if sources is not None else [source],
        company_name=company,
        role_title=title,
        primary_url=url,
        location_raw="",
        date_posted_raw=None,
        status=JobStatus.ACTIVE,
        first_seen_at=first_seen or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# _normalize_company / _normalize_title
# ---------------------------------------------------------------------------


def test_normalize_company_strips_punctuation():
    assert _normalize_company("Acme, Inc.") == "acmeinc"
    assert _normalize_company("Globex Corp.") == "globexcorp"
    assert _normalize_company("OpenAI Labs") == "openailabs"


def test_normalize_company_handles_empty():
    assert _normalize_company("") == ""
    assert _normalize_company(None) == ""


def test_normalize_title_sorts_tokens():
    # "Senior Software Engineer" and "Software Engineer Senior" → same key
    assert _normalize_title("Senior Software Engineer") == _normalize_title(
        "Software Engineer Senior"
    )


def test_normalize_title_lowercases():
    assert _normalize_title("Senior SWE") == _normalize_title("senior swe")


# ---------------------------------------------------------------------------
# canonicalize_url
# ---------------------------------------------------------------------------


def test_canonicalize_url_strips_utm():
    a = canonicalize_url("https://x.com/jobs/1?utm_source=jobright")
    b = canonicalize_url("https://x.com/jobs/1")
    assert a == b


def test_canonicalize_url_handles_empty():
    assert canonicalize_url("") == ""


# ---------------------------------------------------------------------------
# _build_key
# ---------------------------------------------------------------------------


def test_build_key_three_tuple():
    j = _job(company="Acme, Inc.", title="Senior SWE", url="https://x.com/jobs/1")
    key = _build_key(j)
    assert "acmeinc" in key
    assert "senior" in key
    assert "x.com/jobs/1" in key


# ---------------------------------------------------------------------------
# dedup_multi_source — main behaviors
# ---------------------------------------------------------------------------


def test_dedup_empty_input():
    assert dedup_multi_source([]) == []


def test_dedup_no_duplicates_passes_through():
    j1 = _job(job_id="a", company="Acme", title="Senior SWE", url="https://x.com/a")
    j2 = _job(job_id="b", company="Globex", title="Staff Eng", url="https://y.com/b")
    out = dedup_multi_source([j1, j2])
    assert len(out) == 2


def test_dedup_same_job_three_sources_merges_into_one():
    j_jr = _job(
        job_id="a", company="Acme", title="Senior SWE",
        url="https://x.com/jobs/1?utm_source=jr",
        source="jobright-newgrad", sources=["jobright"],
    )
    j_li = _job(
        job_id="b", company="Acme", title="Senior SWE",
        url="https://x.com/jobs/1",
        source="linkedin", sources=["linkedin"],
    )
    j_wf = _job(
        job_id="c", company="Acme", title="Senior SWE",
        url="https://x.com/jobs/1?ref=wellfound",
        source="wellfound", sources=["wellfound"],
    )
    out = dedup_multi_source([j_jr, j_li, j_wf])
    assert len(out) == 1
    assert sorted(out[0].sources) == ["jobright", "linkedin", "wellfound"]


def test_dedup_different_jobs_not_collapsed():
    j1 = _job(
        job_id="a", company="Acme", title="Senior SWE",
        url="https://x.com/jobs/1", source="linkedin", sources=["linkedin"],
    )
    j2 = _job(
        job_id="b", company="Acme", title="Staff Eng",
        url="https://x.com/jobs/2", source="linkedin", sources=["linkedin"],
    )
    out = dedup_multi_source([j1, j2])
    assert len(out) == 2


def test_dedup_takes_freshest_first_seen_at():
    older = datetime.now(UTC) - timedelta(days=5)
    newer = datetime.now(UTC)
    j_old = _job(
        job_id="a", first_seen=older, source="jobright-newgrad",
        sources=["jobright"], url="https://x.com/jobs/1",
    )
    j_new = _job(
        job_id="b", first_seen=newer, source="linkedin",
        sources=["linkedin"], url="https://x.com/jobs/1",
    )
    out = dedup_multi_source([j_old, j_new])
    assert len(out) == 1
    # first_seen_at should be the freshest
    assert out[0].first_seen_at == newer


def test_dedup_promotes_to_higher_priority_source_repo():
    # linkedin (priority 4) > wellfound (3) > jobright (2)
    j_jr = _job(
        job_id="a", source="jobright-newgrad", sources=["jobright"],
        url="https://x.com/jobs/1",
    )
    j_li = _job(
        job_id="b", source="linkedin", sources=["linkedin"],
        url="https://x.com/jobs/1",
    )
    out = dedup_multi_source([j_jr, j_li])
    assert len(out) == 1
    # linkedin is higher priority
    assert out[0].source_repo == "linkedin"


def test_dedup_merge_sources_array_is_sorted_and_deduped():
    j1 = _job(
        job_id="a", source="linkedin", sources=["linkedin"],
        url="https://x.com/jobs/1",
    )
    j2 = _job(
        job_id="b", source="linkedin", sources=["linkedin", "wellfound"],
        url="https://x.com/jobs/1",
    )
    out = dedup_multi_source([j1, j2])
    assert len(out) == 1
    # No duplicates in the merged sources
    assert out[0].sources == sorted(set(out[0].sources))


def test_dedup_falls_back_to_source_repo_when_sources_empty():
    j = _job(
        job_id="a", source="wellfound", sources=[],
        url="https://x.com/jobs/1",
    )
    out = dedup_multi_source([j])
    assert len(out) == 1
    assert "wellfound" in out[0].sources


def test_dedup_company_punctuation_normalization():
    j1 = _job(
        job_id="a", company="Acme, Inc.", title="Senior SWE",
        url="https://x.com/jobs/1", source="linkedin", sources=["linkedin"],
    )
    j2 = _job(
        job_id="b", company="Acme Inc", title="Senior SWE",
        url="https://x.com/jobs/1", source="wellfound", sources=["wellfound"],
    )
    out = dedup_multi_source([j1, j2])
    # Should collapse — punctuation normalized
    assert len(out) == 1
    assert sorted(out[0].sources) == ["linkedin", "wellfound"]
