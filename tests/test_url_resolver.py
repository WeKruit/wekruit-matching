"""Unit tests for url_resolver.py — Phase 16 RESOLVE-02.

Tests use mock conn objects (MagicMock / SimpleNamespace) so no DB is required.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Test: importing url_resolver has zero side effects (no I/O, no DB open)
# ---------------------------------------------------------------------------


def test_import_has_no_side_effects(monkeypatch):
    """Importing url_resolver must not open a DB connection or load files."""
    opened_connections = []

    def fake_get_connection(*args, **kwargs):
        opened_connections.append(True)
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield MagicMock()

        return _cm()

    monkeypatch.setattr(
        "wekruit_matching.db.connection.get_pool",
        lambda: (_ for _ in ()).throw(AssertionError("get_pool called on import")),
        raising=False,
    )

    # Re-import should not raise
    import importlib

    import wekruit_matching.pipeline.url_resolver as resolver

    importlib.reload(resolver)
    assert len(opened_connections) == 0, "DB connection opened on import"


# ---------------------------------------------------------------------------
# Test: ResolveResult dataclass fields
# ---------------------------------------------------------------------------


def test_resolve_result_fields():
    """ResolveResult has expected fields and is frozen."""
    from wekruit_matching.pipeline.url_resolver import ResolveResult

    result = ResolveResult(
        job_id="abc123",
        company_name="Stripe",
        role_title="SWE Intern",
        resolved_url="https://boards.greenhouse.io/stripe/jobs/123",
        source="simplify_copy",
        tier="greenhouse",
    )
    assert result.job_id == "abc123"
    assert result.company_name == "Stripe"
    assert result.role_title == "SWE Intern"
    assert result.resolved_url == "https://boards.greenhouse.io/stripe/jobs/123"
    assert result.source == "simplify_copy"
    assert result.tier == "greenhouse"

    # Must be frozen (immutable)
    import dataclasses

    assert dataclasses.is_dataclass(result)
    try:
        result.job_id = "mutated"
        raise AssertionError("ResolveResult should be frozen")
    except (AttributeError, dataclasses.FrozenInstanceError):
        pass  # expected


def test_resolve_result_nullable_fields():
    """ResolveResult allows None for resolved_url, source, tier."""
    from wekruit_matching.pipeline.url_resolver import ResolveResult

    result = ResolveResult(
        job_id="xyz",
        company_name="Acme",
        role_title="PM",
        resolved_url=None,
        source=None,
        tier=None,
    )
    assert result.resolved_url is None
    assert result.source is None
    assert result.tier is None


# ---------------------------------------------------------------------------
# Test: resolve_simplify_jobs — 3 rows: 1 greenhouse, 1 firecrawl, 1 lever
# ---------------------------------------------------------------------------


def _make_mock_conn(rows: list[dict]):
    """Build a mock psycopg3-style conn that returns `rows` on first execute.

    Second+ calls (UPDATE) return a mock cursor with no rows.
    Supports conn.commit().
    """
    conn = MagicMock()
    # fetchall() returns the job rows on the SELECT, then empty list afterward
    select_cursor = MagicMock()
    select_cursor.fetchall.return_value = rows
    # After first batch, return empty to stop loop
    empty_cursor = MagicMock()
    empty_cursor.fetchall.return_value = []

    update_cursor = MagicMock()

    def _execute_side_effect(query, params=None):
        query_stripped = query.strip().upper()
        if query_stripped.startswith("SELECT"):
            # First call returns rows, subsequent calls return empty
            if not hasattr(_execute_side_effect, "_called"):
                _execute_side_effect._called = True
                return select_cursor
            return empty_cursor
        return update_cursor

    conn.execute.side_effect = _execute_side_effect
    conn.commit = MagicMock()
    return conn


def test_resolve_simplify_jobs_mixed_routes():
    """resolve_simplify_jobs: greenhouse + lever resolved, firecrawl skipped.

    3 rows:
    - Stripe: boards.greenhouse.io → GREENHOUSE → resolved
    - FAANG: simplify.jobs/... → FIRECRAWL → skipped
    - Palantir: jobs.lever.co/... → LEVER → resolved
    """
    from wekruit_matching.pipeline.url_resolver import resolve_simplify_jobs

    rows = [
        {
            "job_id": "job-1",
            "company_name": "Stripe",
            "role_title": "SWE Intern",
            "primary_url": "https://boards.greenhouse.io/stripe/jobs/123456",
        },
        {
            "job_id": "job-2",
            "company_name": "FAANG",
            "role_title": "DS Intern",
            "primary_url": "https://simplify.jobs/p/abc-data-science",
        },
        {
            "job_id": "job-3",
            "company_name": "Palantir",
            "role_title": "SWE NewGrad",
            "primary_url": "https://jobs.lever.co/palantir/abc-123",
        },
    ]

    conn = _make_mock_conn(rows)
    stats = resolve_simplify_jobs(conn)

    assert stats["resolved"] == 2, f"Expected 2 resolved, got {stats}"
    assert stats["skipped"] == 1, f"Expected 1 skipped, got {stats}"
    assert stats["errors"] == 0, f"Expected 0 errors, got {stats}"
    # Should commit once after processing the batch
    conn.commit.assert_called_once()


def test_resolve_simplify_jobs_updates_correct_column():
    """resolve_simplify_jobs writes to ats_apply_url for resolved rows."""
    from wekruit_matching.pipeline.url_resolver import resolve_simplify_jobs

    greenhouse_url = "https://boards.greenhouse.io/stripe/jobs/999"
    rows = [
        {
            "job_id": "job-greenhouse",
            "company_name": "Stripe",
            "role_title": "Intern",
            "primary_url": greenhouse_url,
        }
    ]

    conn = _make_mock_conn(rows)
    resolve_simplify_jobs(conn)

    # At least one UPDATE call must reference ats_apply_url
    all_calls = conn.execute.call_args_list
    update_calls = [c for c in all_calls if "UPDATE" in str(c).upper()]
    assert update_calls, "No UPDATE statement executed"

    # The update must have been called with the greenhouse URL and job_id
    found = False
    for c in update_calls:
        args = c.args
        kwargs = c.kwargs
        params = args[1] if len(args) > 1 else kwargs.get("params", {})
        if isinstance(params, dict):
            if params.get("url") == greenhouse_url or params.get("ats_apply_url") == greenhouse_url:
                found = True
    assert found, f"ats_apply_url not updated with {greenhouse_url}. Calls: {all_calls}"


# ---------------------------------------------------------------------------
# Test: resolve_simplify_jobs — empty result set
# ---------------------------------------------------------------------------


def test_resolve_simplify_jobs_empty():
    """resolve_simplify_jobs with empty result set returns immediately."""
    from wekruit_matching.pipeline.url_resolver import resolve_simplify_jobs

    conn = MagicMock()
    empty_cursor = MagicMock()
    empty_cursor.fetchall.return_value = []
    conn.execute.return_value = empty_cursor

    stats = resolve_simplify_jobs(conn)

    assert stats["resolved"] == 0
    assert stats["skipped"] == 0
    assert stats["errors"] == 0
    # commit not called (nothing processed)
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test: resolve_via_slug_registry stub
# ---------------------------------------------------------------------------


def test_resolve_via_slug_registry_returns_stats_dict():
    """resolve_via_slug_registry returns a dict with resolved/skipped/errors keys."""
    from wekruit_matching.pipeline.url_resolver import resolve_via_slug_registry

    conn = MagicMock()
    empty_cursor = MagicMock()
    empty_cursor.fetchall.return_value = []
    conn.execute.return_value = empty_cursor

    registry = MagicMock()
    registry.lookup_all_ats.return_value = {}  # No matches — avoids network calls

    result = resolve_via_slug_registry(conn, registry)

    assert isinstance(result, dict), "Must return a dict"
    assert "resolved" in result, "'resolved' key missing from result"
    assert "skipped" in result, "'skipped' key missing from result"
    assert "errors" in result, "'errors' key missing from result"


# ---------------------------------------------------------------------------
# Task 1 tests: _title_match helper
# ---------------------------------------------------------------------------


def test_title_match_intern_suffix():
    """Exact prefix match — intern title matches posting with summer suffix."""
    from wekruit_matching.pipeline.url_resolver import _title_match

    assert _title_match("Software Engineer Intern", "Software Engineer Intern - Summer 2026") is True


def test_title_match_senior_superset():
    """Senior variation — 'Data Scientist' tokens all present in 'Senior Data Scientist'."""
    from wekruit_matching.pipeline.url_resolver import _title_match

    assert _title_match("Data Scientist", "Senior Data Scientist") is True


def test_title_match_no_overlap():
    """No token overlap — must return False."""
    from wekruit_matching.pipeline.url_resolver import _title_match

    assert _title_match("Marketing Manager", "Software Engineer") is False


# ---------------------------------------------------------------------------
# Task 1 tests: resolve_via_slug_registry — success path
# ---------------------------------------------------------------------------


def _make_jobright_conn(rows: list[dict]):
    """Build a mock conn that returns jobright rows on SELECT then empty."""
    conn = MagicMock()
    select_cursor = MagicMock()
    select_cursor.fetchall.return_value = rows
    empty_cursor = MagicMock()
    empty_cursor.fetchall.return_value = []
    update_cursor = MagicMock()

    call_count = {"n": 0}

    def _execute_side_effect(query, params=None):
        q = query.strip().upper()
        if q.startswith("SELECT"):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return select_cursor
            return empty_cursor
        return update_cursor

    conn.execute.side_effect = _execute_side_effect
    conn.commit = MagicMock()
    return conn


def test_resolve_via_slug_registry_match_writes_ats_apply_url(monkeypatch):
    """Registry matches Stripe → Greenhouse, title matches → ats_apply_url written."""
    from unittest.mock import MagicMock, patch

    from wekruit_matching.pipeline.url_resolver import resolve_via_slug_registry
    from wekruit_matching.scraper.url_classifier import ATSTier

    rows = [
        {
            "job_id": "jr-001",
            "company_name": "Stripe",
            "role_title": "Software Engineer Intern",
            "primary_url": "https://jobright.ai/jobs/info/abc123",
        }
    ]
    conn = _make_jobright_conn(rows)

    registry = MagicMock()
    registry.lookup_all_ats.return_value = {ATSTier.GREENHOUSE: "stripe"}

    greenhouse_listings = [
        ("Software Engineer Intern", "https://boards.greenhouse.io/stripe/jobs/999")
    ]

    with patch(
        "wekruit_matching.pipeline.url_resolver._fetch_ats_listings",
        return_value=greenhouse_listings,
    ):
        stats = resolve_via_slug_registry(conn, registry)

    assert stats["resolved"] == 1, f"Expected 1 resolved, got {stats}"
    assert stats["skipped"] == 0, f"Expected 0 skipped, got {stats}"

    # ats_apply_url must have been written
    all_calls = conn.execute.call_args_list
    update_calls = [c for c in all_calls if "UPDATE" in str(c).upper()]
    assert update_calls, "No UPDATE executed for resolved job"
    found = any(
        (args[1] if len(args) > 1 else c.kwargs.get("params", {})).get("url")
        == "https://boards.greenhouse.io/stripe/jobs/999"
        for c in update_calls
        for args in [c.args]
    )
    assert found, f"Expected ats_apply_url to be written. Calls: {all_calls}"


def test_resolve_via_slug_registry_no_registry_match():
    """No registry match → job is skipped, nothing written."""
    from wekruit_matching.pipeline.url_resolver import resolve_via_slug_registry

    rows = [
        {
            "job_id": "jr-002",
            "company_name": "UnknownCo",
            "role_title": "PM",
            "primary_url": "https://jobright.ai/jobs/info/xyz",
        }
    ]
    conn = _make_jobright_conn(rows)

    registry = MagicMock()
    registry.lookup_all_ats.return_value = {}  # No match

    stats = resolve_via_slug_registry(conn, registry)

    assert stats["skipped"] == 1, f"Expected 1 skipped, got {stats}"
    assert stats["resolved"] == 0, f"Expected 0 resolved, got {stats}"

    # No UPDATE should be issued
    all_calls = conn.execute.call_args_list
    update_calls = [c for c in all_calls if "UPDATE" in str(c).upper()]
    assert not update_calls, f"Unexpected UPDATE for skipped job: {update_calls}"
