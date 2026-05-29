"""Integration tests: the post-run reliability gate is wired into
run_daily_pipeline() as a final stage and flips the run when data quality
regresses, even though every pipeline stage 'succeeded' (no exception).

Reuses the stub harness from test_pipeline_daily so all costly + DB stages are
mocked; only the gate behaviour differs per test.
"""

from __future__ import annotations

import pytest

from tests.test_pipeline_daily import _patch_all_stages  # reuse stub harness


def _disable_optional_scrapers(monkeypatch):
    for var in (
        "ENABLE_WELLFOUND_SCRAPE", "ENABLE_LINKEDIN_SCRAPE", "ENABLE_OTTA_SCRAPE",
        "ENABLE_GREENHOUSE_DIRECT", "ENABLE_LEVER_DIRECT", "ENABLE_ASHBY_DIRECT",
    ):
        monkeypatch.setenv(var, "0")


def test_gate_failure_flips_status_and_feeds_email(monkeypatch):
    """All stages OK but the gate reports a data-quality failure =>
    pipeline_status must NOT be 'success', errors must mention the gate, and
    the completion email must receive the health_failures."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    _disable_optional_scrapers(monkeypatch)

    captured: dict = {}

    def _capture_email(**kw):
        captured.update(kw)
        return True

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.send_pipeline_complete_email",
        _capture_email,
    )

    failure = {
        "metric": "embedded_cov_of_active",
        "value": 0.7813,
        "threshold": 0.97,
        "message": "embedded coverage of active 0.7813 below 0.97 "
                   "(6166 active jobs not matchable)",
    }
    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_health_gate",
        lambda **kw: {
            "ok": False,
            "metrics": {"active": 28195, "matchable_corpus": 22029,
                        "embedded_cov_of_active": 0.7813,
                        "embeddable_unembedded_backlog": 34},
            "failures": [failure],
        },
    )

    result = run_daily_pipeline()

    # Status is degraded (core stages ok + gate failure -> 'partial').
    assert result["pipeline_status"] != "success", result["stage_outcomes"]
    # Gate failure recorded both as an error and in health_failures.
    assert any("reliability gate" in e.lower() for e in result["errors"]), \
        result["errors"]
    assert result["health_failures"] and \
        result["health_failures"][0]["metric"] == "embedded_cov_of_active"
    assert result["stage_outcomes"].get("health_gate") == "failed"
    # The completion email received the failures so a human is alerted.
    assert captured.get("health_failures")
    assert captured["health_failures"][0]["metric"] == "embedded_cov_of_active"


def test_gate_pass_keeps_success(monkeypatch):
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    _disable_optional_scrapers(monkeypatch)

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_health_gate",
        lambda **kw: {"ok": True,
                      "metrics": {"active": 28195, "matchable_corpus": 27500},
                      "failures": []},
    )

    result = run_daily_pipeline()
    assert result["pipeline_status"] == "success", result["stage_outcomes"]
    assert result["errors"] == []
    assert result["stage_outcomes"].get("health_gate") == "ok"
    assert result["health_failures"] == []


def test_gate_crash_is_fail_closed(monkeypatch):
    """A crash inside the gate must NOT silently pass; it must be recorded as a
    failure (fail-closed) so the regression stays visible."""
    from wekruit_matching.pipeline.daily import run_daily_pipeline

    _patch_all_stages(monkeypatch)
    _disable_optional_scrapers(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.daily.run_health_gate", _boom
    )

    result = run_daily_pipeline()
    assert result["pipeline_status"] != "success"
    assert result["stage_outcomes"].get("health_gate") == "error"
    assert any("reliability gate crash" in e.lower() for e in result["errors"])
