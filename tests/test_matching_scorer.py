"""Unit tests for the 7-signal weighted job scorer.

All tests run without a DB connection or API keys.
Tests written in TDD RED phase — should fail before scorer.py is implemented.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from wekruit_matching.matching.scorer import (
    DERIVED_EXPERIENCE_WEIGHTS,
    WEIGHTS,
    score_skill_depth_bonus,
    score_skill_recency_bonus,
    score_seniority_alignment,
    score_title_similarity,
    score_skills_overlap,
    score_industry_match,
    score_company_size_match,
    score_location_fit,
    score_recency,
    score_feedback_boost,
    score_job,
)
from wekruit_matching.models.user_profile import CompanySizePreference, UserProfile


# ---------------------------------------------------------------------------
# WEIGHTS
# ---------------------------------------------------------------------------


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# score_title_similarity
# ---------------------------------------------------------------------------


def test_score_title_similarity_perfect():
    v = [1.0] + [0.0] * 1535
    result = score_title_similarity(query_embedding=v, job_embedding=v)
    assert abs(result - 1.0) < 1e-6


def test_score_title_similarity_no_embedding():
    result = score_title_similarity(query_embedding=[1.0] * 1536, job_embedding=None)
    assert result == 0.0


def test_score_title_similarity_orthogonal():
    """Orthogonal vectors should have cosine similarity 0."""
    a = [1.0] + [0.0] * 1535
    b = [0.0, 1.0] + [0.0] * 1534
    result = score_title_similarity(query_embedding=a, job_embedding=b)
    assert abs(result) < 1e-6


def test_score_title_similarity_empty_embedding():
    result = score_title_similarity(query_embedding=[1.0] * 1536, job_embedding=[])
    assert result == 0.0


# ---------------------------------------------------------------------------
# score_skills_overlap
# ---------------------------------------------------------------------------


def test_score_skills_overlap_full():
    result = score_skills_overlap(user_skills=["python", "sql"], job_skills=["python", "sql"])
    assert result == 1.0


def test_score_skills_overlap_partial():
    result = score_skills_overlap(user_skills=["python"], job_skills=["python", "sql"])
    assert abs(result - 0.575) < 1e-9


def test_score_skills_overlap_no_job_skills():
    result = score_skills_overlap(user_skills=["python"], job_skills=[])
    assert result == 0.0


def test_score_skills_overlap_case_insensitive():
    result = score_skills_overlap(user_skills=["Python"], job_skills=["python"])
    assert result == 1.0


def test_score_skills_overlap_no_user_skills():
    result = score_skills_overlap(user_skills=[], job_skills=["python", "sql"])
    assert result == 0.0


# ---------------------------------------------------------------------------
# score_industry_match
# ---------------------------------------------------------------------------


def test_score_industry_match_hit():
    result = score_industry_match(job_industry="fintech", preferred_industries=["fintech", "healthtech"])
    assert result == 1.0


def test_score_industry_match_miss():
    result = score_industry_match(job_industry="fintech", preferred_industries=["healthtech"])
    assert result == 0.3


def test_score_industry_match_no_preference():
    result = score_industry_match(job_industry="fintech", preferred_industries=[])
    assert result == 0.3


def test_score_industry_match_case_insensitive():
    result = score_industry_match(job_industry="FinTech", preferred_industries=["fintech"])
    assert result == 1.0


def test_score_industry_match_none_industry():
    result = score_industry_match(job_industry=None, preferred_industries=["fintech"])
    assert result == 0.3


# ---------------------------------------------------------------------------
# score_company_size_match
# ---------------------------------------------------------------------------


def test_score_company_size_match_exact():
    result = score_company_size_match(job_size="startup", preferred_size="startup")
    assert result == 1.0


def test_score_company_size_match_any():
    result = score_company_size_match(job_size="large", preferred_size="any")
    assert result == 1.0


def test_score_company_size_match_mismatch():
    result = score_company_size_match(job_size="large", preferred_size="startup")
    assert result == 0.4


def test_score_company_size_match_none_job_size():
    result = score_company_size_match(job_size=None, preferred_size="startup")
    assert result == 0.4


def test_score_company_size_match_case_insensitive():
    result = score_company_size_match(job_size="Startup", preferred_size="startup")
    assert result == 1.0


# ---------------------------------------------------------------------------
# score_location_fit
# ---------------------------------------------------------------------------


def test_score_location_fit_match():
    result = score_location_fit(location_raw="San Francisco, CA", preferred_locations=["SF"])
    assert result == 1.0


def test_score_location_fit_remote_job():
    result = score_location_fit(location_raw="Remote", preferred_locations=["SF"])
    assert result == 1.0


def test_score_location_fit_no_preference():
    result = score_location_fit(location_raw="Austin, TX", preferred_locations=[])
    assert result == 1.0


def test_score_location_fit_mismatch():
    result = score_location_fit(location_raw="Austin, TX", preferred_locations=["SF"])
    assert result == 0.2


def test_score_location_fit_user_prefers_remote():
    """If user prefers remote, all jobs should match."""
    result = score_location_fit(location_raw="Austin, TX", preferred_locations=["Remote"])
    assert result == 1.0


# ---------------------------------------------------------------------------
# score_recency
# ---------------------------------------------------------------------------


def test_score_recency_today():
    now = datetime.now(timezone.utc)
    result = score_recency(first_seen_at=now)
    assert abs(result - 1.0) < 0.01


def test_score_recency_30_days():
    old = datetime.now(timezone.utc) - timedelta(days=30)
    result = score_recency(first_seen_at=old)
    assert result == 0.0


def test_score_recency_15_days():
    mid = datetime.now(timezone.utc) - timedelta(days=15)
    result = score_recency(first_seen_at=mid)
    assert abs(result - 0.5) < 0.05


def test_score_recency_very_old():
    """Jobs older than 30 days should score 0.0, not negative."""
    ancient = datetime.now(timezone.utc) - timedelta(days=90)
    result = score_recency(first_seen_at=ancient)
    assert result == 0.0


# ---------------------------------------------------------------------------
# score_feedback_boost
# ---------------------------------------------------------------------------


def test_score_feedback_boost_liked():
    result = score_feedback_boost(company_name="Acme", liked=["Acme"], disliked=[])
    assert result == 1.0


def test_score_feedback_boost_disliked():
    result = score_feedback_boost(company_name="Acme", liked=[], disliked=["Acme"])
    assert result == 0.0


def test_score_feedback_boost_cold_start():
    result = score_feedback_boost(company_name="Acme", liked=[], disliked=[])
    assert result == 0.5


def test_score_feedback_boost_case_insensitive():
    result = score_feedback_boost(company_name="acme", liked=["Acme"], disliked=[])
    assert result == 1.0


# ---------------------------------------------------------------------------
# derivedExperience signals
# ---------------------------------------------------------------------------


def test_derived_experience_weights_reserve_at_most_15_percent():
    assert sum(DERIVED_EXPERIENCE_WEIGHTS.values()) <= 0.15


def test_score_skill_depth_bonus_uses_years_per_required_skill():
    profile = UserProfile(
        user_id="u-depth",
        skills=["python", "react"],
        derivedExperience={
            "version": "v1",
            "yearsTotal": 5,
            "yearsPerSkill": {"python": 5, "react": 2},
            "skillRecency": {},
            "titleTrajectory": [],
            "seniorityCurrent": "entry_level",
            "responsibilityCurrent": "individual_contributor",
            "industryHistory": {},
            "unverifiedSkills": [],
            "computedAt": "2026-05-22T12:00:00Z",
        },
    )

    result = score_skill_depth_bonus(["python", "react"], profile)

    assert abs(result - 0.7) < 1e-9


def test_score_skill_recency_bonus_decays_old_mentions():
    profile = UserProfile(
        user_id="u-recency",
        skills=["python", "react"],
        derivedExperience={
            "version": "v1",
            "yearsTotal": 5,
            "yearsPerSkill": {"python": 5, "react": 2},
            "skillRecency": {"python": "present", "react": "2020-01-01"},
            "titleTrajectory": [],
            "seniorityCurrent": "entry_level",
            "responsibilityCurrent": "individual_contributor",
            "industryHistory": {},
            "unverifiedSkills": [],
            "computedAt": "2026-05-22T12:00:00Z",
        },
    )
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)

    result = score_skill_recency_bonus(["python", "react"], profile, now=now)

    assert abs(result - 0.55) < 1e-9


def test_score_seniority_alignment_matches_current_level_to_job_level():
    profile = UserProfile(
        user_id="u-seniority",
        derivedExperience={
            "version": "v1",
            "yearsTotal": 1,
            "yearsPerSkill": {},
            "skillRecency": {},
            "titleTrajectory": ["Software Engineer Intern"],
            "seniorityCurrent": "new_grad",
            "responsibilityCurrent": "individual_contributor",
            "industryHistory": {},
            "unverifiedSkills": [],
            "computedAt": "2026-05-22T12:00:00Z",
        },
    )

    assert score_seniority_alignment("new_grad", profile) == 1.0
    assert score_seniority_alignment("intern", profile) == 0.75
    assert score_seniority_alignment("senior", profile) == 0.2


# ---------------------------------------------------------------------------
# score_job (integration tests)
# ---------------------------------------------------------------------------


def test_score_job_returns_score_and_signals():
    """score_job() must return dict with 'score' float and 'signals' dict of 7 keys."""
    job = {
        "job_id": "a" * 64,
        "company_name": "Acme",
        "role_title": "SWE",
        "location_raw": "Remote",
        "first_seen_at": datetime.now(timezone.utc),
        "required_skills": [],
        "industry": None,
        "company_size": None,
        "embedding": None,
    }
    profile = UserProfile(user_id="u1")
    query_emb = [0.0] * 1536
    result = score_job(job=job, profile=profile, query_embedding=query_emb)
    assert "score" in result
    assert "signals" in result
    assert set(result["signals"].keys()) == {
        "title_similarity",
        "skills_overlap",
        "industry_match",
        "company_size_match",
        "location_fit",
        "recency",
        "feedback_boost",
    }
    assert 0.0 <= result["score"] <= 1.0


def test_score_job_weighted_sum_correct():
    """All signals 1.0 => score should be ~1.0."""
    job = {
        "job_id": "b" * 64,
        "company_name": "Stripe",
        "role_title": "SWE",
        "location_raw": "Remote",
        "first_seen_at": datetime.now(timezone.utc),
        "required_skills": ["python"],
        "industry": "fintech",
        "company_size": "large",
        "embedding": [1.0] + [0.0] * 1535,
    }
    profile = UserProfile(
        user_id="u1",
        skills=["python"],
        preferred_industries=["fintech"],
        preferred_company_size=CompanySizePreference.LARGE,
        preferred_locations=[],
        liked_companies=["Stripe"],
        disliked_companies=[],
    )
    query_emb = [1.0] + [0.0] * 1535
    result = score_job(job=job, profile=profile, query_embedding=query_emb)
    # All signals should be 1.0 -> score should be 1.0 (or very close)
    assert result["score"] > 0.99


def test_score_job_cold_start_feedback_boost():
    """Cold-start profile (no liked/disliked) yields feedback_boost == 0.5 (MTCH-13)."""
    job = {
        "job_id": "c" * 64,
        "company_name": "NewCo",
        "role_title": "Engineer",
        "location_raw": "Remote",
        "first_seen_at": datetime.now(timezone.utc),
        "required_skills": [],
        "industry": None,
        "company_size": None,
        "embedding": None,
    }
    profile = UserProfile(user_id="u2")  # no liked or disliked
    result = score_job(job=job, profile=profile, query_embedding=[0.0] * 1536)
    assert result["signals"]["feedback_boost"] == 0.5


def test_score_job_score_is_rounded():
    """score_job() rounds the final score to 6 decimal places."""
    job = {
        "job_id": "d" * 64,
        "company_name": "RoundCo",
        "role_title": "Dev",
        "location_raw": "Remote",
        "first_seen_at": datetime.now(timezone.utc),
        "required_skills": [],
        "industry": None,
        "company_size": None,
        "embedding": None,
    }
    profile = UserProfile(user_id="u3")
    result = score_job(job=job, profile=profile, query_embedding=[0.0] * 1536)
    # Rounded to 6 places means len(str(result["score"]).split(".")[-1]) <= 6
    score_str = str(result["score"])
    if "." in score_str:
        decimal_places = len(score_str.split(".")[-1])
        assert decimal_places <= 6


def test_score_job_feature_flag_off_ignores_derived_experience():
    """Flag off must preserve the existing 7-signal score exactly."""
    job = {
        "job_id": "e" * 64,
        "company_name": "DepthCo",
        "role_title": "Software Engineer",
        "location_raw": "Remote",
        "first_seen_at": datetime.now(timezone.utc),
        "required_skills": ["python"],
        "industry": None,
        "company_size": None,
        "seniority_level": "entry_level",
        "embedding": None,
    }
    profile = UserProfile(
        user_id="u-derived-off",
        skills=["python"],
        derivedExperience={
            "version": "v1",
            "yearsTotal": 5,
            "yearsPerSkill": {"python": 5},
            "skillRecency": {"python": "present"},
            "titleTrajectory": ["Software Engineer"],
            "seniorityCurrent": "entry_level",
            "responsibilityCurrent": "individual_contributor",
            "industryHistory": {},
            "unverifiedSkills": [],
            "computedAt": "2026-05-22T12:00:00Z",
        },
    )

    with_derived_flag_off = score_job(
        job=job,
        profile=profile,
        query_embedding=[0.0] * 1536,
        use_derived_experience=False,
    )
    without_derived = score_job(
        job=job,
        profile=UserProfile(user_id="u-derived-off", skills=["python"]),
        query_embedding=[0.0] * 1536,
        use_derived_experience=False,
    )

    assert with_derived_flag_off["score"] == without_derived["score"]
    assert set(with_derived_flag_off["signals"].keys()) == set(WEIGHTS.keys())


def test_score_job_feature_flag_on_adds_derived_signals_and_explanation():
    job = {
        "job_id": "f" * 64,
        "company_name": "ExplainCo",
        "role_title": "Software Engineer",
        "location_raw": "Remote",
        "first_seen_at": datetime.now(timezone.utc),
        "required_skills": ["python", "react"],
        "industry": None,
        "company_size": None,
        "seniority_level": "entry_level",
        "embedding": None,
    }
    profile = UserProfile(
        user_id="u-derived-on",
        skills=["python", "react"],
        derivedExperience={
            "version": "v1",
            "yearsTotal": 5,
            "yearsPerSkill": {"python": 5, "react": 2},
            "skillRecency": {"python": "present", "react": "2025-08-01"},
            "titleTrajectory": ["Software Engineer Intern", "Software Engineer"],
            "seniorityCurrent": "entry_level",
            "responsibilityCurrent": "individual_contributor",
            "industryHistory": {},
            "unverifiedSkills": [],
            "computedAt": "2026-05-22T12:00:00Z",
        },
    )

    result = score_job(
        job=job,
        profile=profile,
        query_embedding=[0.0] * 1536,
        use_derived_experience=True,
        include_explanation=True,
    )

    assert set(DERIVED_EXPERIENCE_WEIGHTS).issubset(result["signals"].keys())
    assert result["signals"]["skill_depth_bonus"] == pytest.approx(0.7)
    assert result["signals"]["skill_recency_bonus"] == pytest.approx(1.0)
    assert result["signals"]["seniority_alignment"] == 1.0
    assert result["explanation"] == "matched on python (5y, recent) + react (2y, recent)"


def test_score_job_feature_flag_on_legacy_null_profile_keeps_original_score():
    """Legacy users without derivedExperience must keep the original score path."""
    job = {
        "job_id": "g" * 64,
        "company_name": "LegacyCo",
        "role_title": "Software Engineer",
        "location_raw": "Remote",
        "first_seen_at": datetime.now(timezone.utc),
        "required_skills": ["python"],
        "industry": "tech",
        "company_size": "startup",
        "seniority_level": "entry_level",
        "embedding": [1.0] + [0.0] * 1535,
    }
    profile = UserProfile(
        user_id="u-legacy",
        skills=["python"],
        totalYearsExperience=3,
        preferred_industries=["tech"],
    )
    query_emb = [1.0] + [0.0] * 1535

    flag_off = score_job(
        job=job,
        profile=profile,
        query_embedding=query_emb,
        use_derived_experience=False,
    )
    flag_on = score_job(
        job=job,
        profile=profile,
        query_embedding=query_emb,
        use_derived_experience=True,
    )

    assert flag_on["score"] == flag_off["score"]
    assert flag_on["signals"] == flag_off["signals"]
