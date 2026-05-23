"""7-signal weighted job scorer.

Computes a composite job match score from 7 independent signals, each weighted
according to the specification. No DB or API calls occur inside this module —
all inputs are provided by the caller (matcher.py in Plan 02).

Public API:
    WEIGHTS: dict[str, float]           -- signal name -> weight (sums to 1.0)
    score_title_similarity(...)  -> float
    score_skills_overlap(...)    -> float
    score_industry_match(...)    -> float
    score_company_size_match(...) -> float
    score_location_fit(...)      -> float
    score_recency(...)           -> float
    score_feedback_boost(...)    -> float
    score_job(...)               -> dict[str, float | dict[str, float]]
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from wekruit_matching.matching.filters import _job_location_buckets, _preferred_buckets
from wekruit_matching.models.user_profile import UserProfile

# ---------------------------------------------------------------------------
# Weights — must sum to exactly 1.0 (verified by test_weights_sum_to_one)
# ---------------------------------------------------------------------------

WEIGHTS: dict[str, float] = {
    "title_similarity": 0.30,
    "skills_overlap": 0.25,
    "industry_match": 0.10,
    "company_size_match": 0.05,
    "location_fit": 0.10,
    "recency": 0.10,
    "feedback_boost": 0.10,
}

DERIVED_EXPERIENCE_WEIGHTS: dict[str, float] = {
    "skill_depth_bonus": 0.06,
    "skill_recency_bonus": 0.05,
    "seniority_alignment": 0.04,
}


# ---------------------------------------------------------------------------
# Signal functions
# ---------------------------------------------------------------------------


def score_title_similarity(
    query_embedding: list[float],
    job_embedding: list[float] | None,
) -> float:
    """Cosine similarity between query embedding and job embedding.

    Returns 0.0 if job_embedding is None or empty (no embedding available).
    Clamps result to [0.0, 1.0].
    """
    if not job_embedding:
        return 0.0

    a = np.array(query_embedding, dtype=np.float64)
    b = np.array(job_embedding, dtype=np.float64)

    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    cosine_sim = np.dot(a, b) / (norm_a * norm_b + 1e-9)

    return float(np.clip(cosine_sim, 0.0, 1.0))


def score_skills_overlap(
    user_skills: list[str],
    job_skills: list[str],
) -> float:
    """Coverage-dominant skill overlap score.

    coverage  = what fraction of job's required skills does user have (0-1)
    relevance = what fraction of user's skills does this job use (0-1)
    score     = coverage * 0.85 + relevance * 0.15

    Coverage is what matters for job matching ("do I qualify?"). Relevance
    provides a small tiebreaker bonus for jobs that use more of the user's
    skillset. The old geometric mean formula (sqrt(cov * rel)) punished
    users with many skills: 5/6 match with 53 skills → only 28%. Now → 72%.

    Returns 0.0 if job or user has no skills.
    Case-insensitive comparison.
    """
    if not job_skills or not user_skills:
        return 0.0

    user_set = {s.lower() for s in user_skills}
    job_set = {s.lower() for s in job_skills}
    matched = len(user_set & job_set)

    coverage = matched / len(job_set)
    relevance = matched / len(user_set)

    return coverage * 0.85 + relevance * 0.15


def score_industry_match(
    job_industry: str | None,
    preferred_industries: list[str],
) -> float:
    """1.0 on exact industry match, 0.3 otherwise.

    Returns 0.3 if user has no industry preference (empty list) or if
    job has no industry (None).
    """
    if not preferred_industries:
        return 0.3

    if job_industry is None:
        return 0.3

    preferred_set = {i.lower() for i in preferred_industries}
    return 1.0 if job_industry.lower() in preferred_set else 0.3


def score_company_size_match(
    job_size: str | None,
    preferred_size: str,
) -> float:
    """1.0 on match or when preferred_size is 'any', 0.4 otherwise.

    Returns 0.4 if job has no company size (None).
    Case-insensitive comparison.
    """
    if preferred_size.lower() == "any":
        return 1.0

    if job_size is None:
        return 0.4

    return 1.0 if job_size.lower() == preferred_size.lower() else 0.4


def score_location_fit(
    location_raw: str,
    preferred_locations: list[str],
) -> float:
    """1.0 if location matches user preference or job/preference is remote, 0.2 otherwise.

    Rules:
    - No user preference (empty list): 1.0 (no filter)
    - User prefers remote: 1.0 (matches any job)
    - Job location normalizes to remote: 1.0 (remote jobs are universal)
    - Job location buckets intersect preferred buckets: 1.0
    - Otherwise: 0.2
    """
    if not preferred_locations:
        return 1.0

    pref_buckets = _preferred_buckets(preferred_locations)

    # User prefers remote — all jobs match
    if "remote" in pref_buckets:
        return 1.0

    job_buckets = _job_location_buckets(location_raw)

    # Remote job matches any non-empty preference
    if "remote" in job_buckets:
        return 1.0

    return 1.0 if job_buckets & pref_buckets else 0.2


def score_recency(first_seen_at: datetime) -> float:
    """Linear decay from 1.0 (today) to 0.0 (30 days old), clamped to [0.0, 1.0].

    Uses days (integer) since first_seen_at. Listings 30+ days old score 0.0.
    """
    days_old = (datetime.now(UTC) - first_seen_at).days
    return max(0.0, 1.0 - days_old / 30.0)


def score_feedback_boost(
    company_name: str,
    liked: list[str],
    disliked: list[str],
) -> float:
    """Feedback signal based on user's company like/dislike history.

    Returns:
        1.0  — company is in liked list
        0.0  — company is in disliked list
        0.5  — cold-start / neutral (not in either list)

    Case-insensitive comparison.
    """
    name_lower = company_name.lower()
    liked_set = {c.lower() for c in liked}
    disliked_set = {c.lower() for c in disliked}

    if name_lower in liked_set:
        return 1.0
    if name_lower in disliked_set:
        return 0.0
    return 0.5


def _normalize_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _normalized_skill_years(profile: UserProfile) -> dict[str, float]:
    exp = profile.derived_experience
    if not exp:
        return {}
    result: dict[str, float] = {}
    for skill, years in exp.years_per_skill.items():
        try:
            result[_normalize_token(skill)] = max(0.0, float(years))
        except (TypeError, ValueError):
            continue
    return result


def _job_skill_keys(job_skills: list[str]) -> list[str]:
    return sorted({_normalize_token(skill) for skill in job_skills if skill})


def score_skill_depth_bonus(job_skills: list[str], profile: UserProfile) -> float:
    """Average per-required-skill depth, with 5 years treated as full credit."""
    skill_keys = _job_skill_keys(job_skills)
    if not skill_keys:
        return 0.0

    years_by_skill = _normalized_skill_years(profile)
    if years_by_skill:
        values = [min(years_by_skill.get(skill, 0.0) / 5.0, 1.0) for skill in skill_keys]
        return sum(values) / len(values)

    if profile.total_years_experience is None:
        return 0.0

    user_skills = {_normalize_token(skill) for skill in profile.skills}
    values = [
        min(max(profile.total_years_experience, 0.0) / 5.0, 1.0)
        if skill in user_skills
        else 0.0
        for skill in skill_keys
    ]
    return sum(values) / len(values)


def _recency_value_score(value: str | None, now: datetime) -> float:
    if not value:
        return 0.0
    if value.lower() == "present":
        return 1.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{value}T00:00:00+00:00")
        except ValueError:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    days_old = max(0, (now - parsed).days)
    if days_old <= 365:
        return 1.0
    if days_old <= 365 * 3:
        return 0.7
    if days_old <= 365 * 5:
        return 0.4
    return 0.1


def score_skill_recency_bonus(
    job_skills: list[str],
    profile: UserProfile,
    *,
    now: datetime | None = None,
) -> float:
    """Average recency score for required skills found in PA work history."""
    skill_keys = _job_skill_keys(job_skills)
    exp = profile.derived_experience
    if not skill_keys or not exp:
        return 0.0

    active_now = now or datetime.now(UTC)
    recency = {
        _normalize_token(skill): value
        for skill, value in exp.skill_recency.items()
        if isinstance(value, str)
    }
    values = [_recency_value_score(recency.get(skill), active_now) for skill in skill_keys]
    return sum(values) / len(values)


_SENIORITY_ORDER = {
    "intern": 0,
    "new_grad": 1,
    "entry_level": 1,
    "mid": 2,
    "senior": 3,
    "staff": 4,
    "manager": 4,
    "director": 5,
    "executive": 6,
}

_SENIORITY_ALIASES = {
    "entry": "entry_level",
    "junior": "entry_level",
    "newgrad": "new_grad",
    "new_grad": "new_grad",
    "lead": "staff",
}


def _normalize_seniority(value: str | None) -> str:
    if not value:
        return "unknown"
    normalized = _normalize_token(value)
    return _SENIORITY_ALIASES.get(normalized, normalized)


def _fallback_seniority_from_years(total_years: float | None) -> str:
    if total_years is None:
        return "unknown"
    if total_years < 1:
        return "intern"
    if total_years < 3:
        return "entry_level"
    if total_years < 6:
        return "mid"
    return "senior"


def score_seniority_alignment(job_seniority: str | None, profile: UserProfile) -> float:
    """Score current user seniority against the job's seniority level."""
    exp = profile.derived_experience
    user_level = (
        _normalize_seniority(exp.seniority_current)
        if exp
        else _fallback_seniority_from_years(profile.total_years_experience)
    )
    job_level = _normalize_seniority(job_seniority)

    if user_level == "unknown" or job_level == "unknown":
        return 0.5
    if user_level == job_level:
        return 1.0

    user_rank = _SENIORITY_ORDER.get(user_level)
    job_rank = _SENIORITY_ORDER.get(job_level)
    if user_rank is None or job_rank is None:
        return 0.5

    distance = abs(user_rank - job_rank)
    if distance == 1:
        return 0.75
    return 0.2


def _format_years(years: float) -> str:
    if abs(years - round(years)) < 1e-9:
        return f"{int(round(years))}y"
    return f"{years:.1f}".rstrip("0").rstrip(".") + "y"


def _build_explanation(
    job_skills: list[str],
    profile: UserProfile,
    signals: dict[str, float],
) -> str:
    user_skills = {_normalize_token(skill) for skill in profile.skills}
    years_by_skill = _normalized_skill_years(profile)
    derived_skills = set(years_by_skill)
    matched = [
        skill
        for skill in _job_skill_keys(job_skills)
        if skill in user_skills | derived_skills
    ]

    if matched:
        now = datetime.now(UTC)
        recency = (
            {
                _normalize_token(skill): value
                for skill, value in profile.derived_experience.skill_recency.items()
            }
            if profile.derived_experience
            else {}
        )
        parts = []
        for skill in matched[:3]:
            years = years_by_skill.get(skill)
            recent = _recency_value_score(recency.get(skill), now) >= 0.7
            if years is not None and years > 0:
                suffix = f"{_format_years(years)}"
                if recent:
                    suffix += ", recent"
                parts.append(f"{skill} ({suffix})")
            else:
                parts.append(skill)
        return "matched on " + " + ".join(parts)

    if signals.get("seniority_alignment", 0.0) >= 0.75:
        return "seniority aligns with role level"
    if signals.get("title_similarity", 0.0) >= 0.7:
        return "strong title similarity"
    if signals.get("location_fit", 0.0) >= 1.0:
        return "location preference matches"
    return "balanced fit across matching signals"


# ---------------------------------------------------------------------------
# Combinator
# ---------------------------------------------------------------------------


def score_job(
    job: dict,
    profile: UserProfile,
    query_embedding: list[float],
    *,
    use_derived_experience: bool = False,
    include_explanation: bool = False,
) -> dict:
    """Compute the composite match score for a single job.

    All inputs must be pre-loaded by the caller; no DB or API calls occur here.

    Args:
        job: Job dict from DB query. Required keys:
             company_name, location_raw, first_seen_at,
             required_skills, industry, company_size, embedding.
        profile: UserProfile with skills, preferences, and feedback history.
        query_embedding: Pre-computed 1536-dim query vector representing the
                         user's current job search intent.

    Returns:
        {
            "score": float (0.0-1.0, rounded to 6 decimal places),
            "signals": {
                "title_similarity": float,
                "skills_overlap": float,
                "industry_match": float,
                "company_size_match": float,
                "location_fit": float,
                "recency": float,
                "feedback_boost": float,
            },
        }
    """
    job_skills = job.get("required_skills") or []
    user_set = {_normalize_token(s) for s in profile.skills}
    job_set = {_normalize_token(s) for s in job_skills}
    matched_skills = sorted(user_set & job_set)

    signals: dict[str, float] = {
        "title_similarity": score_title_similarity(
            query_embedding=query_embedding,
            job_embedding=job.get("embedding"),
        ),
        "skills_overlap": score_skills_overlap(
            user_skills=profile.skills,
            job_skills=job_skills,
        ),
        "industry_match": score_industry_match(
            job_industry=job.get("industry"),
            preferred_industries=profile.preferred_industries,
        ),
        "company_size_match": score_company_size_match(
            job_size=job.get("company_size"),
            preferred_size=profile.preferred_company_size.value,
        ),
        "location_fit": score_location_fit(
            location_raw=job.get("location_raw") or "",
            preferred_locations=profile.preferred_locations,
        ),
        "recency": score_recency(
            first_seen_at=job["first_seen_at"],
        ),
        "feedback_boost": score_feedback_boost(
            company_name=job.get("company_name") or "",
            liked=profile.liked_companies,
            disliked=profile.disliked_companies,
        ),
    }

    derived_enabled = use_derived_experience and profile.derived_experience is not None
    if derived_enabled:
        signals.update(
            {
                "skill_depth_bonus": score_skill_depth_bonus(job_skills, profile),
                "skill_recency_bonus": score_skill_recency_bonus(job_skills, profile),
                "seniority_alignment": score_seniority_alignment(
                    job.get("seniority_level"),
                    profile,
                ),
            }
        )
        base_weight = 1.0 - sum(DERIVED_EXPERIENCE_WEIGHTS.values())
        score = (
            base_weight * sum(WEIGHTS[k] * signals[k] for k in WEIGHTS)
            + sum(DERIVED_EXPERIENCE_WEIGHTS[k] * signals[k] for k in DERIVED_EXPERIENCE_WEIGHTS)
        )
    else:
        score = sum(WEIGHTS[k] * signals[k] for k in WEIGHTS)

    result = {
        "score": round(score, 6),
        "signals": signals,
        "matched_skills": matched_skills,
        "required_skills": job_skills,
    }
    if include_explanation:
        result["explanation"] = _build_explanation(job_skills, profile, signals)
    return result
