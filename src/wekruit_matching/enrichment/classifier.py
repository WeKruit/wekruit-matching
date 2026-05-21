"""LLM-based job classifier — INFORMATIONAL only. Canonical tagging
owned by wekruit-pa.

CANONICAL TAGGING ARCHITECTURE (v1.6/v1.7/v1.8 unified — 2026-05-08):

This module's `industry` output is a free-form informational hint; it is
NOT the canonical match-time signal. The unified canonical-tag pipeline
lives in wekruit-pa (`packages/shared-tags` + `packages/pa-job-tag-enricher`)
and runs automatically via the Firestore trigger
`paMatchingJobsAutoEnrich` (deployed CF) on every active matching-jobs
write. That trigger calls the canonical LLM enricher and writes the
match-time fields:

    roleFunction       (17 closed enum, hard filter — D1)
    industrySector     (42 closed enum, soft score — D2)
    relevantTags       (open vocab, max 12 — D6)
    requiredSkills     (Skill[] objects with bucket+baseWeight — D7)
    seniorityLevel     (13 enum)
    locationBuckets    (130+ enum)
    jobType            (10 enum)

Adam-locked decisions (2026-05-05): "tag must be managed in one place"
— that one place is `packages/shared-tags`. Macmini does NOT duplicate
the vocab, does NOT enforce the 38-abbreviation `INDUSTRY_VOCAB` (which
violated D5: NO abbreviations), and does NOT need to know the canonical
schema. It supplies raw enrichment hints; PA owns canonical mapping.

See `docs/canonical-tags-sync.md` for the full rationale + 3-repo arch.

This module's job (post-2026-05-08):
- Pull a free-form industry hint (informational only — `jobs.industry`)
- Pull a `company_size` hint (informational only — `jobs.company_size`)
- Pull a flat `required_skills: list[str]` hint (informational only —
  the canonical bucketed `requiredSkills: Skill[]` is computed by
  wekruit-pa's enricher and lives on the matching-jobs Firestore doc)
- Pull a `sponsorship: bool | None` hint (informational only — the
  canonical `sponsorship` is set by the v1.7 sponsor-allowlist + LLM
  inference path on the wekruit-pa side)

These fields are written to the Postgres `jobs` table for ops/debug
visibility and for the rare case the auto-enrich trigger fails (then
PA falls back to the raw `industry` string for soft scoring).

Tenacity retries handle 429/5xx from the SiliconFlow API.
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
#
# 2026-05-08 — `INDUSTRY_VOCAB` deleted (P7-C unified-canonical-tags). It
# previously shipped 38 abbreviations like `ai_ml`, `c++`, `nextjs` which
# violated v1.6 D5 (NO abbreviations) and confused downstream consumers.
# The canonical 42-token enum lives in
# `wekruit-pa/packages/shared-tags/src/canonical/industry-sector.ts`.
# Macmini emits free-form `industry` strings; the LLM may produce any
# label and PA's `paMatchingJobsAutoEnrich` overwrites with canonical
# `industrySector[]` at Firestore-doc time.
# ---------------------------------------------------------------------------

COMPANY_SIZE_VOCAB: frozenset[str] = frozenset({"startup", "midsize", "large", "unknown"})

# 2026-05-21 — no-hallucination guard. Skill extraction without a real JD
# produced fabricated skills from title+company alone (e.g. "Software
# Engineer @ Acme" → ["python", "javascript", "communication"] with no
# grounding). The classifier now refuses to extract skills when the JD
# is missing or shorter than this threshold. Worker handles the None
# return by skipping the UPDATE — row stays eligible for next run when
# Stage 2b may have landed a real JD.
MIN_JD_CHARS_FOR_SKILLS = 200


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class EnrichmentResult(BaseModel):
    """Validated enrichment output from the LLM classifier.

    `industry` is FREE-FORM informational text (lowercase snake_case) — NOT a
    closed enum. The canonical 42-token `industrySector` enum is enforced on
    the wekruit-pa side via `pa-job-tag-enricher` (paMatchingJobsAutoEnrich
    Firestore trigger). See module docstring for the canonical-tag pipeline.

    null/'unknown' are valid values everywhere — the LLM may produce them
    when the JD doesn't carry a clear signal.
    """
    industry: str
    company_size: str
    required_skills: list[str]
    sponsorship: bool | None = None

    @field_validator("industry")
    @classmethod
    def industry_normalized(cls, v: str) -> str:
        """Lowercase + strip + space->underscore. Default 'unknown'.

        2026-05-08: vocabulary check removed — `industry` is a free-form
        hint, canonical mapping owned by wekruit-pa.
        """
        val = v.lower().strip().replace(" ", "_") if v else "unknown"
        return val or "unknown"

    @field_validator("company_size")
    @classmethod
    def company_size_normalized(cls, v: str) -> str:
        """Normalize to known sizes or 'unknown'."""
        val = v.lower().strip() if v else "unknown"
        if val not in COMPANY_SIZE_VOCAB:
            return "unknown"
        return val

    @field_validator("required_skills")
    @classmethod
    def skills_normalized(cls, v: list[str]) -> list[str]:
        """Normalize skills to lowercase, deduplicate, cap at 15.

        Note: this is the FLAT informational list. Canonical bucketed
        `requiredSkills: Skill[]` (with bucket + baseWeight) is computed by
        wekruit-pa `pa-job-tag-enricher`.
        """
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
  "industry": "<lowercase snake_case industry, e.g. financial_technology, healthcare_and_life_sciences, software_and_saas>",
  "company_size": "<startup | midsize | large | unknown>",
  "skills_inferred": ["<skill1>", "<skill2>", ...],
  "likely_sponsors_visa": <true | false | null>
}

Rules:
- industry: free-form lowercase_snake_case label. Use the most specific term that fits the company; spell out fully (no abbreviations: write "artificial_intelligence_and_machine_learning" not "ai_ml"). Use "unknown" only when truly uncertain.
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


def classify_job(job: Job) -> EnrichmentResult | None:
    """Classify a single job listing — INFORMATIONAL hints only.

    The result populates Postgres `jobs.industry`, `jobs.company_size`,
    `jobs.required_skills` (flat strings), and `jobs.sponsorship` (bool|None).
    These are not authoritative for matching: wekruit-pa's
    `paMatchingJobsAutoEnrich` Firestore trigger overwrites with canonical
    `industrySector[]`, `requiredSkills: Skill[]`, `roleFunction[]`, etc.

    Returns:
      * ``EnrichmentResult`` on success.
      * ``None`` when the job lacks a real JD (length < ``MIN_JD_CHARS_FOR_SKILLS``).
        The caller MUST treat this as "do not write" — leaving ``enriched_at``
        NULL so the row stays eligible for the next pipeline run once Stage 2b
        has landed a JD.
      * ``_safe_default()`` on API/parse failure (never raises). Worker may
        write the safe default; the staleness window will rotate the row back
        into the queue after ``ENRICH_STALE_DAYS``.
    """
    jd_clean = (job.job_description or "").strip()
    if len(jd_clean) < MIN_JD_CHARS_FOR_SKILLS:
        logger.info(
            "skip_no_jd company={} role={} jd_len={} (min={}) — "
            "skill extraction without JD hallucinates, leaving row eligible for next run",
            job.company_name,
            job.role_title,
            len(jd_clean),
            MIN_JD_CHARS_FOR_SKILLS,
        )
        return None

    prompt = (
        f"Company: {job.company_name}\n"
        f"Role: {job.role_title}\n"
        f"Location: {job.location_raw or 'unknown'}\n"
        f"Job Description: {jd_clean[:4000]}"
    )
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
