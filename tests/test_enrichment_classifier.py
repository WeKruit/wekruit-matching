"""Tests for the LLM job classifier.

These tests run without a real Anthropic API key — all API calls are mocked.
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

    def test_invalid_industry_rejected(self):
        from wekruit_matching.enrichment.classifier import EnrichmentResult
        with pytest.raises(ValidationError):
            EnrichmentResult(
                industry="unicorn_industry",
                company_size="startup",
                required_skills=[],
                sponsorship=None,
            )

    def test_invalid_company_size_rejected(self):
        from wekruit_matching.enrichment.classifier import EnrichmentResult
        with pytest.raises(ValidationError):
            EnrichmentResult(
                industry="tech",
                company_size="giant",
                required_skills=[],
                sponsorship=None,
            )

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
        from wekruit_matching.enrichment.classifier import KNOWN_SKILLS, classify_job
        skill = next(iter(KNOWN_SKILLS))  # pick one real skill
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

    def test_skills_not_in_vocab_are_dropped(self):
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
        assert "not_a_real_skill_xyz" not in result.required_skills
        assert "python" in result.required_skills

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

        from wekruit_matching.enrichment.classifier import KNOWN_SKILLS, classify_job
        skill = next(iter(KNOWN_SKILLS))
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
            classify_job(_make_job(job_description="Full JD text for ranking pipelines."))

        assert "Job Description: Full JD text for ranking pipelines." in prompt_capture["prompt"]
