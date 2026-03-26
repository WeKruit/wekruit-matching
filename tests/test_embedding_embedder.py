"""Tests for the OpenAI embedding module.

These tests run without a real OpenAI API key — all API calls are mocked.
Tests verify:
  - compose_embedding_text() canonical string format
  - embed_text() success path
  - embed_text() retries on RateLimitError (429)
  - embed_text() does NOT retry on 4xx APIStatusError (e.g. 400)
  - EMBEDDING_MODEL constant value
"""
from unittest.mock import MagicMock, patch

import openai
import pytest

from wekruit_matching.models.job import Job


def _make_job(**kwargs) -> Job:
    defaults = dict(
        job_id="a" * 64,
        source_repo="Summer2026-Internships",
        company_name="Stripe",
        role_title="Software Engineer",
        required_skills=["python", "go"],
    )
    defaults.update(kwargs)
    return Job(**defaults)


class TestComposeEmbeddingText:
    def test_compose_with_skills(self):
        from wekruit_matching.embedding.embedder import compose_embedding_text
        job = _make_job(role_title="Software Engineer", company_name="Stripe", required_skills=["python", "go"])
        result = compose_embedding_text(job)
        assert result == "Software Engineer at Stripe. Skills: python, go"

    def test_compose_with_empty_skills(self):
        from wekruit_matching.embedding.embedder import compose_embedding_text
        job = _make_job(role_title="Software Engineer", company_name="Stripe", required_skills=[])
        result = compose_embedding_text(job)
        assert result == "Software Engineer at Stripe. Skills: "

    def test_compose_single_skill(self):
        from wekruit_matching.embedding.embedder import compose_embedding_text
        job = _make_job(role_title="Data Scientist", company_name="Acme", required_skills=["python"])
        result = compose_embedding_text(job)
        assert result == "Data Scientist at Acme. Skills: python"


class TestEmbeddingModel:
    def test_embedding_model_constant(self):
        from wekruit_matching.embedding.embedder import EMBEDDING_MODEL
        assert EMBEDDING_MODEL == "text-embedding-3-small"


class TestEmbedText:
    def _mock_openai_response(self, vector: list[float]) -> MagicMock:
        """Return a mock that looks like openai SDK embeddings.create() response."""
        embedding_obj = MagicMock()
        embedding_obj.embedding = vector
        response = MagicMock()
        response.data = [embedding_obj]
        return response

    def test_embed_text_success_returns_vector(self):
        from wekruit_matching.embedding.embedder import embed_text
        vector = [0.1] * 1536
        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = self._mock_openai_response(vector)
        result = embed_text("test text", mock_client)
        assert result == vector
        assert len(result) == 1536

    def test_embed_text_calls_correct_model(self):
        from wekruit_matching.embedding.embedder import EMBEDDING_MODEL, embed_text
        vector = [0.0] * 1536
        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = self._mock_openai_response(vector)
        embed_text("some text", mock_client)
        mock_client.embeddings.create.assert_called_once_with(
            model=EMBEDDING_MODEL, input="some text"
        )

    def test_embed_text_retries_on_rate_limit_error(self):
        from wekruit_matching.embedding.embedder import embed_text
        vector = [0.2] * 1536
        good_response = self._mock_openai_response(vector)
        rate_limit_exc = openai.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body={},
        )
        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = [rate_limit_exc, good_response]
        result = embed_text("test text", mock_client)
        assert mock_client.embeddings.create.call_count == 2
        assert result == vector

    def test_embed_text_does_not_retry_on_400_error(self):
        from wekruit_matching.embedding.embedder import embed_text
        # Build a minimal mock response for APIStatusError
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.headers = {}
        client_error = openai.BadRequestError(
            message="bad request",
            response=mock_response,
            body={},
        )
        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = client_error
        with pytest.raises(openai.BadRequestError):
            embed_text("test text", mock_client)
        # Should have been called exactly once — no retries on 4xx
        assert mock_client.embeddings.create.call_count == 1

    def test_embed_text_raises_after_all_retries_exhausted(self):
        from wekruit_matching.embedding.embedder import embed_text
        rate_limit_exc = openai.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body={},
        )
        mock_client = MagicMock()
        mock_client.embeddings.create.side_effect = rate_limit_exc
        with pytest.raises(openai.RateLimitError):
            embed_text("test text", mock_client)
        # Should have retried up to 5 times total
        assert mock_client.embeddings.create.call_count == 5


class TestGetClient:
    def test_get_client_uses_settings_api_key(self):
        from wekruit_matching.embedding.embedder import _get_client
        # Clear lru_cache so patch takes effect
        _get_client.cache_clear()
        with patch("wekruit_matching.embedding.embedder.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(openai_api_key="test-key-123")
            with patch("openai.OpenAI") as mock_openai_cls:
                mock_openai_cls.return_value = MagicMock()
                _get_client()
                mock_openai_cls.assert_called_once_with(api_key="test-key-123")
        # Clean up cache after test
        _get_client.cache_clear()
