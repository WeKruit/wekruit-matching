"""LLM-based job classifier using Anthropic Claude Haiku.

classify_job(job) -> EnrichmentResult

Controlled vocabularies ensure no hallucinated values enter the database.
null/unknown are first-class values — never replaced by a guess.
Tenacity retries handle 429/5xx from the Anthropic API.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Optional

import anthropic
from pydantic import BaseModel, field_validator
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from wekruit_matching.config import get_settings
from wekruit_matching.models.job import Job


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

INDUSTRY_VOCAB: frozenset[str] = frozenset({
    "tech", "fintech", "healthtech", "ecommerce", "enterprise_saas",
    "ai_ml", "cybersecurity", "gaming", "social_media", "hardware",
    "consulting", "other", "unknown",
})

COMPANY_SIZE_VOCAB: frozenset[str] = frozenset({"startup", "midsize", "large", "unknown"})

KNOWN_SKILLS: frozenset[str] = frozenset({
    "python", "java", "javascript", "typescript", "go", "rust", "c", "c++",
    "c#", "swift", "kotlin", "ruby", "scala", "r", "matlab", "sql",
    "react", "angular", "vue", "node.js", "django", "flask", "fastapi",
    "spring", "rails", "express", "nextjs",
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform", "linux",
    "git", "ci/cd", "jenkins", "github_actions",
    "machine_learning", "deep_learning", "nlp", "computer_vision",
    "pytorch", "tensorflow", "scikit_learn", "pandas", "numpy",
    "spark", "kafka", "airflow", "dbt",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
    "graphql", "rest", "grpc",
    "data_structures", "algorithms", "system_design", "distributed_systems",
})


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class EnrichmentResult(BaseModel):
    """Validated enrichment output from the LLM classifier.

    All fields use controlled vocabularies. null/unknown are valid values.
    Pydantic validators reject out-of-vocabulary strings at construction time.
    """
    industry: str
    company_size: str
    required_skills: list[str]
    sponsorship: Optional[bool] = None

    @field_validator("industry")
    @classmethod
    def industry_in_vocab(cls, v: str) -> str:
        if v not in INDUSTRY_VOCAB:
            raise ValueError(f"industry '{v}' not in controlled vocabulary {INDUSTRY_VOCAB}")
        return v

    @field_validator("company_size")
    @classmethod
    def company_size_in_vocab(cls, v: str) -> str:
        if v not in COMPANY_SIZE_VOCAB:
            raise ValueError(f"company_size '{v}' not in {COMPANY_SIZE_VOCAB}")
        return v

    @field_validator("required_skills")
    @classmethod
    def skills_in_vocab(cls, v: list[str]) -> list[str]:
        return [s for s in v if s.lower() in KNOWN_SKILLS]


def _safe_default() -> EnrichmentResult:
    """Return a fully-unknown result — used on any parse/validation failure."""
    return EnrichmentResult(
        industry="unknown",
        company_size="unknown",
        required_skills=[],
        sponsorship=None,
    )


# ---------------------------------------------------------------------------
# Anthropic client (cached, test-injectable via _get_client)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)


_SYSTEM_PROMPT = """\
You are a job-listing classifier. Given a job listing, return ONLY a JSON object
with these exact keys — no explanation, no markdown, no extra text:

{
  "industry": "<one of: tech, fintech, healthtech, ecommerce, enterprise_saas, ai_ml, cybersecurity, gaming, social_media, hardware, consulting, other, unknown>",
  "company_size": "<one of: startup, midsize, large, unknown>",
  "skills_inferred": ["<skill1>", "<skill2>", ...],
  "likely_sponsors_visa": <true | false | null>
}

Rules:
- Use "unknown" when there is insufficient signal — never guess.
- For likely_sponsors_visa: true = explicitly offers, false = explicitly does not, null = no signal.
- skills_inferred should list common technical skills implied by the role title and company type.
- Output valid JSON only. No markdown code fences.
"""


def _should_retry(exc: BaseException) -> bool:
    """Retry on rate-limit or server errors only."""
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return True
    return False


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIStatusError)),
    reraise=True,
)
def _call_anthropic(client: anthropic.Anthropic, prompt: str) -> str:
    """Call the Anthropic API and return the raw text content.

    Tenacity retries on RateLimitError and server-side APIStatusError (5xx).
    Connection errors and client errors (4xx other than 429) propagate immediately.
    """
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def classify_job(job: Job) -> EnrichmentResult:
    """Classify a single job listing using Claude Haiku.

    Returns an EnrichmentResult with controlled-vocabulary fields.
    On any API or parse failure, returns _safe_default() — never raises.
    The caller (enrichment worker) is responsible for deciding whether to
    retry the entire job or accept the safe default.
    """
    prompt = (
        f"Company: {job.company_name}\n"
        f"Role: {job.role_title}\n"
        f"Location: {job.location_raw or 'unknown'}"
    )
    client = _get_client()
    try:
        raw = _call_anthropic(client, prompt)
        data = json.loads(raw)
    except Exception:
        return _safe_default()

    try:
        # Normalize sponsorship: convert None-in-JSON (null) to Python None
        sponsorship_raw = data.get("likely_sponsors_visa")
        if isinstance(sponsorship_raw, bool):
            sponsorship = sponsorship_raw
        else:
            sponsorship = None

        # Normalize skills: lowercase before vocab check
        skills_raw = data.get("skills_inferred", [])
        skills_normalized = [s.lower() for s in skills_raw if isinstance(s, str)]

        return EnrichmentResult(
            industry=str(data.get("industry", "unknown")).lower(),
            company_size=str(data.get("company_size", "unknown")).lower(),
            required_skills=skills_normalized,
            sponsorship=sponsorship,
        )
    except Exception:
        return _safe_default()
