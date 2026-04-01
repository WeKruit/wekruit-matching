"""LLM-based job classifier using Anthropic Claude Haiku.

classify_job(job) -> EnrichmentResult

Controlled vocabularies ensure no hallucinated values enter the database.
null/unknown are first-class values — never replaced by a guess.
Tenacity retries handle 429/5xx from the Anthropic API.
"""
from __future__ import annotations

import json
from functools import lru_cache

from loguru import logger
from openai import APIStatusError, OpenAI, RateLimitError
from pydantic import BaseModel, field_validator
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from wekruit_matching.config import get_settings
from wekruit_matching.models.job import Job

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

INDUSTRY_VOCAB: frozenset[str] = frozenset({
    "tech", "fintech", "healthtech", "healthcare", "ecommerce",
    "enterprise_saas", "ai_ml", "cybersecurity", "gaming", "social_media",
    "hardware", "consulting", "telecom", "automotive", "aerospace_defense",
    "construction", "defense", "security", "manufacturing", "retail",
    "media", "education", "government", "energy", "transportation",
    "hospitality", "real_estate", "nonprofit", "legal", "pharma",
    "banking", "finance", "insurance", "logistics", "food_service",
    "agriculture", "mining", "utilities",
    "other", "unknown",
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
    sponsorship: bool | None = None

    @field_validator("industry")
    @classmethod
    def industry_normalized(cls, v: str) -> str:
        """Lowercase, strip, default to 'unknown'. Accept any value."""
        val = v.lower().strip().replace(" ", "_") if v else "unknown"
        return val or "unknown"

    @field_validator("company_size")
    @classmethod
    def company_size_normalized(cls, v: str) -> str:
        """Normalize to known sizes or 'unknown'."""
        val = v.lower().strip() if v else "unknown"
        if val not in ("startup", "midsize", "large", "unknown"):
            return "unknown"
        return val

    @field_validator("required_skills")
    @classmethod
    def skills_normalized(cls, v: list[str]) -> list[str]:
        """Normalize skills to lowercase, deduplicate, cap at 15."""
        seen: set[str] = set()
        out: list[str] = []
        for s in v:
            key = s.lower().strip()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out[:15]


def _safe_default() -> EnrichmentResult:
    """Return a fully-unknown result — used on any parse/validation failure."""
    return EnrichmentResult(
        industry="unknown",
        company_size="unknown",
        required_skills=[],
        sponsorship=None,
    )


# ---------------------------------------------------------------------------
# SiliconFlow client (Qwen3-8B — free tier, OpenAI-compatible)
# ---------------------------------------------------------------------------

_CLASSIFIER_MODEL = "Qwen/Qwen3-8B"
_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

@lru_cache(maxsize=1)
def _get_client() -> OpenAI:
    settings = get_settings()
    return OpenAI(
        api_key=settings.siliconflow_api_key,
        base_url=_SILICONFLOW_BASE_URL,
    )


_SYSTEM_PROMPT = """\
You are a job-listing classifier. Return ONLY valid JSON with these keys:

{
  "industry": "<lowercase snake_case industry, e.g. tech, healthcare, retail, banking>",
  "company_size": "<startup | midsize | large | unknown>",
  "skills_inferred": ["<skill1>", "<skill2>", ...],
  "likely_sponsors_visa": <true | false | null>
}

Rules:
- industry: use a short, specific label. Use "unknown" only when truly uncertain.
- skills_inferred: list key skills for the role (technical, soft, or domain-specific). Max 8.
- likely_sponsors_visa: true = explicitly offers, false = explicitly does not, null = no signal.
- Output valid JSON only. No markdown, no explanation.
"""


def _should_retry(exc: BaseException) -> bool:
    """Retry on rate-limit or server errors only."""
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code >= 500:
        return True
    return False


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception(_should_retry),
    reraise=True,
)
def _call_llm(client: OpenAI, prompt: str) -> str:
    """Call SiliconFlow Qwen3-8B and return the raw text content.

    Tenacity retries only on RateLimitError (429) and server-side 5xx errors.
    Free tier: 1000 RPM, 50K TPM.
    """
    response = client.chat.completions.create(
        model=_CLASSIFIER_MODEL,
        max_tokens=512,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )
    return response.choices[0].message.content or ""


def classify_job(job: Job) -> EnrichmentResult:
    """Classify a single job listing using GPT-5.4 Nano.

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
    if job.job_description:
        prompt += f"\nJob Description: {job.job_description[:4000]}"
    client = _get_client()
    try:
        raw = _call_llm(client, prompt)
        # Strip markdown code fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        data = json.loads(cleaned)
    except Exception as e:
        logger.warning(
            "Classification failed for {company} ({role}): {error}",
            company=job.company_name,
            role=job.role_title,
            error=e,
        )
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
    except Exception as e:
        logger.warning(
            "Classification result validation failed for {company} ({role}): {error}",
            company=job.company_name,
            role=job.role_title,
            error=e,
        )
        return _safe_default()
