"""Unit + smoke tests for run_daily_pipeline() — Phase 17 PIPE-03 / ENRICH-01.

Phase 66 (2026-05-06): Stage 2.5 (URL resolution) removed — migrated to
wekruit-pa Cloud Function `paBackfillAtsUrlsBatch`. Tests for url_resolution
ordering / forwarding / crash-isolation deleted accordingly.

Unit tests (no DB, pure monkeypatch):
- Stage ordering: ATS JD < LLM enrichment < embed < job_sync
- Job sync crash isolated (pipeline continues, error captured)

DB smoke test (requires DATABASE_URL):
- Full pipeline with enrich_all + embed_all stubbed out
- Asserts return dict shape
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Skip marker for DB-gated tests
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
skip_no_db = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")

# ---------------------------------------------------------------------------
# Stub factories
# ---------------------------------------------------------------------------

_SCRAPE_STATS = {"simplify": {"inserted": 0, "stale": 0, "unchanged": 0}}
_JOBRIGHT_STATS = {"enriched": 0, "failed": 0, "skills_found": 0}
_JD_STATS = {"processed": 0, "failed": 0, "skipped": 0, "credits_used": 0}
_ENRICH_STATS = {"enriched": 0, "failed": 0, "skipped": 0}
_EMBED_STATS = {"embedded": 0, "failed": 0, "skipped": 0}
_SYNC_STATS = {"active_jobs": 3, "inactive_jobs": 2, "batches": 1, "synced": 5}


@contextmanager
def _fake_get_connection():
    """Fake context-manager-based DB connection for the stale job query."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    yield conn


def _patch_all_stages(monkeypatch):
    """Monkeypatch every pipeline sub-function with harmless stubs.

    Returns a list that each stub appends its stage name to, so callers
    can assert on call order.
    """
    call_order: list[str] = []

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.scrape_all",
        lambda: _SCRAPE_STATS,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.enrich_jobright",
        lambda conn, **kw: _JOBRIGHT_STATS,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_jd_enrichment",
        lambda **kw: (call_order.append("jd_enrichment"), _JD_STATS)[1],
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.enrich_all",
        lambda: (call_order.append("llm_enrich"), _ENRICH_STATS)[1],
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.embed_all",
        lambda: (call_order.append("embed"), _EMBED_STATS)[1],
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.sync_jobs_to_firebase",
        lambda **kw: (call_order.append("job_sync"), _SYNC_STATS)[1],
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.send_pipeline_start_email",
        lambda: True,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.send_pipeline_complete_email",
        lambda **kw: True,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.get_connection",
        _fake_get_connection,
    )

    return call_order


# ---------------------------------------------------------------------------
# Unit tests (no DB)
# ---------------------------------------------------------------------------


def test_stage_ordering_jd_before_llm_before_embed_before_sync(monkeypatch):
    """Stage 2b (ATS JD) < Stage 2c (LLM enrichment) < Stage 3 (embed) < Stage 4 (sync)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)

    run_daily_pipeline()

    assert "jd_enrichment" in call_order, "run_jd_enrichment was not called"
    assert "llm_enrich" in call_order, "enrich_all was not called"
    assert "embed" in call_order, "embed_all was not called"
    assert "job_sync" in call_order, "sync_jobs_to_firebase was not called"

    jd_idx = call_order.index("jd_enrichment")
    llm_idx = call_order.index("llm_enrich")
    embed_idx = call_order.index("embed")
    sync_idx = call_order.index("job_sync")

    assert jd_idx < llm_idx, (
        f"LLM enrichment (pos {llm_idx}) must come AFTER ATS JD enrichment (pos {jd_idx})"
    )
    assert llm_idx < embed_idx, (
        f"embed (pos {embed_idx}) must come AFTER LLM enrichment (pos {llm_idx})"
    )
    assert embed_idx < sync_idx, (
        f"job sync (pos {sync_idx}) must come AFTER embed (pos {embed_idx})"
    )


def test_run_daily_pipeline_returns_expected_keys(monkeypatch):
    """Return dict must contain core stage keys (no url_resolution after Phase 66)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)

    result = run_daily_pipeline()

    expected_keys = {
        "scrape",
        "jd_enrichment",
        "enrich",
        "embed",
        "sync",
        "errors",
        "duration_seconds",
    }
    assert expected_keys.issubset(result.keys()), (
        f"Missing keys: {expected_keys - result.keys()}"
    )
    # url_resolution removed in Phase 66
    assert "url_resolution" not in result, (
        "Phase 66: url_resolution key should have been removed from return dict"
    )


def test_job_sync_called_after_embed_and_returned(monkeypatch):
    """Daily pipeline must append Firebase sync after embedding."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)

    result = run_daily_pipeline()

    assert "embed" in call_order, "embed_all was not called"
    assert "job_sync" in call_order, "sync_jobs_to_firebase was not called"
    assert call_order.index("embed") < call_order.index("job_sync"), (
        "job sync must run after embedding completes"
    )
    assert result["sync"] == _SYNC_STATS


def test_job_sync_crash_is_isolated(monkeypatch):
    """Sync crash must not abort the pipeline after embed."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)

    def _crashing_job_sync(**kw):
        raise RuntimeError("simulated job sync crash")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.sync_jobs_to_firebase",
        _crashing_job_sync,
    )

    result = run_daily_pipeline()

    assert isinstance(result, dict)
    assert any("job sync" in error.lower() for error in result["errors"]), result["errors"]
    assert result["embed"] == _EMBED_STATS


# ---------------------------------------------------------------------------
# Smoke test (requires DATABASE_URL)
# ---------------------------------------------------------------------------


@skip_no_db
def test_pipeline_smoke_1k_jobs(monkeypatch):
    """Run the full pipeline against the live DB with costly API stages stubbed.

    Stubs only enrich_all and embed_all (LLM + embedding API calls).
    Lets scrape, JobRight parse, and ATS JD parse run against real data.
    """
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.enrich_all",
        lambda: {"enriched": 0, "failed": 0, "skipped": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.embed_all",
        lambda: {"embedded": 0, "failed": 0, "skipped": 0},
    )

    result = run_daily_pipeline()

    # Core expected keys must be present (url_resolution removed in Phase 66)
    expected_keys = {
        "scrape",
        "jd_enrichment",
        "enrich",
        "embed",
        "sync",
        "errors",
        "duration_seconds",
    }
    assert expected_keys.issubset(result.keys()), (
        f"Missing keys: {expected_keys - result.keys()}"
    )

    # Pipeline must complete without crashing (errors list should be empty on a healthy DB)
    assert len(result["errors"]) == 0, (
        f"Smoke test pipeline errors: {result['errors']}"
    )
