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

from datetime import datetime, timezone
from typing import Optional

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


# ---------------------------------------------------------------------------
# Signal functions
# ---------------------------------------------------------------------------


def score_title_similarity(
    query_embedding: list[float],
    job_embedding: Optional[list[float]],
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
    """Geometric mean of coverage and relevance for better discrimination.

    coverage  = what fraction of job's required skills does user have (0-1)
    relevance = what fraction of user's skills does this job use (0-1)
    score     = sqrt(coverage * relevance)

    This discriminates better than pure coverage for users with many skills:
    - User has 50 skills, job needs [Python] → coverage=1.0, relevance=0.02 → score=0.14
    - User has 50 skills, job needs [Python,React,TS,Docker,AWS] → cov=1.0, rel=0.10 → score=0.32

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

    return (coverage * relevance) ** 0.5


def score_industry_match(
    job_industry: Optional[str],
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
    job_size: Optional[str],
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
    days_old = (datetime.now(timezone.utc) - first_seen_at).days
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


# ---------------------------------------------------------------------------
# Combinator
# ---------------------------------------------------------------------------


def score_job(
    job: dict,
    profile: UserProfile,
    query_embedding: list[float],
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
    user_set = {s.lower() for s in profile.skills}
    job_set = {s.lower() for s in job_skills}
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

    score = sum(WEIGHTS[k] * signals[k] for k in WEIGHTS)

    return {
        "score": round(score, 6),
        "signals": signals,
        "matched_skills": matched_skills,
        "required_skills": job_skills,
    }
