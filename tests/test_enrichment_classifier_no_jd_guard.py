"""Anti-hallucination guard — 2026-05-21 launch blocker.

classify_job must NOT extract required_skills when the JD is missing or
too short. Without a real JD, the LLM produces plausible-looking but
fabricated skills from title + company alone — corrupting downstream
matching. The guard returns None; the worker treats None as "skip
UPDATE" so the row stays eligible for next run when Stage 2b may have
landed a JD.

Production bug it pins:
  6,006 active jobs / 1,809 with required_skills=[] / many of those
  passed through the classifier with no JD and got fabricated skills
  before this guard.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from wekruit_matching.enrichment.classifier import (
    MIN_JD_CHARS_FOR_SKILLS,
    classify_job,
)
from wekruit_matching.models.job import Job


def _make_job(**kwargs) -> Job:
    defaults = dict(
        job_id="a" * 64,
        source_repo="jobright-newgrad",
        company_name="Acme Corp",
        role_title="Software Engineer",
        location_raw="San Francisco, CA",
    )
    defaults.update(kwargs)
    return Job(**defaults)


_VALID_LLM_RESPONSE = json.dumps({
    "industry": "tech",
    "company_size": "startup",
    "skills_inferred": ["python", "go"],
    "likely_sponsors_visa": None,
})


def test_classify_job_returns_none_when_jd_is_none(monkeypatch) -> None:
    """JD=None → None, no LLM call, no hallucinated skills."""
    captured: list[str] = []
    monkeypatch.setattr(
        "wekruit_matching.enrichment.classifier._call_llm",
        lambda c, p: captured.append(p) or _VALID_LLM_RESPONSE,
    )

    result = classify_job(_make_job(job_description=None))

    assert result is None
    assert len(captured) == 0, "LLM must not be invoked when JD is missing"


def test_classify_job_returns_none_when_jd_is_empty_string(monkeypatch) -> None:
    """Empty string treated same as None."""
    captured: list[str] = []
    monkeypatch.setattr(
        "wekruit_matching.enrichment.classifier._call_llm",
        lambda c, p: captured.append(p) or _VALID_LLM_RESPONSE,
    )

    result = classify_job(_make_job(job_description=""))

    assert result is None
    assert len(captured) == 0


def test_classify_job_returns_none_when_jd_is_whitespace_only(monkeypatch) -> None:
    """A JD of only spaces/newlines is treated as missing."""
    captured: list[str] = []
    monkeypatch.setattr(
        "wekruit_matching.enrichment.classifier._call_llm",
        lambda c, p: captured.append(p) or _VALID_LLM_RESPONSE,
    )

    result = classify_job(_make_job(job_description="   \n\t   \n   "))

    assert result is None
    assert len(captured) == 0


def test_classify_job_returns_none_when_jd_below_min_length(monkeypatch) -> None:
    """A JD shorter than MIN_JD_CHARS_FOR_SKILLS is still rejected."""
    captured: list[str] = []
    monkeypatch.setattr(
        "wekruit_matching.enrichment.classifier._call_llm",
        lambda c, p: captured.append(p) or _VALID_LLM_RESPONSE,
    )

    short_jd = "Build APIs for new grads."  # ~26 chars
    assert len(short_jd) < MIN_JD_CHARS_FOR_SKILLS
    result = classify_job(_make_job(job_description=short_jd))

    assert result is None
    assert len(captured) == 0


def test_classify_job_invokes_llm_when_jd_meets_min_length(monkeypatch) -> None:
    """At MIN_JD_CHARS_FOR_SKILLS exactly, classify_job proceeds."""
    captured: list[str] = []
    monkeypatch.setattr(
        "wekruit_matching.enrichment.classifier._get_client",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "wekruit_matching.enrichment.classifier._call_llm",
        lambda c, p: captured.append(p) or _VALID_LLM_RESPONSE,
    )

    long_jd = "x" * MIN_JD_CHARS_FOR_SKILLS
    result = classify_job(_make_job(job_description=long_jd))

    assert result is not None
    assert result.required_skills == ["python", "go"]
    assert len(captured) == 1
    assert "Job Description:" in captured[0]


def test_min_jd_chars_constant_is_reasonable() -> None:
    """Sanity: the guard threshold is non-trivial but not crippling.

    Too low (e.g. 20) lets hallucinated skills through; too high (e.g.
    2000) rejects legitimate short job postings (small companies, brief
    listings). 100-500 is the right band.
    """
    assert 100 <= MIN_JD_CHARS_FOR_SKILLS <= 500
