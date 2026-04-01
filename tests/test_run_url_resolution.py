"""Unit tests for run_url_resolution.py — Phase 16 RESOLVE-03 orchestrator.

Tests use monkeypatching so no DB or network calls are required.
"""
from __future__ import annotations

from unittest.mock import MagicMock


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

    fake_conn = MagicMock()

    result = run_url_resolution(conn=fake_conn)

    assert isinstance(result, dict), "Must return a dict"
    assert "simplify" in result, "'simplify' key missing"
    assert "slug_registry" in result, "'slug_registry' key missing"
    assert "total_resolved" in result, "'total_resolved' key missing"


# ---------------------------------------------------------------------------
# Test: total_resolved is the sum of resolved counts from both sub-functions
# ---------------------------------------------------------------------------


def test_total_resolved_is_sum(monkeypatch):
    """total_resolved equals resolved from simplify + resolved from slug_registry."""
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

    fake_conn = MagicMock()

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

    fake_conn = MagicMock()
    run_url_resolution(conn=fake_conn)

    assert len(load_calls) == 1, f"load_registry() called {len(load_calls)} times, expected 1"
    assert registry_received[0] is registry_instance, "Wrong registry passed to resolve_via_slug_registry"
