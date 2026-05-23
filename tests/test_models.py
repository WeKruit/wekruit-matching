"""Tests for Pydantic v2 data models (FOUND-05)."""
import pytest
from pydantic import ValidationError
from wekruit_matching.models import (
    DerivedExperience,
    Feedback,
    Job,
    JobStatus,
    JobType,
    ReactionType,
    UserProfile,
)


def test_job_valid():
    """Job with required fields validates successfully."""
    job = Job(
        job_id="a" * 64,
        source_repo="Summer2026-Internships",
        company_name="Acme Corp",
        role_title="Software Engineer Intern",
    )
    assert job.status == JobStatus.ACTIVE
    assert job.required_skills == []


def test_job_invalid_content_hash():
    """Job with malformed content_hash raises ValidationError."""
    with pytest.raises(ValidationError):
        Job(
            job_id="a" * 64,
            source_repo="Summer2026-Internships",
            company_name="Acme",
            role_title="SWE",
            content_hash="not-a-hash",
        )


def test_job_valid_content_hash():
    """Job with a 64-char hex content_hash accepts successfully."""
    import hashlib
    h = hashlib.sha256(b"test").hexdigest()
    job = Job(
        job_id="a" * 64,
        source_repo="Summer2026-Internships",
        company_name="Acme",
        role_title="SWE",
        content_hash=h,
    )
    assert len(job.content_hash) == 64


def test_user_profile_defaults():
    """UserProfile with only user_id has sane defaults."""
    profile = UserProfile(user_id="user-123")
    assert profile.preferred_job_type == JobType.ANY
    assert profile.skills == []
    assert profile.requires_sponsorship is False
    assert profile.affinity_embedding is None


def test_user_profile_with_skills():
    """UserProfile accepts skills list and preferred_locations."""
    profile = UserProfile(
        user_id="user-123",
        skills=["Python", "SQL"],
        preferred_locations=["San Francisco", "Remote"],
        preferred_job_type=JobType.INTERN,
    )
    assert len(profile.skills) == 2
    assert profile.preferred_job_type == JobType.INTERN


def test_user_profile_accepts_derived_experience_wire_contract():
    """UserProfile keeps PA's derivedExperience fields optional and typed."""
    profile = UserProfile(
        user_id="user-123",
        skills=["Python", "React"],
        totalYearsExperience=5,
        derivedExperience={
            "version": "v1",
            "yearsTotal": 5,
            "yearsPerSkill": {"python": 5, "react": 2},
            "skillRecency": {"python": "present", "react": "2025-08-01"},
            "titleTrajectory": ["Software Engineer Intern", "Software Engineer"],
            "seniorityCurrent": "entry_level",
            "responsibilityCurrent": "individual_contributor",
            "industryHistory": {"fintech": 3, "saas": 2},
            "unverifiedSkills": ["kubernetes"],
            "computedAt": "2026-05-22T12:00:00Z",
        },
        derivedExperienceVersion="v1",
        derivedExperienceContentHash="abc123",
    )

    assert isinstance(profile.derived_experience, DerivedExperience)
    assert profile.derived_experience.years_per_skill["python"] == 5
    assert profile.derived_experience.skill_recency["python"] == "present"
    assert profile.derived_experience.seniority_current == "entry_level"
    assert profile.total_years_experience == 5


def test_feedback_valid_reaction():
    """Feedback with reaction='like' succeeds."""
    fb = Feedback(user_id="user-1", job_id="a" * 64, reaction=ReactionType.LIKE)
    assert fb.reaction == ReactionType.LIKE


def test_feedback_invalid_reaction():
    """Feedback with unknown reaction string raises ValidationError."""
    with pytest.raises(ValidationError):
        Feedback(user_id="user-1", job_id="a" * 64, reaction="invalid")
