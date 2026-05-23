"""Unit tests for Stage 2a (JobRight enrichment) integration in run_daily_pipeline."""
from __future__ import annotations

from contextlib import contextmanager


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------

@contextmanager
def _fake_conn_ctx():
    class _FakeConn:
        def execute(self, *a, **kw):
            class _R:
                def fetchall(self):
                    return []
            return _R()

        def commit(self):
            pass

    yield _FakeConn()


def _make_stubs(monkeypatch, *, enrich_jobright_side_effect=None, enrich_jobright_return=None):
    """Patch all external I/O used by run_daily_pipeline.

    - enrich_jobright_side_effect: if set, calling enrich_jobright raises this exception.
    - enrich_jobright_return: dict returned by enrich_jobright when it succeeds.
    """
    monkeypatch.setattr("wekruit_matching.pipeline.daily.scrape_all", lambda: {})
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.firestore_dead_backfill",
        lambda conn: {"synced": 0, "total_seen": 0, "skipped": "test"},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.sync_jobs_to_firebase",
        lambda *, since, full_sync: {"active_jobs": 0, "inactive_jobs": 0, "synced": 0, "batches": 0},
    )
    for env_key in (
        "ENABLE_WELLFOUND_SCRAPE",
        "ENABLE_LINKEDIN_SCRAPE",
        "ENABLE_OTTA_SCRAPE",
        "ENABLE_GREENHOUSE_DIRECT",
        "ENABLE_LEVER_DIRECT",
        "ENABLE_ASHBY_DIRECT",
    ):
        monkeypatch.setenv(env_key, "0")
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_jd_enrichment",
        lambda conn: {"processed": 0, "failed": 0, "skipped": 0, "credits_used": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.enrich_all",
        lambda: {"enriched": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.embed_all",
        lambda: {"embedded": 0},
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.send_pipeline_start_email",
        lambda: None,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.send_pipeline_complete_email",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.get_connection",
        _fake_conn_ctx,
    )

    if enrich_jobright_side_effect is not None:
        def _raising_enrich(conn, *, max_workers=8, batch_size=50):
            raise enrich_jobright_side_effect

        monkeypatch.setattr(
            "wekruit_matching.pipeline.daily.enrich_jobright",
            _raising_enrich,
        )
    else:
        ret = enrich_jobright_return or {"enriched": 0, "failed": 0, "skills_found": 0}

        monkeypatch.setattr(
            "wekruit_matching.pipeline.daily.enrich_jobright",
            lambda conn, *, max_workers=8, batch_size=50: ret,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_jobright_enrichment_stats_returned(monkeypatch) -> None:
    """Stage 2a runs successfully; pipeline completes without error.

    JobRight stats are logged internally but not present in the result dict.
    The scrape key comes from scrape_all (stubbed to {}) and errors should be empty.
    """
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _make_stubs(
        monkeypatch,
        enrich_jobright_return={"enriched": 5, "failed": 1, "skills_found": 12},
    )

    result = run_daily_pipeline()

    # scrape key maps to scrape_all return value
    assert result["scrape"] == {}

    # no errors — all stages succeeded
    assert "errors" in result
    assert result["errors"] == []

    # jd_enrichment comes from run_jd_enrichment (Stage 2b), not JobRight
    assert result["jd_enrichment"] == {
        "processed": 0,
        "failed": 0,
        "skipped": 0,
        "credits_used": 0,
    }


def test_jobright_crash_does_not_abort_pipeline(monkeypatch) -> None:
    """A RuntimeError in Stage 2a (JobRight) must not abort the pipeline.

    The error should be captured in result["errors"] and the pipeline must
    return normally rather than raise.
    """
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _make_stubs(
        monkeypatch,
        enrich_jobright_side_effect=RuntimeError("jobright down"),
    )

    # Must NOT raise
    result = run_daily_pipeline()

    # Pipeline completes and returns a result dict
    assert "errors" in result

    # The JobRight crash should be recorded in errors
    assert any(
        "jobright" in e.lower() or "JobRight" in e
        for e in result["errors"]
    ), f"Expected a JobRight error in errors list, got: {result['errors']}"
