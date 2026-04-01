"""Unit tests for run_url_resolution.py — Phase 16 RESOLVE-03 orchestrator.

Tests use monkeypatching so no DB or network calls are required.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _fake_settings(serper_api_key: str = ""):
    """Return a minimal Settings-like object with serper_api_key."""
    return SimpleNamespace(serper_api_key=serper_api_key)


def _mock_conn_with_rate_row(resolved: int = 0, total: int = 1000):
    """Build a fake conn whose final execute().fetchone() returns a resolution rate row."""
    conn = MagicMock()
    rate_cursor = MagicMock()
    rate_cursor.fetchone.return_value = {"resolved": resolved, "total": total}
    # Any execute call that isn't SELECT * returns the rate cursor for fetchone
    conn.execute.return_value = rate_cursor
    return conn


# ---------------------------------------------------------------------------
# Test: run_url_resolution returns dict with expected top-level keys
# ---------------------------------------------------------------------------


def test_run_url_resolution_returns_dict(monkeypatch):
    """run_url_resolution returns a dict with 'simplify', 'slug_registry', 'total_resolved'."""
    from wekruit_matching.pipeline.run_url_resolution import run_url_resolution

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_simplify_jobs",
        lambda conn, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_via_slug_registry",
        lambda conn, registry, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.load_registry",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.get_settings",
        lambda: _fake_settings(serper_api_key=""),
    )

    fake_conn = _mock_conn_with_rate_row()

    result = run_url_resolution(conn=fake_conn)

    assert isinstance(result, dict), "Must return a dict"
    assert "simplify" in result, "'simplify' key missing"
    assert "slug_registry" in result, "'slug_registry' key missing"
    assert "total_resolved" in result, "'total_resolved' key missing"
    assert "serper" in result, "'serper' key missing"
    assert "resolution_rate" in result, "'resolution_rate' key missing"


# ---------------------------------------------------------------------------
# Test: total_resolved is the sum of resolved counts from all sub-functions
# ---------------------------------------------------------------------------


def test_total_resolved_is_sum(monkeypatch):
    """total_resolved equals resolved from simplify + resolved from slug_registry (no serper key)."""
    from wekruit_matching.pipeline.run_url_resolution import run_url_resolution

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_simplify_jobs",
        lambda conn, **kwargs: {"resolved": 5, "skipped": 1, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_via_slug_registry",
        lambda conn, registry, **kwargs: {"resolved": 3, "skipped": 2, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.load_registry",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.get_settings",
        lambda: _fake_settings(serper_api_key=""),
    )

    fake_conn = _mock_conn_with_rate_row(resolved=8, total=1000)

    result = run_url_resolution(conn=fake_conn)

    assert result["total_resolved"] == 8, f"Expected 8, got {result['total_resolved']}"
    assert result["simplify"]["resolved"] == 5
    assert result["slug_registry"]["resolved"] == 3


# ---------------------------------------------------------------------------
# Test: registry is loaded once and passed to resolve_via_slug_registry
# ---------------------------------------------------------------------------


def test_registry_loaded_once_and_passed(monkeypatch):
    """load_registry() called exactly once; returned object passed to resolve_via_slug_registry."""
    from wekruit_matching.pipeline.run_url_resolution import run_url_resolution

    registry_instance = MagicMock(name="registry")
    load_calls = []
    registry_received = []

    def _fake_load():
        load_calls.append(True)
        return registry_instance

    def _fake_slug(conn, registry, **kwargs):
        registry_received.append(registry)
        return {"resolved": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_simplify_jobs",
        lambda conn, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_via_slug_registry",
        _fake_slug,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.load_registry",
        _fake_load,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.get_settings",
        lambda: _fake_settings(serper_api_key=""),
    )

    fake_conn = _mock_conn_with_rate_row()
    run_url_resolution(conn=fake_conn)

    assert len(load_calls) == 1, f"load_registry() called {len(load_calls)} times, expected 1"
    assert registry_received[0] is registry_instance, "Wrong registry passed to resolve_via_slug_registry"


# ---------------------------------------------------------------------------
# Test: serper pass is called when serper_api_key is set
# ---------------------------------------------------------------------------


def test_serper_pass_called_when_key_set(monkeypatch):
    """resolve_via_serper is called and stats included when serper_api_key is non-empty."""
    from wekruit_matching.pipeline.run_url_resolution import run_url_resolution

    serper_calls = []

    def _fake_serper(conn, serper_api_key, **kwargs):
        serper_calls.append(serper_api_key)
        return {"resolved": 2, "skipped": 1, "errors": 0, "queries_used": 3}

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_simplify_jobs",
        lambda conn, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_via_slug_registry",
        lambda conn, registry, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_via_serper",
        _fake_serper,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.load_registry",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.get_settings",
        lambda: _fake_settings(serper_api_key="test-serper-key"),
    )

    fake_conn = _mock_conn_with_rate_row(resolved=2, total=1000)
    result = run_url_resolution(conn=fake_conn)

    assert len(serper_calls) == 1, "resolve_via_serper should be called once"
    assert serper_calls[0] == "test-serper-key"
    assert result["serper"]["resolved"] == 2
    assert result["serper"]["queries_used"] == 3
    assert result["total_resolved"] == 2


# ---------------------------------------------------------------------------
# Test: resolution_rate is computed correctly
# ---------------------------------------------------------------------------


def test_resolution_rate_computed(monkeypatch):
    """resolution_rate = resolved / total from the latest 1K jobs query."""
    from wekruit_matching.pipeline.run_url_resolution import run_url_resolution

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_simplify_jobs",
        lambda conn, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_via_slug_registry",
        lambda conn, registry, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.load_registry",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.get_settings",
        lambda: _fake_settings(serper_api_key=""),
    )

    fake_conn = _mock_conn_with_rate_row(resolved=650, total=1000)
    result = run_url_resolution(conn=fake_conn)

    assert abs(result["resolution_rate"] - 0.65) < 1e-6, (
        f"Expected resolution_rate=0.65, got {result['resolution_rate']}"
    )


def test_resolution_rate_zero_when_no_jobs(monkeypatch):
    """resolution_rate = 0.0 when total=0 (no active jobs in DB)."""
    from wekruit_matching.pipeline.run_url_resolution import run_url_resolution

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_simplify_jobs",
        lambda conn, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.resolve_via_slug_registry",
        lambda conn, registry, **kwargs: {"resolved": 0, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.load_registry",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_url_resolution.get_settings",
        lambda: _fake_settings(serper_api_key=""),
    )

    fake_conn = _mock_conn_with_rate_row(resolved=0, total=0)
    result = run_url_resolution(conn=fake_conn)

    assert result["resolution_rate"] == 0.0, (
        f"Expected resolution_rate=0.0 when total=0, got {result['resolution_rate']}"
    )
