"""Unit tests for get_matches() in matcher.py.

All DB and OpenAI calls are mocked — no real connections required.
Tests verify ANN retrieval -> hard filters -> scoring pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from wekruit_matching.models.user_profile import (
    CompanySizePreference,
    JobType,
    UserProfile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_EMBEDDING = [0.1] * 1536


def _make_profile(**kwargs: Any) -> UserProfile:
    defaults = dict(
        user_id="u1",
        skills=["python", "sql"],
        preferred_job_type=JobType.ANY,
        preferred_locations=[],
        requires_sponsorship=False,
        preferred_company_size=CompanySizePreference.ANY,
        preferred_industries=["tech"],
        liked_companies=[],
        disliked_companies=[],
        affinity_embedding=None,
    )
    defaults.update(kwargs)
    return UserProfile(**defaults)


def _make_job(job_id: str = "job1", **kwargs: Any) -> dict:
    """Return a fake job row dict (as if from DB with dict_row factory)."""
    defaults = dict(
        job_id=job_id,
        source_repo="Summer2026-Internships",
        company_name="ACME Corp",
        role_title="Software Engineer Intern",
        primary_url="https://example.com/job/1",
        location_raw="Remote",
        date_posted_raw="2026-03-01",
        status="active",
        first_seen_at=datetime(2026, 3, 20, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 3, 25, tzinfo=timezone.utc),
        industry="tech",
        company_size="large",
        required_skills=["python", "sql"],
        sponsorship=False,
        embedding=[0.1] * 1536,
        embedding_model="text-embedding-3-small",
    )
    defaults.update(kwargs)
    return defaults


def _build_mock_conn(jobs: list[dict]) -> MagicMock:
    """Build a mock psycopg3 connection whose cursor.fetchall() returns the given rows."""
    conn = MagicMock()

    def execute(sql: str, params: Any = None) -> MagicMock:
        cursor = MagicMock()
        if "FROM feedback" in sql:
            cursor.fetchall.return_value = []
        else:
            cursor.fetchall.return_value = jobs
        return cursor

    conn.execute.side_effect = execute
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetMatchesReturnsList:
    """get_matches returns a list."""

    def test_get_matches_returns_list(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        jobs = [_make_job("j1"), _make_job("j2"), _make_job("j3")]
        conn = _build_mock_conn(jobs)
        profile = _make_profile()

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"):
            result = get_matches(profile, conn=conn, top_n=30)

        assert isinstance(result, list)


class TestGetMatchesSortedByScoreDesc:
    """Results must be sorted descending by score."""

    def test_get_matches_sorted_by_score_desc(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        # Three jobs with varying "liked" status to influence score ordering
        job1 = _make_job("j1", company_name="LikedCo", industry="tech")
        job2 = _make_job("j2", company_name="NeutralCo", industry="tech")
        job3 = _make_job(
            "j3",
            company_name="DislikedCo",
            industry="tech",
        )
        profile = _make_profile(
            liked_companies=["likedco"],
            disliked_companies=["dislikedco"],
        )
        conn = _build_mock_conn([job1, job2, job3])

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"):
            result = get_matches(profile, conn=conn, top_n=30)

        assert len(result) >= 1
        for i in range(len(result) - 1):
            assert result[i]["score"] >= result[i + 1]["score"]


class TestGetMatchesColdStart:
    """Cold-start profile (no skills, no liked/disliked, no affinity) must not error."""

    def test_get_matches_cold_start_no_error(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        profile = _make_profile(
            skills=[],
            liked_companies=[],
            disliked_companies=[],
            affinity_embedding=None,
        )
        conn = _build_mock_conn([_make_job("j1"), _make_job("j2")])

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"):
            result = get_matches(profile, conn=conn)

        assert isinstance(result, list)


class TestGetMatchesRespectsTopN:
    """get_matches must cap results at top_n."""

    def test_get_matches_respects_top_n(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        jobs = [_make_job(f"j{i}") for i in range(10)]
        conn = _build_mock_conn(jobs)
        profile = _make_profile()

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"):
            result = get_matches(profile, conn=conn, top_n=3)

        assert len(result) <= 3


class TestGetMatchesResultShape:
    """Each result dict must have 'score' and 'signals' with 7 keys."""

    def test_get_matches_result_has_score_and_signals(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        expected_signal_keys = {
            "title_similarity",
            "skills_overlap",
            "industry_match",
            "company_size_match",
            "location_fit",
            "recency",
            "feedback_boost",
        }

        jobs = [_make_job("j1"), _make_job("j2")]
        conn = _build_mock_conn(jobs)
        profile = _make_profile()

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"):
            result = get_matches(profile, conn=conn, top_n=30)

        assert len(result) > 0
        for r in result:
            assert "score" in r, "Missing 'score' key in result dict"
            assert isinstance(r["score"], float), "'score' must be a float"
            assert "signals" in r, "Missing 'signals' key in result dict"
            assert set(r["signals"].keys()) == expected_signal_keys


class TestGetMatchesAffinityEmbedding:
    """When affinity_embedding is present, embed_text must NOT be called."""

    def test_get_matches_uses_affinity_embedding_when_present(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        affinity = [0.5] * 1536
        profile = _make_profile(affinity_embedding=affinity)
        conn = _build_mock_conn([_make_job("j1")])

        mock_embed = MagicMock(return_value=_FAKE_EMBEDDING)

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            mock_embed,
        ), patch("wekruit_matching.matching.matcher.register_vector"):
            result = get_matches(profile, conn=conn, top_n=30)

        mock_embed.assert_not_called()
        assert isinstance(result, list)


class TestGetMatchesJobFieldsPreserved:
    """All job fields from the DB row must be present in the result dict."""

    def test_get_matches_preserves_job_fields(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        job = _make_job("preserve_test", company_name="Acme", role_title="SWE Intern")
        conn = _build_mock_conn([job])
        profile = _make_profile()

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"):
            result = get_matches(profile, conn=conn, top_n=30)

        assert len(result) == 1
        row = result[0]
        assert row["job_id"] == "preserve_test"
        assert row["company_name"] == "Acme"
        assert row["role_title"] == "SWE Intern"


class TestGetMatchesANNLimit:
    """ANN candidate fetch limit must be top_n * 4."""

    def test_ann_limit_is_top_n_times_four(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        profile = _make_profile()
        conn = _build_mock_conn([])

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"):
            get_matches(profile, conn=conn, top_n=10)

        # The second positional arg in execute call is the params tuple/list
        # Params should be (embedding, limit=40)
        call_args = next(
            call
            for call in conn.execute.call_args_list
            if "ORDER BY embedding <=>" in call.args[0]
        )
        params = call_args[0][1]  # second positional arg
        # limit is the second param in the query
        assert params[1] == 40, f"Expected limit=40 (10*4), got {params[1]}"


class TestDerivedExperienceFlag:
    """get_matches reads pa-users derivedExperience only behind the flag."""

    def test_flag_off_does_not_fetch_pa_user_profile_patch(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        profile = _make_profile(skills=["python"])
        conn = _build_mock_conn([_make_job("j1", required_skills=["python"])])

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"), patch(
            "wekruit_matching.matching.matcher.get_settings",
            return_value=SimpleNamespace(matching_use_derived_experience=False),
        ), patch(
            "wekruit_matching.matching.matcher.fetch_pa_user_profile_patch",
            return_value={},
        ) as fetch_patch:
            result = get_matches(profile, conn=conn, top_n=1)

        fetch_patch.assert_not_called()
        assert "skill_depth_bonus" not in result[0]["signals"]

    def test_flag_on_fetches_pa_user_derived_experience_once_per_request(self) -> None:
        from wekruit_matching.matching.matcher import get_matches

        profile = _make_profile(user_id="pa-user-1", skills=["python", "react"])
        conn = _build_mock_conn([
            _make_job(
                "j1",
                required_skills=["python", "react"],
                seniority_level="entry_level",
            )
        ])
        patch_doc = {
            "derivedExperience": {
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
            "derivedExperienceVersion": "v1",
            "totalYearsExperience": 5,
        }

        with patch(
            "wekruit_matching.matching.matcher.embed_text",
            return_value=_FAKE_EMBEDDING,
        ), patch("wekruit_matching.matching.matcher.register_vector"), patch(
            "wekruit_matching.matching.matcher.get_settings",
            return_value=SimpleNamespace(matching_use_derived_experience=True),
        ), patch(
            "wekruit_matching.matching.matcher.fetch_pa_user_profile_patch",
            return_value=patch_doc,
        ) as fetch_patch:
            result = get_matches(
                profile,
                conn=conn,
                top_n=1,
                include_explanations=True,
            )

        fetch_patch.assert_called_once_with("pa-user-1")
        assert result[0]["signals"]["skill_depth_bonus"] == 0.7
        assert result[0]["signals"]["seniority_alignment"] == 1.0
        assert result[0]["explanation"].startswith("matched on python (5y, recent)")
