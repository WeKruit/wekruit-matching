"""Tests for the post-run reliability / data-quality gate.

These use synthetic metric dicts (for the pure ``evaluate``) and mocked DB /
``compute_metrics`` (for ``run_health_gate``), so they run without a database.
Thresholds mirror the live baseline measured 2026-05-29:
    active=28195, active_embedded=22029, matchable_corpus=22029,
    embedded_cov_of_active=0.7813, embeddable_unembedded_backlog=34,
    sponsorship=0.169, seniority=0.206, industry=0.989, skills=0.788.
"""

from __future__ import annotations

import json

import pytest

from wekruit_matching.pipeline import health_gate as hg


def _healthy_metrics() -> dict:
    """A HEALTHY corpus: embedded coverage above floor, tiny backlog."""
    return {
        "active": 28195,
        "active_enriched": 28014,
        "active_embedded": 27500,
        "matchable_corpus": 27500,
        "embeddable_unembedded_backlog": 34,
        "embedded_cov_of_active": 27500 / 28195,  # 0.9753
        "sponsorship_cov_of_enriched": 0.169,
        "seniority_cov_of_enriched": 0.206,
        "industry_cov_of_enriched": 0.989,
        "skills_nonempty_cov_of_enriched": 0.788,
    }


def _live_today_metrics() -> dict:
    """The ACTUAL live numbers on 2026-05-29 (embedded coverage 0.7813)."""
    return {
        "active": 28195,
        "active_enriched": 28014,
        "active_embedded": 22029,
        "matchable_corpus": 22029,
        "embeddable_unembedded_backlog": 34,
        "embedded_cov_of_active": 22029 / 28195,  # 0.7813
        "sponsorship_cov_of_enriched": 0.1689,
        "seniority_cov_of_enriched": 0.2059,
        "industry_cov_of_enriched": 0.9891,
        "skills_nonempty_cov_of_enriched": 0.7875,
    }


# --- thresholds -------------------------------------------------------------

def test_default_thresholds_sane():
    t = hg.DEFAULT_THRESHOLDS
    assert 0.90 <= t["min_embedded_cov_of_active"] < 1.0
    assert t["max_embeddable_unembedded_backlog"] >= 100
    assert 0 < t["max_matchable_drop_frac"] <= 0.2
    assert t["min_active"] >= 1


# --- healthy passes ---------------------------------------------------------

def test_healthy_passes_no_prior():
    assert hg.evaluate(_healthy_metrics(), prior=None) == []


def test_healthy_passes_stable_prior():
    m = _healthy_metrics()
    assert hg.evaluate(m, prior=dict(m)) == []


# --- the live defect IS caught ---------------------------------------------

def test_live_today_embedded_coverage_cliff_is_caught():
    """The real 2026-05-29 corpus (0.7813 embedded coverage) MUST fail the
    absolute floor even with no prior run -- this is the defect the gate
    exists to surface."""
    failures = hg.evaluate(_live_today_metrics(), prior=None)
    keys = {f["metric"] for f in failures}
    assert "embedded_cov_of_active" in keys
    # message should quantify the non-matchable jobs (28195 - 22029 = 6166)
    msg = next(f["message"] for f in failures
               if f["metric"] == "embedded_cov_of_active")
    assert "6166" in msg


def test_live_today_low_fields_do_NOT_false_positive():
    """sponsorship 0.169 / seniority 0.206 must NOT trip an absolute floor;
    only embedded coverage should fail today (no prior run)."""
    failures = hg.evaluate(_live_today_metrics(), prior=None)
    keys = {f["metric"] for f in failures}
    assert "sponsorship_cov_of_enriched" not in keys
    assert "seniority_cov_of_enriched" not in keys
    assert keys == {"embedded_cov_of_active"}


# --- absolute floors --------------------------------------------------------

def test_embedded_coverage_below_floor_fails():
    m = _healthy_metrics()
    m["active_embedded"] = int(0.90 * m["active"])
    m["matchable_corpus"] = m["active_embedded"]
    m["embedded_cov_of_active"] = m["active_embedded"] / m["active"]
    keys = {f["metric"] for f in hg.evaluate(m, prior=None)}
    assert "embedded_cov_of_active" in keys


def test_large_embeddable_backlog_fails():
    m = _healthy_metrics()
    m["embeddable_unembedded_backlog"] = 5000
    keys = {f["metric"] for f in hg.evaluate(m, prior=None)}
    assert "embeddable_unembedded_backlog" in keys


def test_active_zero_fails_without_prior():
    m = _healthy_metrics()
    m.update(active=0, active_enriched=0, active_embedded=0,
             matchable_corpus=0, embedded_cov_of_active=0.0)
    keys = {f["metric"] for f in hg.evaluate(m, prior=None)}
    assert "active" in keys


# --- relative drop guards (need prior) -------------------------------------

def test_matchable_cliff_vs_prior_fails():
    prior = _healthy_metrics()
    m = _healthy_metrics()
    m["matchable_corpus"] = int(0.5 * prior["matchable_corpus"])
    keys = {f["metric"] for f in hg.evaluate(m, prior=prior)}
    assert "matchable_corpus" in keys


def test_matchable_small_drop_ok_vs_prior():
    prior = _healthy_metrics()
    m = _healthy_metrics()
    m["matchable_corpus"] = int(0.95 * prior["matchable_corpus"])  # 5% drop
    keys = {f["metric"] for f in hg.evaluate(m, prior=prior)}
    assert "matchable_corpus" not in keys


def test_matchable_growth_never_fails():
    prior = _healthy_metrics()
    m = _healthy_metrics()
    m["matchable_corpus"] = prior["matchable_corpus"] + 5000
    keys = {f["metric"] for f in hg.evaluate(m, prior=prior)}
    assert "matchable_corpus" not in keys


def test_active_collapse_vs_prior_fails():
    prior = _healthy_metrics()
    m = _healthy_metrics()
    m["active"] = 1000
    keys = {f["metric"] for f in hg.evaluate(m, prior=prior)}
    assert "active" in keys


def test_field_coverage_regression_vs_prior_fails():
    """Low-but-stable fields are fine; a real DROP vs prior trips the guard."""
    prior = _healthy_metrics()  # sponsorship 0.169
    m = _healthy_metrics()
    m["sponsorship_cov_of_enriched"] = 0.05  # -11.9 pts
    keys = {f["metric"] for f in hg.evaluate(m, prior=prior)}
    assert "sponsorship_cov_of_enriched" in keys


def test_field_coverage_stable_low_ok_vs_prior():
    prior = _healthy_metrics()
    m = _healthy_metrics()  # identical low sponsorship 0.169
    keys = {f["metric"] for f in hg.evaluate(m, prior=prior)}
    assert "sponsorship_cov_of_enriched" not in keys


# --- failure object shape + summary ----------------------------------------

def test_failure_objects_well_formed():
    m = _healthy_metrics()
    m["embeddable_unembedded_backlog"] = 9999
    failures = hg.evaluate(m, prior=None)
    assert failures
    for f in failures:
        assert set(f) >= {"metric", "value", "threshold", "message"}
        assert isinstance(f["message"], str) and f["message"]


def test_summarize_ok_when_empty():
    assert "OK" in hg.summarize_failures([])


def test_summarize_lists_failures():
    text = hg.summarize_failures([
        {"metric": "embedded_cov_of_active", "value": 0.78, "threshold": 0.97,
         "message": "coverage low"},
    ])
    assert "embedded_cov_of_active" in text


# --- state persistence ------------------------------------------------------

def test_state_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    m = _healthy_metrics()
    hg.save_state(m, path=p)
    loaded = hg.load_prior_state(path=p)
    assert loaded["matchable_corpus"] == m["matchable_corpus"]
    assert loaded["active"] == m["active"]


def test_load_missing_returns_none(tmp_path):
    assert hg.load_prior_state(path=tmp_path / "nope.json") is None


def test_load_corrupt_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert hg.load_prior_state(path=p) is None  # must not raise


def test_save_atomic_no_temp_leftovers(tmp_path):
    p = tmp_path / "s.json"
    hg.save_state({"active": 1, "matchable_corpus": 1}, path=p)
    hg.save_state({"active": 2, "matchable_corpus": 2}, path=p)
    assert json.loads(p.read_text())["metrics"]["active"] == 2
    assert list(tmp_path.glob("*.tmp")) == []


# --- run_health_gate orchestration (DB + compute mocked) -------------------

class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_run_health_gate_passes_on_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(hg, "get_connection", lambda: _Conn())
    monkeypatch.setattr(hg, "compute_metrics", lambda conn: _healthy_metrics())
    state = tmp_path / "s.json"
    result = hg.run_health_gate(state_path=state)
    assert result["ok"] is True
    assert result["failures"] == []
    assert state.exists()  # state persisted for next run


def test_run_health_gate_fails_on_live_cliff(monkeypatch, tmp_path):
    monkeypatch.setattr(hg, "get_connection", lambda: _Conn())
    monkeypatch.setattr(hg, "compute_metrics", lambda conn: _live_today_metrics())
    result = hg.run_health_gate(state_path=tmp_path / "s.json")
    assert result["ok"] is False
    assert any(f["metric"] == "embedded_cov_of_active"
               for f in result["failures"])


def test_run_health_gate_uses_prior_for_drop(monkeypatch, tmp_path):
    state = tmp_path / "s.json"
    monkeypatch.setattr(hg, "get_connection", lambda: _Conn())
    # Run 1: healthy, persists state.
    monkeypatch.setattr(hg, "compute_metrics", lambda conn: _healthy_metrics())
    assert hg.run_health_gate(state_path=state)["ok"] is True
    # Run 2: matchable corpus cliff vs the saved prior -> must fail.
    cliff = _healthy_metrics()
    cliff["matchable_corpus"] = 10000
    monkeypatch.setattr(hg, "compute_metrics", lambda conn: cliff)
    r2 = hg.run_health_gate(state_path=state)
    assert r2["ok"] is False
    assert any(f["metric"] == "matchable_corpus" for f in r2["failures"])
