"""Unit tests for ENRICH-02: JD text inclusion in LLM classifier prompt.

Validates that classify_job injects the job_description field into the LLM prompt
when available, omits it when None, and truncates at 4000 characters.
All tests are pure unit tests — no real LLM or network calls.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from wekruit_matching.models.job import Job


def _make_job(**kwargs) -> Job:
    defaults = dict(
        job_id="a" * 64,
        source_repo="Summer2026-Internships",
        company_name="Acme Corp",
        role_title="Software Engineer Intern",
        location_raw="San Francisco, CA",
    )
    defaults.update(kwargs)
    return Job(**defaults)


_VALID_LLM_RESPONSE = json.dumps({
    "industry": "tech",
    "company_size": "startup",
    "skills_inferred": [],
    "likely_sponsors_visa": None,
})


def test_classify_job_includes_jd_text_in_prompt_when_available(monkeypatch) -> None:
    """ENRICH-02: classify_job must inject job_description into the LLM prompt.

    JD must be at least MIN_JD_CHARS_FOR_SKILLS chars to clear the anti-
    hallucination guard, so the fixture uses a 200+ char description.
    """
    from wekruit_matching.enrichment.classifier import classify_job

    captured: list[str] = []

    def fake_call_llm(client, prompt: str) -> str:
        captured.append(prompt)
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr("wekruit_matching.enrichment.classifier._get_client", lambda: MagicMock())
    monkeypatch.setattr("wekruit_matching.enrichment.classifier._call_llm", fake_call_llm)

    long_jd = "Build APIs for new grads. " + ("Responsibilities include backend development. " * 20)
    job = _make_job(job_description=long_jd)
    classify_job(job)

    assert len(captured) == 1, "LLM must be called exactly once"
    assert "Job Description:" in captured[0]
    assert "Build APIs for new grads." in captured[0]


def test_classify_job_returns_none_and_skips_llm_when_jd_is_empty(monkeypatch) -> None:
    """2026-05-21 anti-hallucination: classify_job must NOT call the LLM
    when job_description is missing — skill extraction without a JD
    fabricates plausible-looking skills from title+company alone."""
    from wekruit_matching.enrichment.classifier import classify_job

    captured: list[str] = []

    def fake_call_llm(client, prompt: str) -> str:
        captured.append(prompt)
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr("wekruit_matching.enrichment.classifier._call_llm", fake_call_llm)

    job = _make_job(job_description=None)
    result = classify_job(job)

    assert result is None, "classify_job must return None for missing JD"
    assert len(captured) == 0, "LLM must NOT be called when JD is missing"


def test_classify_job_truncates_jd_at_4000_chars(monkeypatch) -> None:
    """ENRICH-02: classify_job must truncate job_description to 4000 chars before sending."""
    from wekruit_matching.enrichment.classifier import classify_job

    captured: list[str] = []

    def fake_call_llm(client, prompt: str) -> str:
        captured.append(prompt)
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr("wekruit_matching.enrichment.classifier._get_client", lambda: MagicMock())
    monkeypatch.setattr("wekruit_matching.enrichment.classifier._call_llm", fake_call_llm)

    long_description = "x" * 5000
    job = _make_job(job_description=long_description)
    classify_job(job)

    assert len(captured) == 1
    prompt = captured[0]
    assert "Job Description:" in prompt
    # The portion after "Job Description:" should be at most 4000 chars
    jd_section = prompt.split("Job Description:")[-1]
    assert len(jd_section) <= 4010, (
        f"JD section length {len(jd_section)} exceeds 4010 chars (4000 + margin)"
    )
