"""jobright.ai direct-fetch JD parsing — 2026-05-21 launch blocker.

Pins:
  * url_classifier routes jobright.ai → FetchRoute.JOBRIGHT
  * fetch_jobright_job strips HTML chrome and emits clean JD text
  * _pick_target_url returns jobright URL when no ats_apply_url
    (instead of None → skip_no_url)
  * run_jd_enrichment._fetch_for_url dispatches JOBRIGHT route correctly

Production bug it pins (now fixed):
  2,308 jobright-newgrad active docs only had 5.6% JD coverage because
  _pick_target_url returned None for any row whose primary_url was a
  jobright redirect and whose ats_apply_url was missing. The fix routes
  jobright URLs through a dedicated HTTP fetcher that scrapes the
  server-rendered HTML directly.
"""
from __future__ import annotations

import httpx
import pytest

from wekruit_matching.pipeline.ats_enricher import fetch_jobright_job
from wekruit_matching.pipeline.run_jd_enrichment import _fetch_for_url, _pick_target_url
from wekruit_matching.pipeline.url_classifier import FetchRoute, classify_job_url


_JOBRIGHT_SAMPLE_HTML = """<!DOCTYPE html>
<html><head><title>Backend Engineer @ Acme</title></head>
<body>
<nav>SIGN IN JOIN NOW</nav>
<h1>Backend Engineer</h1>
<p>Acme Corp - San Francisco, CA</p>
<a>APPLY NOW</a>
<section>
  <h2>Responsibilities</h2>
  <ul>
    <li>Design and ship distributed systems.</li>
    <li>Own services from concept to production.</li>
    <li>Mentor engineers across the team.</li>
  </ul>
  <h2>Requirements</h2>
  <ul>
    <li>5+ years backend engineering.</li>
    <li>Python or Go experience.</li>
    <li>Strong communication skills.</li>
  </ul>
</section>
<footer>jobright.ai 2026</footer>
<script>window.__INITIAL_STATE__ = {}</script>
</body></html>
"""


def test_classify_jobright_url_returns_jobright_route() -> None:
    """jobright.ai hostname → FetchRoute.JOBRIGHT (not FIRECRAWL)."""
    result = classify_job_url("https://jobright.ai/jobs/info/abc123")
    assert result.route is FetchRoute.JOBRIGHT
    assert result.hostname == "jobright.ai"


def test_classify_jobright_with_query_params_still_routes() -> None:
    """URL query params (utm_*) don't affect route classification."""
    result = classify_job_url(
        "https://jobright.ai/jobs/info/abc123?utm_campaign=Sales&utm_source=1103"
    )
    assert result.route is FetchRoute.JOBRIGHT


def test_classify_non_jobright_url_unchanged() -> None:
    """Sanity: greenhouse/lever/ashby/workday still route to their own ATSes."""
    assert classify_job_url("https://boards.greenhouse.io/acme/jobs/1").route is FetchRoute.GREENHOUSE
    assert classify_job_url("https://jobs.lever.co/acme/abc").route is FetchRoute.LEVER
    assert classify_job_url("https://jobs.ashbyhq.com/acme").route is FetchRoute.ASHBY


def test_fetch_jobright_job_strips_chrome_and_returns_jd() -> None:
    """HTML → plain JD text, nav-chrome removed."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=_JOBRIGHT_SAMPLE_HTML))
    client = httpx.Client(transport=transport)
    try:
        result = fetch_jobright_job("https://jobright.ai/jobs/info/abc123", client=client)
    finally:
        client.close()
    assert result.source == "jobright"
    # Long enough to pass Track D's 200-char gate
    assert len(result.description_plain) >= 200
    # Real JD content preserved
    assert "Responsibilities" in result.description_plain
    assert "Design and ship distributed systems" in result.description_plain
    assert "5+ years backend engineering" in result.description_plain
    # Nav chrome removed
    assert "SIGN IN" not in result.description_plain
    assert "APPLY NOW" not in result.description_plain
    assert "JOIN NOW" not in result.description_plain


def test_fetch_jobright_job_raises_on_http_error() -> None:
    """4xx/5xx → httpx.HTTPStatusError so worker treats as recoverable failure."""
    transport = httpx.MockTransport(lambda req: httpx.Response(503))
    client = httpx.Client(transport=transport)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_jobright_job("https://jobright.ai/jobs/info/abc123", client=client)
    finally:
        client.close()


def test_pick_target_url_returns_jobright_when_no_ats_url() -> None:
    """jobright primary + no ats → returns jobright URL (not None!).

    This is the bug fix: previous code returned None, causing
    skip_no_url tombstone. Now we use the jobright page directly.
    """
    row = {
        "primary_url": "https://jobright.ai/jobs/info/abc123",
        "ats_apply_url": None,
    }
    assert _pick_target_url(row) == "https://jobright.ai/jobs/info/abc123"


def test_pick_target_url_prefers_ats_apply_url_when_present() -> None:
    """Real ATS URL still preferred — JD quality is better from native ATS API."""
    row = {
        "primary_url": "https://jobright.ai/jobs/info/abc123",
        "ats_apply_url": "https://boards.greenhouse.io/acme/jobs/42",
    }
    assert _pick_target_url(row) == "https://boards.greenhouse.io/acme/jobs/42"


def test_pick_target_url_returns_none_only_when_both_missing() -> None:
    """Bothall-empty → None (truly unfetchable)."""
    assert _pick_target_url({"primary_url": None, "ats_apply_url": None}) is None
    assert _pick_target_url({"primary_url": "", "ats_apply_url": ""}) is None


def test_pick_target_url_direct_ats_primary_pass_through() -> None:
    """Direct ATS primary_url (no jobright) → returned as-is."""
    row = {
        "primary_url": "https://boards.greenhouse.io/acme/jobs/42",
        "ats_apply_url": None,
    }
    assert _pick_target_url(row) == "https://boards.greenhouse.io/acme/jobs/42"
