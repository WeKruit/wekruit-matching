"""Behavioral tests for the hard filter layer.

All tests run without a real DB or API keys — pure unit tests operating on
plain Python dicts and UserProfile models.
"""
import pytest

from wekruit_matching.models.user_profile import JobType, UserProfile
from wekruit_matching.matching.filters import (
    LOCATION_ALIASES,
    apply_hard_filters,
    filter_by_job_type,
    filter_by_location,
    filter_by_sponsorship,
    normalize_location,
)


# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------

def _job(
    source_repo: str = "Summer2026-Internships",
    sponsorship=None,
    location_raw: str = "San Francisco, CA",
) -> dict:
    return {
        "job_id": "a" * 64,
        "source_repo": source_repo,
        "company_name": "Acme",
        "role_title": "SWE Intern",
        "location_raw": location_raw,
        "sponsorship": sponsorship,
    }


def _profile(**kwargs) -> UserProfile:
    defaults = dict(user_id="test-user")
    defaults.update(kwargs)
    return UserProfile(**defaults)


# ---------------------------------------------------------------------------
# TestNormalizeLocation
# ---------------------------------------------------------------------------

class TestNormalizeLocation:
    def test_sf_alias(self):
        assert normalize_location("SF") == "san francisco"

    def test_nyc_alias(self):
        assert normalize_location("NYC") == "new york"

    def test_la_alias(self):
        assert normalize_location("LA") == "los angeles"

    def test_remote_alias(self):
        assert normalize_location("Remote") == "remote"

    def test_case_insensitive(self):
        assert normalize_location("San Francisco, CA") == "san francisco"
        assert normalize_location("NEW YORK, NY".lower()) == "new york"

    def test_unknown_passthrough(self):
        assert normalize_location("Unknown City, TX") == "unknown city, tx"

    def test_strips_whitespace(self):
        assert normalize_location("  SF  ") == "san francisco"

    def test_location_aliases_covers_required_cities(self):
        """LOCATION_ALIASES must cover SF, NYC, LA, Remote, Seattle, Austin, Boston, Chicago."""
        canonical_buckets = set(LOCATION_ALIASES.values())
        required = {"san francisco", "new york", "los angeles", "remote",
                    "seattle", "austin", "boston", "chicago"}
        assert required.issubset(canonical_buckets)


# ---------------------------------------------------------------------------
# TestFilterByJobType
# ---------------------------------------------------------------------------

class TestFilterByJobType:
    def test_any_passes_all(self):
        jobs = [
            _job(source_repo="Summer2026-Internships"),
            _job(source_repo="New-Grad-Positions"),
        ]
        result = filter_by_job_type(jobs, JobType.ANY)
        assert result == jobs

    def test_intern_filter(self):
        intern_job = _job(source_repo="Summer2026-Internships")
        newgrad_job = _job(source_repo="New-Grad-Positions")
        result = filter_by_job_type([intern_job, newgrad_job], JobType.INTERN)
        assert result == [intern_job]

    def test_new_grad_filter(self):
        intern_job = _job(source_repo="Summer2026-Internships")
        newgrad_job = _job(source_repo="New-Grad-Positions")
        result = filter_by_job_type([intern_job, newgrad_job], JobType.NEW_GRAD)
        assert result == [newgrad_job]

    def test_mixed_source_repos(self):
        jobs = [
            _job(source_repo="Summer2026-Internships"),
            _job(source_repo="New-Grad-Positions"),
            _job(source_repo="Summer2026-Internships"),
        ]
        result = filter_by_job_type(jobs, JobType.INTERN)
        assert len(result) == 2
        assert all(j["source_repo"] == "Summer2026-Internships" for j in result)

    def test_empty_jobs_returns_empty(self):
        assert filter_by_job_type([], JobType.INTERN) == []

    def test_intern_filter_returns_only_summer2026_rows(self):
        """Spec truth: job_type='intern' returns only rows where source_repo is Summer2026-Internships."""
        jobs = [
            _job(source_repo="Summer2026-Internships"),
            _job(source_repo="New-Grad-Positions"),
        ]
        result = filter_by_job_type(jobs, JobType.INTERN)
        assert all(j["source_repo"] == "Summer2026-Internships" for j in result)
        assert len(result) == 1

    def test_new_grad_filter_returns_only_new_grad_positions(self):
        """Spec truth: job_type='new_grad' returns only rows where source_repo is New-Grad-Positions."""
        jobs = [
            _job(source_repo="Summer2026-Internships"),
            _job(source_repo="New-Grad-Positions"),
        ]
        result = filter_by_job_type(jobs, JobType.NEW_GRAD)
        assert all(j["source_repo"] == "New-Grad-Positions" for j in result)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# TestFilterBySponsorship
# ---------------------------------------------------------------------------

class TestFilterBySponsorship:
    def test_false_requirement_passes_all(self):
        jobs = [
            _job(sponsorship=True),
            _job(sponsorship=False),
            _job(sponsorship=None),
        ]
        result = filter_by_sponsorship(jobs, requires_sponsorship=False)
        assert result == jobs

    def test_true_requirement_keeps_only_true(self):
        """Spec truth: requires_sponsorship=True keeps only rows where sponsorship is True."""
        jobs = [
            _job(sponsorship=True),
            _job(sponsorship=False),
            _job(sponsorship=None),
        ]
        result = filter_by_sponsorship(jobs, requires_sponsorship=True)
        assert len(result) == 1
        assert result[0]["sponsorship"] is True

    def test_true_requirement_drops_false(self):
        """Spec truth: rows where sponsorship is False are excluded."""
        jobs = [_job(sponsorship=False)]
        result = filter_by_sponsorship(jobs, requires_sponsorship=True)
        assert result == []

    def test_true_requirement_drops_none(self):
        """Spec truth: rows where sponsorship is None are excluded."""
        jobs = [_job(sponsorship=None)]
        result = filter_by_sponsorship(jobs, requires_sponsorship=True)
        assert result == []

    def test_empty_jobs_returns_empty(self):
        assert filter_by_sponsorship([], requires_sponsorship=True) == []

    def test_false_passes_including_none(self):
        """requires_sponsorship=False must pass rows even when sponsorship is None."""
        job = _job(sponsorship=None)
        result = filter_by_sponsorship([job], requires_sponsorship=False)
        assert result == [job]


# ---------------------------------------------------------------------------
# TestFilterByLocation
# ---------------------------------------------------------------------------

class TestFilterByLocation:
    def test_empty_preferred_passes_all(self):
        """Spec truth: empty preferred_locations returns all jobs (no filter)."""
        jobs = [
            _job(location_raw="San Francisco, CA"),
            _job(location_raw="New York, NY"),
            _job(location_raw="Remote"),
        ]
        result = filter_by_location(jobs, preferred_locations=[])
        assert result == jobs

    def test_sf_alias_matches_san_francisco_ca(self):
        """Spec truth: location='SF' matches jobs with location_raw='San Francisco, CA'."""
        job = _job(location_raw="San Francisco, CA")
        result = filter_by_location([job], preferred_locations=["SF"])
        assert result == [job]

    def test_sf_alias_matches_sf_ca(self):
        """Spec truth: location='SF' matches jobs with location_raw='SF, CA'."""
        job = _job(location_raw="SF, CA")
        result = filter_by_location([job], preferred_locations=["SF"])
        assert result == [job]

    def test_sf_alias_no_match_for_new_york(self):
        """Spec truth: location='SF' does NOT match jobs with location_raw='New York, NY'."""
        job = _job(location_raw="New York, NY")
        result = filter_by_location([job], preferred_locations=["SF"])
        assert result == []

    def test_remote_job_matches_any_pref(self):
        """Spec truth: job with location_raw='Remote' matches any non-empty preferred list."""
        job = _job(location_raw="Remote")
        result = filter_by_location([job], preferred_locations=["SF"])
        assert result == [job]

    def test_remote_pref_matches_all_jobs(self):
        """Spec truth: preferred=['Remote'] matches all jobs regardless of their location_raw."""
        jobs = [
            _job(location_raw="San Francisco, CA"),
            _job(location_raw="New York, NY"),
            _job(location_raw="Austin, TX"),
        ]
        result = filter_by_location(jobs, preferred_locations=["Remote"])
        assert result == jobs

    def test_multiple_preferred_locations(self):
        sf_job = _job(location_raw="San Francisco, CA")
        ny_job = _job(location_raw="New York, NY")
        la_job = _job(location_raw="Los Angeles, CA")
        result = filter_by_location([sf_job, ny_job, la_job], preferred_locations=["SF", "NYC"])
        assert sf_job in result
        assert ny_job in result
        assert la_job not in result

    def test_empty_jobs_returns_empty(self):
        result = filter_by_location([], preferred_locations=["SF"])
        assert result == []

    def test_location_with_semicolon_separator(self):
        """Jobs with multiple locations separated by semicolons."""
        job = _job(location_raw="San Francisco, CA; New York, NY")
        # Preferred SF should match
        result = filter_by_location([job], preferred_locations=["SF"])
        assert result == [job]

    def test_non_matching_location_excluded(self):
        job = _job(location_raw="Chicago, IL")
        result = filter_by_location([job], preferred_locations=["SF"])
        assert result == []


# ---------------------------------------------------------------------------
# TestApplyHardFilters
# ---------------------------------------------------------------------------

class TestApplyHardFilters:
    def test_empty_jobs_returns_empty(self):
        """Spec truth: empty input produces empty output."""
        profile = _profile(
            preferred_job_type=JobType.INTERN,
            requires_sponsorship=True,
            preferred_locations=["SF"],
        )
        result = apply_hard_filters([], profile)
        assert result == []

    def test_chains_all_filters(self):
        """apply_hard_filters chains job_type -> sponsorship -> location in order."""
        intern_sf_sponsor = _job(
            source_repo="Summer2026-Internships",
            sponsorship=True,
            location_raw="San Francisco, CA",
        )
        intern_ny_sponsor = _job(
            source_repo="Summer2026-Internships",
            sponsorship=True,
            location_raw="New York, NY",
        )
        intern_sf_no_sponsor = _job(
            source_repo="Summer2026-Internships",
            sponsorship=False,
            location_raw="San Francisco, CA",
        )
        newgrad_sf_sponsor = _job(
            source_repo="New-Grad-Positions",
            sponsorship=True,
            location_raw="San Francisco, CA",
        )

        profile = _profile(
            preferred_job_type=JobType.INTERN,
            requires_sponsorship=True,
            preferred_locations=["SF"],
        )
        result = apply_hard_filters(
            [intern_sf_sponsor, intern_ny_sponsor, intern_sf_no_sponsor, newgrad_sf_sponsor],
            profile,
        )
        # Location is a scoring signal, not a hard filter; both sponsored
        # internship rows survive the chained hard filters.
        assert result == [intern_sf_sponsor, intern_ny_sponsor]

    def test_profile_with_no_constraints_passes_all(self):
        """Profile with ANY job type, no sponsorship requirement, no location preference passes all jobs."""
        jobs = [
            _job(source_repo="Summer2026-Internships", sponsorship=None, location_raw="San Francisco, CA"),
            _job(source_repo="New-Grad-Positions", sponsorship=False, location_raw="New York, NY"),
            _job(source_repo="Summer2026-Internships", sponsorship=True, location_raw="Remote"),
        ]
        profile = _profile(
            preferred_job_type=JobType.ANY,
            requires_sponsorship=False,
            preferred_locations=[],
        )
        result = apply_hard_filters(jobs, profile)
        assert result == jobs

    def test_job_type_filter_applied_first(self):
        """Job type filter excludes new grad rows before other filters see them."""
        intern_job = _job(source_repo="Summer2026-Internships", sponsorship=True, location_raw="Remote")
        newgrad_job = _job(source_repo="New-Grad-Positions", sponsorship=True, location_raw="Remote")
        profile = _profile(preferred_job_type=JobType.INTERN)
        result = apply_hard_filters([intern_job, newgrad_job], profile)
        assert newgrad_job not in result
        assert intern_job in result

    def test_sponsorship_filter_excludes_none(self):
        """Chained: sponsorship=None rows are excluded when requires_sponsorship=True."""
        job_none = _job(sponsorship=None, location_raw="Remote")
        job_true = _job(sponsorship=True, location_raw="Remote")
        profile = _profile(requires_sponsorship=True)
        result = apply_hard_filters([job_none, job_true], profile)
        assert job_none not in result
        assert job_true in result

    def test_location_preference_is_not_a_chained_hard_filter(self):
        """Chained filters keep non-matching locations for scoring."""
        sf_job = _job(location_raw="San Francisco, CA")
        ny_job = _job(location_raw="New York, NY")
        profile = _profile(preferred_locations=["SF"])
        result = apply_hard_filters([sf_job, ny_job], profile)
        assert sf_job in result
        assert ny_job in result
