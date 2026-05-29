"""Unit tests for record_feedback() in feedback/handler.py.

All DB calls are mocked — no real connections required.
Tests verify:
  - feedback row insertion for all reaction types
  - liked_companies / disliked_companies array updates
  - affinity_embedding first-set and 70/30 blend logic
  - applied reaction has no profile side-effects
  - conn=None falls back to get_connection() context manager
  - no-op when job embedding is None (company still recorded)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_conn(side_effects: list | None = None) -> MagicMock:
    """Return a mock psycopg3 connection.

    conn.execute() returns a cursor MagicMock by default.
    If side_effects is provided, conn.execute side_effect is set so successive
    calls return different cursors (used for SELECT -> UPDATE sequences).
    """
    conn = MagicMock()
    if side_effects is not None:
        conn.execute.side_effect = side_effects
    else:
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn.execute.return_value = cursor
    return conn


def _cursor_returning(row: Any) -> MagicMock:
    """Return a cursor mock whose fetchone() returns `row`."""
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    return cursor


def _successful_profile_insert() -> MagicMock:
    """Cursor placeholder for lazy user_profiles creation."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLikeInsertsFeedbackRow:
    """Like reaction must issue INSERT INTO feedback."""

    def test_like_inserts_feedback_row(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        # Simulate: SELECT jobs returns a valid row; SELECT affinity returns None
        job_row = {"company_name": "ACME", "embedding": [0.1] * 1536}
        side_effects = [
            _successful_profile_insert(),      # INSERT INTO user_profiles
            _cursor_returning({"feedback_id": 1}),          # INSERT INTO feedback
            _cursor_returning(job_row),       # SELECT company_name, embedding FROM jobs
            MagicMock(),                       # UPDATE liked_companies
            _cursor_returning({"affinity_embedding": None}),  # SELECT affinity_embedding
            MagicMock(),                       # UPDATE affinity_embedding
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "like", conn=conn)

        sqls = [call[0][0] for call in conn.execute.call_args_list]
        assert any("INSERT INTO user_profiles" in sql for sql in sqls)
        assert any("INSERT INTO feedback" in sql for sql in sqls)


class TestLikeAppendsToLikedCompanies:
    """Like reaction must append company_name to liked_companies array."""

    def test_like_appends_to_liked_companies(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        job_row = {"company_name": "ACME", "embedding": [0.1] * 1536}
        side_effects = [
            _successful_profile_insert(),      # INSERT INTO user_profiles
            _cursor_returning({"feedback_id": 1}),          # INSERT INTO feedback
            _cursor_returning(job_row),       # SELECT company_name, embedding FROM jobs
            MagicMock(),                       # UPDATE liked_companies
            _cursor_returning({"affinity_embedding": None}),  # SELECT affinity_embedding
            MagicMock(),                       # UPDATE affinity_embedding
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "like", conn=conn)

        # Find the UPDATE liked_companies call
        all_calls = conn.execute.call_args_list
        liked_call = next(
            (c for c in all_calls if "UPDATE user_profiles" in c[0][0] and "liked_companies" in c[0][0]),
            None,
        )
        assert liked_call is not None, "No SQL call contained 'liked_companies'"
        # The parameter containing the company name must be "ACME"
        params = liked_call[0][1]  # second positional arg = params tuple
        assert "ACME" in params, f"Expected 'ACME' in params, got {params}"


class TestDislikeAppendsToDislikedCompanies:
    """Dislike reaction must append company_name to disliked_companies array."""

    def test_dislike_appends_to_disliked_companies(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        job_row = {"company_name": "ACME", "embedding": [0.1] * 1536}
        side_effects = [
            _successful_profile_insert(),      # INSERT INTO user_profiles
            _cursor_returning({"feedback_id": 1}),          # INSERT INTO feedback
            _cursor_returning(job_row),       # SELECT company_name, embedding FROM jobs
            MagicMock(),                       # UPDATE disliked_companies
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "dislike", conn=conn)

        all_calls = conn.execute.call_args_list
        disliked_call = next(
            (c for c in all_calls if "UPDATE user_profiles" in c[0][0] and "disliked_companies" in c[0][0]),
            None,
        )
        assert disliked_call is not None, "No SQL call contained 'disliked_companies'"
        params = disliked_call[0][1]
        assert "ACME" in params, f"Expected 'ACME' in params, got {params}"


class TestFirstLikeSetsAffinityEmbedding:
    """First like must set affinity_embedding to the exact job embedding."""

    def test_first_like_sets_affinity_embedding(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        job_embedding = [0.5] * 1536
        job_row = {"company_name": "ACME", "embedding": job_embedding}
        side_effects = [
            _successful_profile_insert(),       # INSERT INTO user_profiles
            _cursor_returning({"feedback_id": 1}),           # INSERT INTO feedback
            _cursor_returning(job_row),        # SELECT company_name, embedding FROM jobs
            MagicMock(),                        # UPDATE liked_companies
            _cursor_returning({"affinity_embedding": None}),  # SELECT affinity_embedding (None = first like)
            MagicMock(),                        # UPDATE affinity_embedding
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "like", conn=conn)

        all_calls = conn.execute.call_args_list
        affinity_call = next(
            (c for c in all_calls if "affinity_embedding" in c[0][0] and "UPDATE" in c[0][0]),
            None,
        )
        assert affinity_call is not None, "No UPDATE affinity_embedding call found"
        params = affinity_call[0][1]
        # First param should be the affinity embedding (list/array), second is user_id
        new_affinity = list(params[0])
        assert np.allclose(new_affinity, job_embedding), (
            f"Expected affinity = job embedding, got {new_affinity[:5]}..."
        )


class TestSubsequentLikeBlendsAffinity:
    """Second like must blend: normalize(0.7 * existing + 0.3 * new_signal)."""

    def test_subsequent_like_blends_affinity(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        job_embedding = [1.0] * 1536
        existing_affinity = [0.0] * 1536
        job_row = {"company_name": "B", "embedding": job_embedding}
        side_effects = [
            _successful_profile_insert(),       # INSERT INTO user_profiles
            _cursor_returning({"feedback_id": 1}),           # INSERT INTO feedback
            _cursor_returning(job_row),        # SELECT company_name, embedding FROM jobs
            MagicMock(),                        # UPDATE liked_companies
            _cursor_returning({"affinity_embedding": existing_affinity}),  # SELECT affinity
            MagicMock(),                        # UPDATE affinity_embedding
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "like", conn=conn)

        # Expected: normalize(0.7 * [0]*1536 + 0.3 * [1]*1536) = normalize([0.3]*1536)
        blended = 0.7 * np.zeros(1536) + 0.3 * np.ones(1536)
        norm = np.linalg.norm(blended)
        expected = (blended / (norm + 1e-9)).tolist()

        all_calls = conn.execute.call_args_list
        affinity_call = next(
            (c for c in all_calls if "affinity_embedding" in c[0][0] and "UPDATE" in c[0][0]),
            None,
        )
        assert affinity_call is not None, "No UPDATE affinity_embedding call found"
        params = affinity_call[0][1]
        new_affinity = list(params[0])
        assert np.allclose(new_affinity, expected, atol=1e-6), (
            f"Affinity blend mismatch. First 5 values: {new_affinity[:5]}"
        )


class TestAppliedNoProfileUpdate:
    """Applied reaction inserts feedback but must NOT update liked/disliked/affinity."""

    def test_applied_no_profile_update(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        job_row = {"company_name": "ACME", "embedding": [0.1] * 1536}
        side_effects = [
            _successful_profile_insert(),       # INSERT INTO user_profiles
            _cursor_returning({"feedback_id": 1}),           # INSERT INTO feedback
            _cursor_returning(job_row),        # SELECT company_name, embedding FROM jobs
            # No more calls — applied should stop here
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "applied", conn=conn)

        all_calls = conn.execute.call_args_list
        sqls = [c[0][0] for c in all_calls]

        assert any("INSERT INTO feedback" in s for s in sqls), "INSERT INTO feedback not called"
        assert not any("UPDATE user_profiles" in s and "liked_companies" in s for s in sqls), "liked_companies must NOT be updated"
        assert not any("UPDATE user_profiles" in s and "disliked_companies" in s for s in sqls), "disliked_companies must NOT be updated"
        assert not any("affinity_embedding" in s for s in sqls), "affinity_embedding must NOT be updated"


class TestConnNoneUsesGetConnection:
    """When conn=None, record_feedback must use get_connection() context manager."""

    def test_conn_none_uses_get_connection(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        job_row = {"company_name": "ACME", "embedding": [0.1] * 1536}
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = [
            _successful_profile_insert(),       # INSERT INTO user_profiles
            _cursor_returning({"feedback_id": 1}),           # INSERT INTO feedback
            _cursor_returning(job_row),        # SELECT company_name, embedding FROM jobs
            MagicMock(),                        # UPDATE liked_companies
            _cursor_returning({"affinity_embedding": None}),  # SELECT affinity
            MagicMock(),                        # UPDATE affinity_embedding
        ]

        with patch(
            "wekruit_matching.feedback.handler.get_connection"
        ) as mock_get_conn, patch(
            "wekruit_matching.feedback.handler.register_vector"
        ):
            mock_get_conn.return_value.__enter__ = lambda s: mock_conn
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=False)

            record_feedback("u1", "job1", "like")

        mock_get_conn.assert_called_once()
        sqls = [call[0][0] for call in mock_conn.execute.call_args_list]
        assert any("INSERT INTO user_profiles" in sql for sql in sqls)
        assert any("INSERT INTO feedback" in sql for sql in sqls)


class TestNoEmbeddingLikeSkipsAffinityUpdate:
    """Like with job embedding=None must update liked_companies but NOT affinity_embedding."""

    def test_no_embedding_like_skips_affinity_update(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        job_row = {"company_name": "C", "embedding": None}
        side_effects = [
            _successful_profile_insert(),       # INSERT INTO user_profiles
            _cursor_returning({"feedback_id": 1}),           # INSERT INTO feedback
            _cursor_returning(job_row),        # SELECT company_name, embedding FROM jobs
            MagicMock(),                        # UPDATE liked_companies
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "like", conn=conn)

        all_calls = conn.execute.call_args_list
        sqls = [c[0][0] for c in all_calls]

        assert any("liked_companies" in s for s in sqls), "liked_companies must be updated"
        assert not any(
            "affinity_embedding" in s and "UPDATE" in s for s in sqls
        ), "affinity_embedding must NOT be updated when job has no embedding"


class TestDuplicateReactionSkipsProfileUpdate:
    """A duplicate reaction (ON CONFLICT suppressed the row) must NOT re-apply
    profile effects — otherwise at-least-once replays drift affinity_embedding."""

    def test_duplicate_like_skips_profile_update(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        # RETURNING feedback_id -> fetchone() None == conflict (duplicate).
        side_effects = [
            _successful_profile_insert(),   # INSERT user_profiles
            _cursor_returning(None),        # feedback insert: conflict -> no row
            # A 3rd execute() would raise StopIteration, proving the short-circuit.
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "like", conn=conn)

        sqls = [c[0][0] for c in conn.execute.call_args_list]
        assert any("INSERT INTO feedback" in s for s in sqls), "feedback INSERT must run"
        assert conn.execute.call_count == 2, (
            f"duplicate must stop after the feedback INSERT, got "
            f"{conn.execute.call_count} execute() calls"
        )
        assert not any("SELECT company_name" in s for s in sqls), "must not fetch job"
        assert not any("UPDATE user_profiles" in s for s in sqls), "must not touch profile"
        assert not any("affinity_embedding" in s and "UPDATE" in s for s in sqls), (
            "must not re-drift affinity on a duplicate"
        )

    def test_duplicate_dislike_skips_profile_update(self) -> None:
        from wekruit_matching.feedback.handler import record_feedback

        side_effects = [
            _successful_profile_insert(),   # INSERT user_profiles
            _cursor_returning(None),        # feedback insert: conflict -> no row
        ]
        conn = _make_mock_conn(side_effects)

        with patch("wekruit_matching.feedback.handler.register_vector"):
            record_feedback("u1", "job1", "dislike", conn=conn)

        assert conn.execute.call_count == 2
        sqls = [c[0][0] for c in conn.execute.call_args_list]
        assert not any("disliked_companies" in s for s in sqls), (
            "must not append company on a duplicate dislike"
        )
