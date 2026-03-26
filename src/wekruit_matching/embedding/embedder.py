"""OpenAI embedding client for wekruit-matching.

embed_text(text, client) -> list[float]  (1536 dimensions)
compose_embedding_text(job) -> str       canonical text for a job listing

Tenacity retries handle 429/5xx from the OpenAI API.
embed_text RAISES after retries exhausted — per-job isolation is the caller's job.
"""
from __future__ import annotations

from functools import lru_cache

import openai
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from wekruit_matching.config import get_settings
from wekruit_matching.models.job import Job

EMBEDDING_MODEL = "text-embedding-3-small"


@lru_cache(maxsize=1)
def _get_client() -> openai.OpenAI:
    return openai.OpenAI(api_key=get_settings().openai_api_key)


def compose_embedding_text(job: Job) -> str:
    """Compose the canonical embedding input string for a job listing.

    Format: "{role_title} at {company_name}. Skills: {skills_csv}"
    Empty skills list yields "Skills: " (trailing space — harmless for embeddings).
    """
    skills_str = ", ".join(job.required_skills)
    return f"{job.role_title} at {job.company_name}. Skills: {skills_str}"


def _should_retry_openai(exc: BaseException) -> bool:
    """Retry on rate-limit (429) or server errors (5xx) only.

    Does NOT retry on 4xx client errors other than RateLimitError.
    RateLimitError is a subclass of APIStatusError in the openai SDK, so
    we check for it explicitly first.
    """
    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
        return True
    return False


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception(_should_retry_openai),
    reraise=True,
)
def _call_openai(client: openai.OpenAI, text: str) -> list[float]:
    """Call the OpenAI embeddings endpoint and return the vector.

    Retries on RateLimitError (429) and server errors (5xx).
    Client errors (4xx other than 429) propagate immediately.
    Raises after 5 failed attempts — caller must handle isolation.
    """
    response = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


_embedding_cache: dict[str, list[float]] = {}
_CACHE_MAX = 256


def embed_text(text: str, client: openai.OpenAI | None = None) -> list[float]:
    """Generate a 1536-dimension embedding for the given text.

    Results are cached in-memory (LRU, 256 entries) to avoid duplicate
    OpenAI calls for repeated skill combinations.

    Args:
        text: The text to embed (use compose_embedding_text to produce it).
        client: Optional OpenAI client (defaults to cached _get_client()).
                Pass explicitly in tests to avoid real API calls.

    Returns:
        list[float] of length 1536.

    Raises:
        openai.RateLimitError or openai.APIStatusError after retries exhausted.
    """
    if client is None and text in _embedding_cache:
        return _embedding_cache[text]

    c = client if client is not None else _get_client()
    result = _call_openai(c, text)

    if client is None:
        if len(_embedding_cache) >= _CACHE_MAX:
            # Evict oldest entry
            _embedding_cache.pop(next(iter(_embedding_cache)))
        _embedding_cache[text] = result

    return result
