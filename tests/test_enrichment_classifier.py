"""Tests for the LLM job classifier.

These tests run without a real Anthropic API key — all API calls are mocked.

NOTE (2026-05-08, P7-C unified-canonical-tags): The classifier's `industry`
output is informational free-form (canonical mapping owned by wekruit-pa
`paMatchingJobsAutoEnrich` Firestore trigger). Tests that previously
asserted vocab rejection (`test_invalid_industry_rejected`,
`test_skills_not_in_vocab_are_dropped`) have been replaced with
free-form passthrough assertions matching the new contract.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from wekruit_matching.models.job import Job


def _make_job(**kwargs) -> Job:
    defaults = dict(
        job_id="a" * 64,
        source_repo="Summer2026-Internships",
        company_name="Acme Corp",
        role_title="Software Engineer Intern",
        location_raw="San Francisco, CA",
        # 2026-05-21: classify_job now requires a non-trivial JD or it
        # short-circuits with None (anti-hallucination guard). Tests that
        # don't care about JD content still need a JD long enough to clear
        # MIN_JD_CHARS_FOR_SKILLS (200). Use a generic placeholder.
        job_description=(
            "Generic engineering job description used for classifier tests. "
            "Responsibilities include backend development, API design, and "
            "shipping reliable services. Requirements: strong CS fundamentals, "
            "production engineering experience, comfortable with on-call rotation."
        ),
    )
    defaults.update(kwargs)
    return Job(**defaults)


class TestEnrichmentResultValidation:
    def test_valid_industry_accepted(self):
        from wekruit_matching.enrichment.classifier import EnrichmentResult
        r = EnrichmentResult(
            industry="tech",
            company_size="startup",
            required_skills=[],
            sponsorship=None,
        )
        assert r.industry == "tech"

    def test_unknown_industry_accepted(self):
        from wekruit_matching.enrichment.classifier import EnrichmentResult
        r = EnrichmentResult(
            industry="unknown",
            company_size="unknown",
            required_skills=[],
            sponsorship=None,
        )
        assert r.industry == "unknown"

    def test_freeform_industry_passes_through_unchanged(self):
        """v1.6 D5 + P7-C 2026-05-08: industry is free-form (canonical
        owned by wekruit-pa). Any lowercase snake_case string is accepted;
        the old `INDUSTRY_VOCAB` 38-abbreviation gate has been removed."""
        from wekruit_matching.enrichment.classifier import EnrichmentResult
        r = EnrichmentResult(
            industry="some_unusual_sector",
            company_size="startup",
            required_skills=[],
            sponsorship=None,
        )
        assert r.industry == "some_unusual_sector"

    def test_industry_normalized_lowercase_underscore(self):
        from wekruit_matching.enrichment.classifier import EnrichmentResult
        r = EnrichmentResult(
            industry="Financial Technology",  # mixed-case + spaces
            company_size="midsize",
            required_skills=[],
            sponsorship=None,
        )
        # Normalizer lowercases + replaces spaces with underscore
        assert r.industry == "financial_technology"

    def test_invalid_company_size_normalized_to_unknown(self):
        """v1.6 D5: company_size is a closed 4-enum. Out-of-vocab → 'unknown'.
        (Note: pre-2026-05-08 this raised ValidationError for industry;
        company_size still uses a closed enum because there are only 4
        legitimate values and the normalizer falls through to 'unknown'.)"""
        from wekruit_matching.enrichment.classifier import EnrichmentResult
        r = EnrichmentResult(
            industry="tech",
            company_size="giant",
            required_skills=[],
            sponsorship=None,
        )
        assert r.company_size == "unknown"

    def test_sponsorship_bool_or_none_accepted(self):
        from wekruit_matching.enrichment.classifier import EnrichmentResult
        r_true = EnrichmentResult(
            industry="tech",
            company_size="startup",
            required_skills=[],
            sponsorship=True,
        )
        r_false = EnrichmentResult(
            industry="tech",
            company_size="startup",
            required_skills=[],
            sponsorship=False,
        )
        r_none = EnrichmentResult(
            industry="tech",
            company_size="startup",
            required_skills=[],
            sponsorship=None,
        )
        assert r_true.sponsorship is True
        assert r_false.sponsorship is False
        assert r_none.sponsorship is None


class TestClassifyJob:
    def _mock_llm_response(self, payload: dict) -> MagicMock:
        """Return a mock that matches the OpenAI-compatible chat response shape."""
        msg = MagicMock()
        msg.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
        return msg

    def test_valid_response_produces_enrichment_result(self):
        from wekruit_matching.enrichment.classifier import classify_job
        # P7-C 2026-05-08: KNOWN_SKILLS deleted; tests now use literal strings.
        # Skills are informational hints, canonical bucketed Skill[] computed by
        # wekruit-pa pa-job-tag-enricher.
        skill = "python"
        payload = {
            "industry": "tech",
            "company_size": "startup",
            "skills_inferred": [skill],
            "likely_sponsors_visa": True,
        }
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_llm_response(payload)
        with patch("wekruit_matching.enrichment.classifier._get_client", return_value=mock_client):
            result = classify_job(_make_job())
        assert result.industry == "tech"
        assert result.company_size == "startup"
        assert result.sponsorship is True
        assert skill in result.required_skills

    def test_invalid_json_returns_safe_default(self):
        from wekruit_matching.enrichment.classifier import classify_job
        mock_client = MagicMock()
        bad_msg = MagicMock()
        bad_msg.choices = [MagicMock(message=MagicMock(content="not json at all {{{"))]
        mock_client.chat.completions.create.return_value = bad_msg
        with patch("wekruit_matching.enrichment.classifier._get_client", return_value=mock_client):
            result = classify_job(_make_job())
        assert result.industry == "unknown"
        assert result.company_size == "unknown"
        assert result.required_skills == []
        assert result.sponsorship is None

    def test_oov_skills_pass_through_under_freeform_contract(self):
        """v1.6 D5 + P7-C 2026-05-08: required_skills is informational
        free-form (canonical owned by wekruit-pa). The old `KNOWN_SKILLS`
        gate has been removed; OOV skills are preserved for downstream
        consumers (Postgres + Firebase sync). Capped at 15 + dedupe."""
        from wekruit_matching.enrichment.classifier import classify_job
        payload = {
            "industry": "tech",
            "company_size": "startup",
            "skills_inferred": ["not_a_real_skill_xyz", "python"],
            "likely_sponsors_visa": False,
        }
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_llm_response(payload)
        with patch("wekruit_matching.enrichment.classifier._get_client", return_value=mock_client):
            result = classify_job(_make_job())
        # Both kept — wekruit-pa enricher will canonicalize on the Firestore side.
        assert "not_a_real_skill_xyz" in result.required_skills
        assert "python" in result.required_skills

    def test_skills_capped_at_15_and_deduped(self):
        from wekruit_matching.enrichment.classifier import classify_job
        skills = [f"skill_{i}" for i in range(20)] + ["python", "python"]  # dupes
        payload = {
            "industry": "tech",
            "company_size": "startup",
            "skills_inferred": skills,
            "likely_sponsors_visa": None,
        }
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_llm_response(payload)
        with patch("wekruit_matching.enrichment.classifier._get_client", return_value=mock_client):
            result = classify_job(_make_job())
        assert len(result.required_skills) == 15
        # Dedupe occurs before slicing, so 'python' appears only once
        assert result.required_skills.count("python") <= 1

    def test_unknown_industry_from_llm_passes_through(self):
        from wekruit_matching.enrichment.classifier import classify_job
        payload = {
            "industry": "unknown",
            "company_size": "unknown",
            "skills_inferred": [],
            "likely_sponsors_visa": None,
        }
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = self._mock_llm_response(payload)
        with patch("wekruit_matching.enrichment.classifier._get_client", return_value=mock_client):
            result = classify_job(_make_job())
        assert result.industry == "unknown"
        assert result.company_size == "unknown"
        assert result.sponsorship is None

    def test_429_triggers_retry(self):
        import openai

        from wekruit_matching.enrichment.classifier import classify_job
        skill = "python"
        good_payload = {
            "industry": "fintech",
            "company_size": "midsize",
            "skills_inferred": [skill],
            "likely_sponsors_visa": False,
        }
        good_response = self._mock_llm_response(good_payload)
        rate_limit_exc = openai.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body={},
        )
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [rate_limit_exc, good_response]
        with patch("wekruit_matching.enrichment.classifier._get_client", return_value=mock_client):
            # With min wait=0 in tests, retry should succeed on second call
            result = classify_job(_make_job())
        assert mock_client.chat.completions.create.call_count == 2
        assert result.industry == "fintech"

    def test_job_description_is_included_in_prompt_when_available(self):
        from wekruit_matching.enrichment.classifier import classify_job

        payload = {
            "industry": "tech",
            "company_size": "startup",
            "skills_inferred": ["python"],
            "likely_sponsors_visa": None,
        }
        prompt_capture: dict[str, str] = {}

        def fake_call(_client, prompt: str) -> str:
            prompt_capture["prompt"] = prompt
            return json.dumps(payload)

        with (
            patch("wekruit_matching.enrichment.classifier._get_client", return_value=MagicMock()),
            patch("wekruit_matching.enrichment.classifier._call_llm", side_effect=fake_call),
        ):
            # 2026-05-21 guard: JD must be >= MIN_JD_CHARS_FOR_SKILLS chars
            # to reach the LLM. Pad to clear the threshold while preserving
            # the original assertion target substring.
            long_jd = "Full JD text for ranking pipelines. " + ("x " * 100)
            classify_job(_make_job(job_description=long_jd))

        assert "Job Description: Full JD text for ranking pipelines." in prompt_capture["prompt"]
