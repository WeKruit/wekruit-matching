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

# The 1k smoke runs REAL network stages (scrape/JobRight/ATS) against the DB, so
# it hangs / fails in CI (no API keys, ephemeral empty DB, rate limits). It is
# opt-in only — set WEKRUIT_RUN_LIVE_SMOKE=1 to run it against a prod-data DB.
# The offline-mocked replacement (seeded throwaway DB, recorded fixtures, hard
# timeout) is the tracked reliability follow-up (DoD #6 / TCG-5).
skip_unless_live_smoke = pytest.mark.skipif(
    os.getenv("WEKRUIT_RUN_LIVE_SMOKE") != "1",
    reason="live smoke (real network) — set WEKRUIT_RUN_LIVE_SMOKE=1 to run",
)

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

    for env_key in (
        "ENABLE_WELLFOUND_SCRAPE",
        "ENABLE_LINKEDIN_SCRAPE",
        "ENABLE_OTTA_SCRAPE",
        "ENABLE_GREENHOUSE_DIRECT",
        "ENABLE_LEVER_DIRECT",
        "ENABLE_ASHBY_DIRECT",
    ):
        monkeypatch.setenv(env_key, "0")
    # skipped="" mirrors the REAL success shape (firestore_dead_backfill returns
    # skipped="" on success, "no_sdk"/"no_creds" only when it actually skips). A
    # truthy skipped now correctly degrades the stage, so the healthy-path stub
    # must use the real success value.
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.firestore_dead_backfill",
        lambda conn: {"synced": 0, "total_seen": 0, "skipped": ""},
    )
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
    # Stage 2.5 (ATS resolve) — stub the imported resolver so unit tests never
    # hit Serper/DB. The stage is gated on the SERPER_API_KEY env var (mirrors
    # Stage 1.7's FIRECRAWL_BASE_URL gate), so set it here; tests that exercise
    # the skip path delete it. Records call order.
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key")
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.resolve_jobright_pending",
        lambda **kw: (
            call_order.append("ats_resolve"),
            {"resolved": 0, "missed": 0, "skipped": 0, "errors": 0},
        )[1],
    )
    # Stage 4.5 (Firestore reconcile) — stub healthy/ok by default so DB-free
    # unit tests don't touch Firestore. Tests that exercise it override this.
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.firestore_reconcile",
        lambda conn, **kw: {
            "ok": True,
            "skipped": False,
            "divergence": 0.0,
            "pg_matchable": 5,
            "fs_active": 5,
            "fs_total": 7,
            "reason": "",
        },
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
    # P-REL: the post-run reliability gate is now a stage of the pipeline.
    # Stub it healthy by default so these DB-free unit tests don't touch the
    # live DB; tests that exercise the gate override this.
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_health_gate",
        lambda **kw: {"ok": True, "metrics": {}, "failures": []},
    )
    # Gate-4 (2026-06-02): the BLOCKING pre-sync data-quality gate is now a stage
    # (3.6) that runs assert_pre_sync_ready(conn) against the live DB before sync.
    # Stub it pass-by-default so these DB-free unit tests don't trip it on the
    # MagicMock conn (compute_metrics over a mock yields non-zero "violations" and
    # would wrongly skip sync -> flip status to partial). Tests that exercise the
    # gate's blocking behaviour use tests/test_pre_sync_gate.py (real test DB).
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.assert_pre_sync_ready",
        lambda conn: None,
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


@skip_unless_live_smoke
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


# ---------------------------------------------------------------------------
# P7-B: Per-stage timeout + always-fire finalizer tests
# ---------------------------------------------------------------------------

def test_stage_timeout_isolated_to_one_stage(monkeypatch):
    """Stage 2c LLM enrich timeout must NOT prevent embed/sync/email.

    Simulates the real-world failure mode that cost two days of pipeline
    output: the wrapper SIGALRM killed python mid-Stage-2c, never reaching
    Stage 3 (embed) or Stage 4 (sync) or the completion email.

    Test: shrink the LLM stage budget to 1s and make ``enrich_all`` sleep
    for 3s. Assert:
      * ``errors`` contains a TIMEOUT entry for ``llm_enrich``
      * embed_all + sync_jobs_to_firebase + send_pipeline_complete_email
        WERE called after the timeout
      * pipeline_status == 'partial' (some stages ok, errors present)
    """
    import time as _time
    from wekruit_matching.pipeline import daily as daily_mod
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)

    # Track whether the completion email fires — the critical regression test
    email_fired = {"called": False}

    def _track_email(**kw):
        email_fired["called"] = True
        return True

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.send_pipeline_complete_email",
        _track_email,
    )

    # Shrink the LLM budget to 1s; sleep 3s in enrich_all to force timeout
    monkeypatch.setitem(daily_mod.STAGE_BUDGETS, "llm_enrich", 1)

    def _slow_enrich():
        _time.sleep(3)
        return {"enriched": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr("wekruit_matching.pipeline.daily.enrich_all", _slow_enrich)

    result = run_daily_pipeline()

    # 1. Timeout was recorded
    timeout_entries = [e for e in result["errors"] if "TIMEOUT" in e and "llm_enrich" in e]
    assert timeout_entries, (
        f"Expected llm_enrich TIMEOUT in errors, got: {result['errors']}"
    )

    # 2. Downstream stages still ran (the whole point of P7-B)
    assert "embed" in call_order, (
        "embed_all must run after llm_enrich timeout — got: " + repr(call_order)
    )
    assert "job_sync" in call_order, (
        "sync_jobs_to_firebase must run after llm_enrich timeout — got: "
        + repr(call_order)
    )

    # 3. Completion email fired (always-fire finalizer)
    assert email_fired["called"], (
        "send_pipeline_complete_email MUST fire even on stage timeout"
    )

    # 4. pipeline_status == 'partial' (some stages ok + errors present)
    assert result["pipeline_status"] == "partial", (
        f"Expected partial, got {result['pipeline_status']} "
        f"(stage_outcomes={result['stage_outcomes']})"
    )

    # 5. stage_outcomes records the timeout
    assert result["stage_outcomes"].get("llm_enrich") == "timeout", (
        f"Expected stage_outcomes['llm_enrich']='timeout', "
        f"got {result['stage_outcomes']}"
    )


def test_finalizer_email_fires_on_full_pipeline_crash(monkeypatch):
    """If Stage 1 itself crashes hard (non-timeout), email + tokens still fire.

    Tests the try/finally outer block — even if a stage raises something
    weirder than a normal Exception (e.g. KeyboardInterrupt should NOT
    swallow, but a regular crash chain shouldn't either).
    """
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)
    # Disable real-network senior scrapers (Stage 1.5+1.6) for unit-test
    # isolation — we only want to test the new try/finally + status logic.
    for var in (
        "ENABLE_WELLFOUND_SCRAPE", "ENABLE_LINKEDIN_SCRAPE", "ENABLE_OTTA_SCRAPE",
        "ENABLE_GREENHOUSE_DIRECT", "ENABLE_LEVER_DIRECT", "ENABLE_ASHBY_DIRECT",
    ):
        monkeypatch.setenv(var, "0")
    email_fired = {"called": False}

    def _track_email(**kw):
        email_fired["called"] = True
        return True

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.send_pipeline_complete_email",
        _track_email,
    )

    # Make every "real work" stage crash
    def _crash():
        raise RuntimeError("simulated crash")

    monkeypatch.setattr("wekruit_matching.pipeline.daily.scrape_all", _crash)
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_jd_enrichment",
        lambda **kw: _crash(),
    )
    monkeypatch.setattr("wekruit_matching.pipeline.daily.enrich_all", _crash)
    monkeypatch.setattr("wekruit_matching.pipeline.daily.embed_all", _crash)
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.sync_jobs_to_firebase",
        lambda **kw: _crash(),
    )

    result = run_daily_pipeline()

    assert email_fired["called"], "Email MUST fire even when every stage crashed"
    assert result["pipeline_status"] == "failed", (
        f"All stages crashed -> expected 'failed', got {result['pipeline_status']}"
    )
    assert len(result["errors"]) > 0


def test_pipeline_status_success_when_all_ok(monkeypatch):
    """Healthy pipeline -> status='success', no errors."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    # Disable real-network senior scrapers (Stage 1.5+1.6) for unit-test
    # isolation — we only want to test the new try/finally + status logic.
    for var in (
        "ENABLE_WELLFOUND_SCRAPE", "ENABLE_LINKEDIN_SCRAPE", "ENABLE_OTTA_SCRAPE",
        "ENABLE_GREENHOUSE_DIRECT", "ENABLE_LEVER_DIRECT", "ENABLE_ASHBY_DIRECT",
    ):
        monkeypatch.setenv(var, "0")
    result = run_daily_pipeline()

    assert result["pipeline_status"] == "success", (
        f"All stages ok -> expected 'success', got {result['pipeline_status']}"
    )
    assert result["errors"] == [], (
        f"Healthy pipeline should have no errors, got {result['errors']}"
    )


def test_stdout_emits_pipeline_status_token(monkeypatch, capsys):
    """The wrapper greps stdout for ``pipelineStatus=...`` — must always be emitted."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    run_daily_pipeline()

    captured = capsys.readouterr()
    assert "pipelineStatus=" in captured.out, (
        f"Wrapper depends on stdout 'pipelineStatus=' token; not found in: "
        f"{captured.out!r}"
    )


# ---------------------------------------------------------------------------
# Fix #2: Stage 2.5 ATS resolve
# ---------------------------------------------------------------------------


def test_ats_resolve_runs_before_embed_and_sync(monkeypatch):
    """Stage 2.5 ATS resolve must run AFTER llm enrich and BEFORE embed/sync
    (ordering is load-bearing for fix #4: resolve content_hash before embed)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)
    result = run_daily_pipeline()

    assert "ats_resolve" in call_order, "resolve_jobright_pending was not called"
    assert "embed" in call_order and "job_sync" in call_order
    assert call_order.index("llm_enrich") < call_order.index("ats_resolve")
    assert call_order.index("ats_resolve") < call_order.index("embed")
    assert call_order.index("ats_resolve") < call_order.index("job_sync")
    assert result["stage_outcomes"].get("ats_resolve") == "ok"


def test_ats_resolve_skipped_when_no_serper_key(monkeypatch):
    """No SERPER_API_KEY -> stage skipped (no resolver call), pipeline healthy."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    def _boom(**kw):
        raise AssertionError("resolver must not run without a serper key")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.resolve_jobright_pending", _boom
    )

    result = run_daily_pipeline()

    assert "ats_resolve" not in call_order
    assert result["stage_outcomes"].get("ats_resolve") == "skipped"
    assert result["pipeline_status"] == "success"


def test_ats_resolve_crash_is_isolated(monkeypatch):
    """A resolver crash must NOT block embed/sync/gate (best-effort stage)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)

    def _crash(**kw):
        raise RuntimeError("simulated serper outage")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.resolve_jobright_pending", _crash
    )

    result = run_daily_pipeline()

    assert "embed" in call_order, "embed must still run after ats_resolve crash"
    assert "job_sync" in call_order, "sync must still run after ats_resolve crash"
    assert result["stage_outcomes"].get("ats_resolve") == "error"
    assert any("ATS resolve" in e for e in result["errors"])


def test_ats_resolve_infra_error_degrades_and_alerts(monkeypatch):
    """A DOWN Serper (infra_error=1, e.g. out of credits) must NOT be stamped
    'ok'. It flips the run to 'degraded'/partial, records an error, and fires a
    human alert — this is the signal that was missing for days."""
    from wekruit_matching.pipeline import daily as daily_mod
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.resolve_jobright_pending",
        lambda **kw: (
            call_order.append("ats_resolve"),
            {
                "resolved": 0, "missed": 0, "skipped": 0, "errors": 0,
                "aborted": 12302, "infra_error": 1,
                "infra_detail": "HTTP 400: Not enough credits",
            },
        )[1],
    )
    alerts: list[tuple] = []
    monkeypatch.setattr(
        daily_mod, "send_dependency_alert",
        lambda dep, detail, **kw: alerts.append((dep, detail, kw)) or True,
    )

    result = run_daily_pipeline()

    assert result["stage_outcomes"].get("ats_resolve") == "degraded"
    assert result["pipeline_status"] == "partial"
    assert any("Serper dependency DOWN" in e for e in result["errors"])
    # embed/sync still run — the degraded resolver does not block the pipeline.
    assert "embed" in call_order and "job_sync" in call_order
    # A human alert was fired naming Serper + the cause.
    assert alerts, "expected a dependency-down alert"
    assert alerts[0][0] == "Serper (ATS resolver)"
    assert "credits" in alerts[0][1].lower()


def test_ats_resolve_rate_collapse_degrades_and_alerts(monkeypatch):
    """Even WITHOUT an explicit infra_error, a 0% resolve rate over a non-trivial
    number of queries (e.g. a silent API change) must degrade + alert."""
    from wekruit_matching.pipeline import daily as daily_mod
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    call_order = _patch_all_stages(monkeypatch)
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.resolve_jobright_pending",
        lambda **kw: (
            call_order.append("ats_resolve"),
            {
                "resolved": 0, "missed": 200, "skipped": 0, "errors": 0,
                "aborted": 0, "infra_error": 0, "infra_detail": "",
            },
        )[1],
    )
    alerts: list[tuple] = []
    monkeypatch.setattr(
        daily_mod, "send_dependency_alert",
        lambda dep, detail, **kw: alerts.append((dep, detail, kw)) or True,
    )

    result = run_daily_pipeline()

    assert result["stage_outcomes"].get("ats_resolve") == "degraded"
    assert result["pipeline_status"] == "partial"
    assert any("resolve-rate collapse" in e for e in result["errors"])
    assert alerts, "expected a dependency-down alert on 0% resolve rate"


def test_dead_backfill_skip_degrades_and_alerts(monkeypatch):
    """If the Firestore dead-flag mirror SKIPS (no SDK/creds), confirmed-dead
    postings are not reconciled and can be served to users as 404s. The stage
    must degrade + alert, not silently stamp 'ok' (the documented 'dead jobs
    served to users' regression, re-armed by an FS credential outage)."""
    from wekruit_matching.pipeline import daily as daily_mod
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.firestore_dead_backfill",
        lambda conn: {"synced": 0, "total_seen": 0, "skipped": "no_creds"},
    )
    alerts: list[tuple] = []
    monkeypatch.setattr(
        daily_mod, "send_dependency_alert",
        lambda dep, detail, **kw: alerts.append((dep, detail, kw)) or True,
    )

    result = run_daily_pipeline()

    assert result["stage_outcomes"].get("dead_backfill") == "degraded"
    assert result["pipeline_status"] == "partial"
    assert any("dead-flag mirror skipped" in e for e in result["errors"])
    assert alerts and alerts[0][0] == "Firestore (dead-flag mirror)"


# ---------------------------------------------------------------------------
# Fix #5: Stage 4.5 Firestore <-> Postgres reconcile
# ---------------------------------------------------------------------------


def test_firestore_reconcile_ok(monkeypatch):
    """Reconcile ok -> stage_outcomes 'ok', pipeline still success (non-fatal)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    result = run_daily_pipeline()

    assert result["stage_outcomes"].get("firestore_reconcile") == "ok"
    assert result["pipeline_status"] == "success"


def test_firestore_reconcile_degraded_is_non_fatal(monkeypatch):
    """Real divergence -> 'degraded' in stage_outcomes but NEVER flips the run
    to partial/failed (must not append to errors)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.firestore_reconcile",
        lambda conn, **kw: {
            "ok": False,
            "skipped": False,
            "divergence": 0.30,
            "pg_matchable": 100,
            "fs_active": 130,
            "fs_total": 140,
            "reason": "PG matchable=100 vs Firestore active=130 diverge 30.0% > 5%",
        },
    )

    result = run_daily_pipeline()

    assert result["stage_outcomes"].get("firestore_reconcile") == "degraded"
    assert not any("reconcile" in e.lower() for e in result["errors"]), result["errors"]
    assert result["pipeline_status"] == "success"


def test_firestore_reconcile_skipped_falls_back_to_sync_count(monkeypatch):
    """When Firestore read is unavailable, fall back to PG-matchable vs the
    active_jobs count Stage 4 reported; a sane window stays 'skipped' (not a
    false degraded)."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    # _SYNC_STATS reports active_jobs=3; pg_matchable=5 -> over = max(3-5,0)/5 = 0.
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.firestore_reconcile",
        lambda conn, **kw: {
            "ok": True,
            "skipped": True,
            "divergence": 0.0,
            "pg_matchable": 5,
            "fs_active": 0,
            "fs_total": 0,
            "reason": "firestore client unavailable",
        },
    )

    result = run_daily_pipeline()

    assert result["stage_outcomes"].get("firestore_reconcile") == "skipped"
    assert result["pipeline_status"] == "success"


def test_firestore_reconcile_crash_is_non_fatal(monkeypatch):
    """A reconcile crash records 'error' in stage_outcomes but does not gate."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)

    def _crash(conn, **kw):
        raise RuntimeError("firestore boom")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.firestore_reconcile", _crash
    )

    result = run_daily_pipeline()

    assert result["stage_outcomes"].get("firestore_reconcile") == "error"
    assert not any("reconcile" in e.lower() for e in result["errors"]), result["errors"]
    assert result["pipeline_status"] == "success"
