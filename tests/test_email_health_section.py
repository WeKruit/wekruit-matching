"""The completion email must render the reliability-gate section so a human is
alerted to a data-quality regression. Intercepts the Mailgun send so nothing
goes out; asserts on the rendered HTML."""

from __future__ import annotations

import pytest

from wekruit_matching.notifications import email


def _intercept(monkeypatch) -> dict:
    captured: dict = {}

    def _fake_send(subject, html, text=""):
        captured["subject"] = subject
        captured["html"] = html
        return True

    monkeypatch.setattr(email, "_send_email", _fake_send)
    return captured


def _base_kwargs() -> dict:
    return dict(
        scrape_stats={"simplify": {"inserted": 1, "stale": 0, "unchanged": 0}},
        jd_stats={"processed": 0, "failed": 0, "credits_used": 0},
        enrich_stats={"enriched": 0, "failed": 0},
        embed_stats={"embedded": 0, "failed": 0},
        duration_seconds=120.0,
        errors=[],
    )


def test_health_failures_rendered(monkeypatch):
    captured = _intercept(monkeypatch)
    email.send_pipeline_complete_email(
        **_base_kwargs(),
        health_metrics={"active": 28195, "matchable_corpus": 22029,
                        "embedded_cov_of_active": 0.7813,
                        "embeddable_unembedded_backlog": 34},
        health_failures=[{
            "metric": "embedded_cov_of_active", "value": 0.7813,
            "threshold": 0.97,
            "message": "embedded coverage of active 0.7813 below 0.97 "
                       "(6166 active jobs not matchable)",
        }],
    )
    html = captured["html"]
    assert "Reliability Gate FAILED" in html
    assert "embedded_cov_of_active" in html
    assert "6166 active jobs not matchable" in html


def test_health_pass_shows_summary(monkeypatch):
    captured = _intercept(monkeypatch)
    email.send_pipeline_complete_email(
        **_base_kwargs(),
        health_metrics={"active": 28195, "matchable_corpus": 27500,
                        "embedded_cov_of_active": 0.9753,
                        "embeddable_unembedded_backlog": 12},
        health_failures=[],
    )
    html = captured["html"]
    assert "Reliability Gate" in html
    assert "PASS" in html


def test_backward_compatible_without_health_args(monkeypatch):
    """Existing callers that pass no health args must still work and must NOT
    render a reliability section."""
    captured = _intercept(monkeypatch)
    email.send_pipeline_complete_email(**_base_kwargs())
    assert "Reliability Gate" not in captured["html"]


# ---------------------------------------------------------------------------
# 2026-06-04: dependency-down alert + degraded-stage visibility in the email.
# ---------------------------------------------------------------------------

def test_send_dependency_alert_subject_and_body(monkeypatch):
    captured = _intercept(monkeypatch)
    ok = email.send_dependency_alert(
        "Serper (ATS resolver)",
        "HTTP 400: Not enough credits",
        impact="New jobright jobs lack direct ATS apply URLs.",
        action="Top up Serper credits.",
    )
    assert ok is True
    assert "DEPENDENCY DOWN" in captured["subject"]
    assert "Serper (ATS resolver)" in captured["subject"]
    html = captured["html"]
    assert "Not enough credits" in html
    assert "Top up Serper credits." in html


def test_completion_subject_prefixed_degraded_when_stage_degraded(monkeypatch):
    captured = _intercept(monkeypatch)
    email.send_pipeline_complete_email(
        **_base_kwargs(),
        stage_outcomes={"scrape": "ok", "ats_resolve": "degraded", "embed": "ok"},
    )
    assert captured["subject"].startswith("[DEGRADED] "), captured["subject"]
    assert "Degraded / Failed Stages" in captured["html"]
    assert "ats_resolve" in captured["html"]


def test_completion_subject_clean_when_all_ok(monkeypatch):
    captured = _intercept(monkeypatch)
    email.send_pipeline_complete_email(
        **_base_kwargs(),
        stage_outcomes={"scrape": "ok", "ats_resolve": "ok", "embed": "ok"},
    )
    assert not captured["subject"].startswith("[DEGRADED]"), captured["subject"]
    assert "Degraded / Failed Stages" not in captured["html"]


def test_url_resolution_stats_rendered_when_passed(monkeypatch):
    """The resolver's resolved-count/rate must render (was always 0 because
    daily.py never wired url_resolution_stats)."""
    captured = _intercept(monkeypatch)
    email.send_pipeline_complete_email(
        **_base_kwargs(),
        url_resolution_stats={"total_resolved": 873, "resolution_rate": 0.87},
    )
    assert "873" in captured["html"]
