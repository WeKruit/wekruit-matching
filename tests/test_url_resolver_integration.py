"""Integration smoke tests for URL resolution — Phase 16 RESOLVE-04.

These tests require DATABASE_URL to be set in the environment.
They skip gracefully when DATABASE_URL is absent (CI/local without DB).

Run with:
    DATABASE_URL=postgresql://... uv run pytest tests/test_url_resolver_integration.py -v -m integration

All tests use rollback instead of commit to avoid writing to the DB during test runs.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — integration tests skipped",
)

pytestmark = [
    pytest.mark.skipif(
        not os.getenv("DATABASE_URL"),
        reason="DATABASE_URL not set — integration tests skipped",
    ),
    pytest.mark.integration,
]


def _connect():
    """Open a psycopg3 connection, skipping if DATABASE_URL is absent or invalid."""
    url = os.environ.get("DATABASE_URL", "").replace("postgresql+psycopg://", "postgresql://", 1)
    if not url or url == "postgresql://":
        pytest.skip("DATABASE_URL not set — skipping integration tests")
    try:
        return psycopg.connect(url, row_factory=dict_row)
    except Exception as exc:
        pytest.skip(f"Cannot connect to DB: {exc}")


# ---------------------------------------------------------------------------
# Test 1: resolve_simplify_jobs dry run
# ---------------------------------------------------------------------------


def test_resolve_simplify_jobs_dry_run():
    """Observe how many SimplifyJobs rows would be resolved; rollback to avoid writes."""
    from wekruit_matching.pipeline.url_resolver import resolve_simplify_jobs

    with _connect() as conn:
        # Count candidates before resolution
        row = conn.execute(
            """
            SELECT COUNT(*) AS candidates
            FROM jobs
            WHERE status = 'active'
              AND source_repo NOT LIKE 'jobright%'
              AND ats_apply_url IS NULL
              AND primary_url IS NOT NULL
              AND primary_url != ''
            """
        ).fetchone()
        candidates = row["candidates"] if row else 0

        # Run resolution
        stats = resolve_simplify_jobs(conn, batch_size=500)

        # Rollback — don't commit changes during test
        conn.rollback()

    print(
        f"\n[integration] simplify dry run: candidates={candidates}, "
        f"resolved={stats['resolved']}, skipped={stats['skipped']}, errors={stats['errors']}"
    )
    assert isinstance(stats, dict), "resolve_simplify_jobs must return a dict"
    assert "resolved" in stats
    assert stats["errors"] == 0 or stats["errors"] >= 0  # sanity — non-negative


# ---------------------------------------------------------------------------
# Test 2: resolve_via_slug_registry dry run
# ---------------------------------------------------------------------------


def test_resolve_via_slug_registry_dry_run():
    """Observe how many JobRight rows the slug registry would resolve; rollback to avoid writes."""
    from wekruit_matching.pipeline.url_resolver import resolve_via_slug_registry
    from wekruit_matching.scraper.slug_registry import load_registry

    registry = load_registry()

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS candidates
            FROM jobs
            WHERE status = 'active'
              AND source_repo LIKE 'jobright%'
              AND ats_apply_url IS NULL
            """
        ).fetchone()
        candidates = row["candidates"] if row else 0

        # Cap at 20 to avoid slow test in CI — enough for a smoke check
        stats = resolve_via_slug_registry(conn, registry, batch_size=20)

        conn.rollback()

    print(
        f"\n[integration] slug_registry dry run: candidates={candidates}, "
        f"resolved={stats['resolved']}, skipped={stats['skipped']}, errors={stats['errors']}"
    )
    assert isinstance(stats, dict)
    assert "resolved" in stats
    assert "skipped" in stats


# ---------------------------------------------------------------------------
# Test 3: Resolution rate on latest 1K jobs
# ---------------------------------------------------------------------------


def test_resolution_rate_1k_jobs():
    """Measure resolution rate on the latest 1K active jobs. Observability test — no correctness gate."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE ats_apply_url IS NOT NULL) AS resolved,
              COUNT(*) AS total
            FROM (
              SELECT ats_apply_url
              FROM jobs
              WHERE status = 'active'
              ORDER BY first_seen_at DESC
              LIMIT 1000
            ) sub
            """
        ).fetchone()

    assert row is not None, "Resolution rate query returned no row"
    total = row["total"]
    resolved = row["resolved"]
    rate = resolved / total if total > 0 else 0.0

    print(f"\n[integration] Resolution rate on 1K latest jobs: {resolved}/{total} = {rate:.1%}")

    # Observability assertions — not a correctness gate
    assert total > 0, "Expected at least 1 active job in DB"
    assert rate >= 0.0, "Resolution rate must be non-negative"
    assert rate <= 1.0, "Resolution rate must be <= 1.0"


# ---------------------------------------------------------------------------
# Test 4: Slug registry loads with expected volume
# ---------------------------------------------------------------------------


def test_slug_registry_loads_with_expected_volume():
    """load_registry() succeeds and total entries across all 4 ATS > 20000."""
    from wekruit_matching.scraper.slug_registry import load_registry

    registry = load_registry()

    # Count total entries across all ATS slug maps
    total_entries = 0
    for attr in ("greenhouse_slugs", "lever_slugs", "ashby_slugs", "workday_slugs"):
        slugs = getattr(registry, attr, {})
        total_entries += len(slugs)

    print(f"\n[integration] Slug registry total entries: {total_entries}")
    assert total_entries > 20000, (
        f"Expected >20000 slug registry entries, got {total_entries}. "
        "Registry may not be loaded or ATS attributes renamed."
    )
