"""Unit + smoke tests for run_daily_pipeline() — Phase 17 PIPE-03 / ENRICH-01.

Unit tests (no DB, pure monkeypatch):
- Stage ordering: URL resolution is called AFTER ATS JD enrichment and BEFORE LLM enrichment
- url_resolution key is present in the return dict
- url_resolution_stats is forwarded to send_pipeline_complete_email
- Crash in run_url_resolution is isolated (pipeline continues, error captured)

DB smoke test (requires DATABASE_URL):
- Full pipeline with enrich_all + embed_all stubbed out
- Asserts return dict shape and resolution_rate bounds
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
_URL_STATS = {
    "simplify": {},
    "slug_registry": {},
    "serper": {},
    "total_resolved": 7,
    "resolution_rate": 0.42,
}
_ENRICH_STATS = {"enriched": 0, "failed": 0, "skipped": 0}
_EMBED_STATS = {"embedded": 0, "failed": 0, "skipped": 0}
_SYNC_STATS = {"active_jobs": 3, "inactive_jobs": 2, "batches": 1, "synced": 5}


@contextmanager
def _fake_get_connection():
    """Fake context-manager-based DB connection for the stale job query."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    yield conn


def _patch_all_stages(monkeypatch, *, url_resolution_override=None):
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

    url_stub = url_resolution_override or (
        lambda **kw: (call_order.append("url_resolution"), _URL_STATS)[1]
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_url_resolution",
        url_stub,
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


def test_url_resolution_called_after_ats_jd_and_before_llm(monkeypatch):
    """Stage 2b (ATS JD) < Stage 2.5 (URL resolution) < Stage 2c (LLM enrichment)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)

    run_daily_pipeline()

    assert "jd_enrichment" in call_order, "run_jd_enrichment was not called"
    assert "url_resolution" in call_order, "run_url_resolution was not called"
    assert "llm_enrich" in call_order, "enrich_all was not called"

    jd_idx = call_order.index("jd_enrichment")
    url_idx = call_order.index("url_resolution")
    llm_idx = call_order.index("llm_enrich")

    assert jd_idx < url_idx, (
        f"URL resolution (pos {url_idx}) must come AFTER ATS JD enrichment (pos {jd_idx})"
    )
    assert url_idx < llm_idx, (
        f"LLM enrichment (pos {llm_idx}) must come AFTER URL resolution (pos {url_idx})"
    )


def test_run_daily_pipeline_returns_url_resolution_key(monkeypatch):
    """Return dict must contain 'url_resolution' key with total_resolved from the stub."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)

    result = run_daily_pipeline()

    assert "url_resolution" in result, (
        "'url_resolution' key missing from run_daily_pipeline return dict"
    )
    assert result["url_resolution"]["total_resolved"] == 7
    assert result["url_resolution"]["resolution_rate"] == pytest.approx(0.42)


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


def test_url_resolution_stats_forwarded_to_email(monkeypatch):
    """send_pipeline_complete_email must receive url_resolution_stats kwarg (not None)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    captured_kwargs: dict = {}

    def _capture_email(**kw):
        captured_kwargs.update(kw)
        return True

    _patch_all_stages(monkeypatch)
    # Override the email stub with one that captures kwargs
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.send_pipeline_complete_email",
        _capture_email,
    )

    run_daily_pipeline()

    assert "url_resolution_stats" in captured_kwargs, (
        "send_pipeline_complete_email must receive url_resolution_stats kwarg"
    )
    assert captured_kwargs["url_resolution_stats"] is not None, (
        "url_resolution_stats must not be None — it should carry the stage stats dict"
    )
    assert captured_kwargs["url_resolution_stats"]["total_resolved"] == 7


def test_url_resolution_crash_is_isolated(monkeypatch):
    """RuntimeError in run_url_resolution must NOT abort the pipeline.

    The crash should be captured in result['errors'] and all other stages
    (LLM enrich, embed) must still run.
    """
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    def _exploding_url_resolution(**kw):
        raise RuntimeError("simulated URL resolution crash")

    call_order = _patch_all_stages(
        monkeypatch,
        url_resolution_override=lambda **kw: (
            call_order_ref.append("url_resolution_attempted"),
            (_ for _ in ()).throw(RuntimeError("simulated URL resolution crash")),
        )[1],
    )
    # The above lambda trick is awkward; use a simpler approach:
    call_order_ref: list[str] = []

    def _crashing_url_resolution(**kw):
        call_order_ref.append("url_resolution_attempted")
        raise RuntimeError("simulated URL resolution crash")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_url_resolution",
        _crashing_url_resolution,
    )

    result = run_daily_pipeline()

    # Pipeline must return a result (not re-raise)
    assert isinstance(result, dict), "Pipeline must return dict even after URL resolution crash"

    # Error must be captured
    assert len(result["errors"]) >= 1, "Crash must appear in errors list"
    assert any("URL resolution" in e for e in result["errors"]), (
        f"Expected 'URL resolution' in errors, got: {result['errors']}"
    )

    # Other stages must still have run (LLM enrich records to call_order from _patch_all_stages)
    assert "llm_enrich" in call_order, "LLM enrichment must still run after URL resolution crash"
    assert result["enrich"] == _ENRICH_STATS
    assert result["embed"] == _EMBED_STATS


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
    Lets scrape, JobRight parse, ATS JD parse, and URL resolution run
    against real data so we can measure the live resolution_rate.
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

    # All 6 expected keys must be present
    expected_keys = {
        "scrape",
        "jd_enrichment",
        "url_resolution",
        "enrich",
        "embed",
        "sync",
        "errors",
        "duration_seconds",
    }
    assert expected_keys.issubset(result.keys()), (
        f"Missing keys: {expected_keys - result.keys()}"
    )

    # url_resolution sub-dict must have resolution_rate as a float in [0, 1]
    url_stats = result["url_resolution"]
    assert isinstance(url_stats.get("resolution_rate"), float), (
        f"resolution_rate must be a float, got: {url_stats.get('resolution_rate')!r}"
    )
    assert 0.0 <= url_stats["resolution_rate"] <= 1.0, (
        f"resolution_rate out of bounds: {url_stats['resolution_rate']}"
    )

    # Pipeline must complete without crashing (errors list should be empty on a healthy DB)
    assert len(result["errors"]) == 0, (
        f"Smoke test pipeline errors: {result['errors']}"
    )
