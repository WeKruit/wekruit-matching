"""Unit tests for the JD enrichment orchestrator."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from wekruit_matching.pipeline.ats_enricher import build_ats_job_data


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, batches):
        self._batches = list(batches)
        self.executed: list[tuple[str, dict | None]] = []
        self.commit_count = 0

    def execute(self, query: str, params: dict | None = None):
        self.executed.append((query, params))
        if query.lstrip().startswith("SELECT"):
            rows = self._batches.pop(0) if self._batches else []
            return _FakeResult(rows)
        return _FakeResult([])

    def commit(self):
        self.commit_count += 1


def _settings(**overrides):
    defaults = {
        "firecrawl_api_key": "",
        "firecrawl_base_url": "https://api.firecrawl.dev",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_run_jd_enrichment_writes_successful_greenhouse_results(monkeypatch) -> None:
    """Successful ATS fetches should update JD fields and tracking metadata."""
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "a" * 64,
                "company_name": "Acme",
                "role_title": "Backend Engineer",
                "primary_url": "https://boards.greenhouse.io/acme/jobs/123",
            }
        ], []]
    )

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        lambda url: build_ats_job_data(
            source="greenhouse",
            description_plain="Build APIs for the matching engine.",
            qualifications=["Python"],
        ),
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["processed"] == 1
    assert stats["failed"] == 0
    assert conn.commit_count == 1
    update_params = [
        params for query, params in conn.executed if query.lstrip().startswith("UPDATE")
    ]
    assert update_params
    assert update_params[0]["jd_fetch_source"] == "greenhouse"
    assert update_params[0]["job_description"] == "Build APIs for the matching engine."
    assert update_params[0]["qualifications"] == ["Python"]
    assert len(update_params[0]["ats_content_hash"]) == 64


def test_run_jd_enrichment_dry_run_skips_network_and_db_writes(monkeypatch) -> None:
    """Dry-run should classify and count work without fetching or updating."""
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "b" * 64,
                "company_name": "Acme",
                "role_title": "Platform Engineer",
                "primary_url": "https://jobs.lever.co/acme/abc123",
            }
        ], []]
    )

    def _should_not_run(_url: str):
        raise AssertionError("network fetch should not run in dry-run mode")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_lever_job",
        _should_not_run,
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
        dry_run=True,
    )

    assert stats["dry_run"] is True
    assert stats["processed"] == 1
    assert conn.commit_count == 0
    assert not [query for query, _ in conn.executed if query.lstrip().startswith("UPDATE")]


def test_run_jd_enrichment_uses_search_before_fetching_aggregator_urls(monkeypatch) -> None:
    """Aggregator URLs should search for a canonical employer URL before fetching."""
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "c" * 64,
                "company_name": "Acme",
                "role_title": "ML Intern",
                "primary_url": "https://www.linkedin.com/jobs/view/123",
            }
        ], []]
    )

    async def _search(**_kwargs):
        return "https://boards.greenhouse.io/acme/jobs/999"

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.search_canonical_job_url",
        _search,
    )
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        lambda url: build_ats_job_data(
            source="greenhouse",
            description_plain="Research ranking models for interns.",
        ),
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(firecrawl_api_key="fc-test"),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["processed"] == 1
    update_params = [
        params for query, params in conn.executed if query.lstrip().startswith("UPDATE")
    ]
    assert update_params[0]["jd_fetch_source"] == "greenhouse"


# ---------------------------------------------------------------------------
# P7-F (2026-05-08) — Stage 2b gating fix tests
#
# Mirrors the P7-E pattern at Stage 2c (tests/test_enrichment_worker.py):
# stuck-failed jobs must re-enter the queue after STAGE2B_STALE_DAYS days,
# but permanent failures (404 / dead URL) must NOT.
# ---------------------------------------------------------------------------


def test_select_query_uses_two_clause_gating_with_staleness_window() -> None:
    """SELECT must allow never-tried jobs OR aged-out recoverable failures.

    The two-clause OR guarantees:
      - Fresh jobs (jd_fetch_attempted_at IS NULL) enter on first run
      - Recoverable failures re-enter after STAGE2B_STALE_DAYS days
      - Permanent 404s NEVER re-enter (excluded by COALESCE(permanent_404,FALSE))
    """
    from wekruit_matching.pipeline.run_jd_enrichment import (
        STAGE2B_STALE_DAYS,
        run_jd_enrichment,
    )

    conn = _FakeConn([[]])
    run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    select_query = next(q for q, _ in conn.executed if q.lstrip().startswith("SELECT"))
    assert "jd_fetch_attempted_at IS NULL" in select_query, (
        "SELECT must still allow first-time fetches"
    )
    assert f"INTERVAL '{STAGE2B_STALE_DAYS} days'" in select_query, (
        f"SELECT must reference STAGE2B_STALE_DAYS={STAGE2B_STALE_DAYS} (P7-F gating fix)"
    )
    assert "COALESCE(permanent_404, FALSE) = FALSE" in select_query, (
        "SELECT must exclude permanent 404 rows even after staleness window"
    )
    assert "jd_fetch_source = 'failed'" in select_query, (
        "SELECT staleness branch must scope to failed rows (not e.g. 'serper')"
    )


def test_select_query_keeps_data_gap_predicate() -> None:
    """Fully-fetched jobs (have JD) must never re-enter regardless of age."""
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn([[]])
    run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    select_query = next(q for q, _ in conn.executed if q.lstrip().startswith("SELECT"))
    assert "job_description IS NULL" in select_query
    assert "job_description = ''" in select_query


def test_404_response_marks_permanent_404_true(monkeypatch) -> None:
    """When the fetcher raises HTTPStatusError 404, _write_failure must
    persist permanent_404=TRUE so the row is excluded from future runs.

    P7-F red-line two: the permanent_404 logic is only verified when a
    test simulates a 404 response and asserts the flag is set. This is it.
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "d" * 64,
                "company_name": "ZombieCo",
                "role_title": "Pulled Listing",
                "primary_url": "https://boards.greenhouse.io/zombieco/jobs/dead",
            }
        ], []]
    )

    def _raise_404(_url: str):
        request = httpx.Request("GET", _url)
        response = httpx.Response(status_code=404, request=request)
        raise httpx.HTTPStatusError(
            "404 Not Found", request=request, response=response
        )

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        _raise_404,
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["failed"] == 1
    update_params = [
        params for query, params in conn.executed if query.lstrip().startswith("UPDATE")
    ]
    assert update_params, "expected one UPDATE for the failed row"
    assert update_params[0]["jd_fetch_source"] == "failed"
    assert update_params[0]["permanent_404"] is True, (
        "404 from fetcher must mark permanent_404=TRUE — otherwise the row "
        "would re-enter the queue after the staleness window expires."
    )


def test_recoverable_5xx_does_not_mark_permanent_404(monkeypatch) -> None:
    """When the fetcher raises HTTPStatusError 503 (or similar 5xx), the
    row must be marked permanent_404=FALSE so it re-enters the queue after
    the staleness window — these are transient outages (e.g. Firecrawl down).
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "e" * 64,
                "company_name": "TransientCo",
                "role_title": "Recoverable Role",
                "primary_url": "https://boards.greenhouse.io/transientco/jobs/123",
            }
        ], []]
    )

    def _raise_503(_url: str):
        request = httpx.Request("GET", _url)
        response = httpx.Response(status_code=503, request=request)
        raise httpx.HTTPStatusError(
            "503 Service Unavailable", request=request, response=response
        )

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        _raise_503,
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["failed"] == 1
    update_params = [
        params for query, params in conn.executed if query.lstrip().startswith("UPDATE")
    ]
    assert update_params, "expected one UPDATE for the failed row"
    assert update_params[0]["permanent_404"] is False, (
        "5xx must be classified as recoverable — row should re-enter after "
        "staleness window when upstream service is healthy."
    )


def test_recoverable_timeout_does_not_mark_permanent_404(monkeypatch) -> None:
    """ConnectError / TimeoutException are recoverable — the upstream is
    temporarily unreachable. Row must retry after staleness window.
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "f" * 64,
                "company_name": "FlakyNet",
                "role_title": "Network Engineer",
                "primary_url": "https://boards.greenhouse.io/flakynet/jobs/456",
            }
        ], []]
    )

    def _raise_timeout(_url: str):
        raise httpx.ConnectTimeout("connection timed out")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        _raise_timeout,
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["failed"] == 1
    update_params = [
        params for query, params in conn.executed if query.lstrip().startswith("UPDATE")
    ]
    assert update_params[0]["permanent_404"] is False, (
        "Timeout must be classified as recoverable — row should retry after "
        "staleness window."
    )


def test_lookup_error_marks_permanent_404_true(monkeypatch) -> None:
    """When fetch_workday_job raises LookupError (page exists but no job
    posting / CXS endpoint not discoverable), the listing was pulled —
    permanent.
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "1" * 64,
                "company_name": "WorkdayCorp",
                "role_title": "Pulled Workday Role",
                "primary_url": "https://workdaycorp.wd1.myworkdayjobs.com/recruiting/dead",
            }
        ], []]
    )

    async def _fake_firecrawl(*_args, **_kwargs):
        raise LookupError("Could not discover Workday CXS endpoint for url")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_firecrawl_job",
        _fake_firecrawl,
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(firecrawl_api_key="fc-test"),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["failed"] == 1
    update_params = [
        params for query, params in conn.executed if query.lstrip().startswith("UPDATE")
    ]
    assert update_params[0]["permanent_404"] is True, (
        "LookupError on Workday CXS discovery means the listing is gone — "
        "permanent."
    )


def test_successful_fetch_clears_permanent_404_flag(monkeypatch) -> None:
    """A previously-failed row that now succeeds must clear permanent_404 to
    FALSE — defends against state-flip edge cases (manual DB edit, schema
    backfill setting True erroneously, etc).
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "2" * 64,
                "company_name": "ResurrectCo",
                "role_title": "Brought Back Role",
                "primary_url": "https://boards.greenhouse.io/resurrectco/jobs/789",
            }
        ], []]
    )

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        lambda url: build_ats_job_data(
            source="greenhouse",
            description_plain="JD content available now.",
        ),
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["processed"] == 1
    update_query = next(
        q for q, _ in conn.executed if q.lstrip().startswith("UPDATE")
    )
    assert "permanent_404 = FALSE" in update_query, (
        "Successful fetch must reset permanent_404 to FALSE."
    )


def test_is_permanent_404_helper_classification() -> None:
    """Direct unit test of the classifier — guards against silent regressions
    in future fetcher additions.
    """
    from wekruit_matching.pipeline.run_jd_enrichment import _is_permanent_404

    request = httpx.Request("GET", "https://example.com")
    resp_404 = httpx.Response(status_code=404, request=request)
    resp_500 = httpx.Response(status_code=500, request=request)
    resp_429 = httpx.Response(status_code=429, request=request)

    assert _is_permanent_404(
        httpx.HTTPStatusError("404", request=request, response=resp_404)
    ) is True
    assert _is_permanent_404(
        httpx.HTTPStatusError("500", request=request, response=resp_500)
    ) is False
    assert _is_permanent_404(
        httpx.HTTPStatusError("429", request=request, response=resp_429)
    ) is False
    assert _is_permanent_404(LookupError("CXS discovery failed")) is True
    assert _is_permanent_404(httpx.ConnectTimeout("timeout")) is False
    assert _is_permanent_404(httpx.ConnectError("dns")) is False
    assert _is_permanent_404(ValueError("parse error")) is False
    assert _is_permanent_404(RuntimeError("anything else")) is False


# ---------------------------------------------------------------------------
# P7-L (2026-05-08) — ats_apply_url fallback tests
#
# Why these matter: prior to P7-L, Stage 2b only fetched primary_url. Most
# jobright-sourced rows kept primary_url=https://jobright.ai/... even after
# paBackfillAtsUrlsBatch wrote a real employer URL into ats_apply_url. Result:
# 29,497 active rows starved at any given time. These tests pin the behaviour
# that prevents that regression.
# ---------------------------------------------------------------------------


def test_select_query_includes_ats_apply_url_column_and_predicate() -> None:
    """SELECT must return ats_apply_url and treat it as a fallback URL.

    Pin both: the column is in the SELECT list, and the WHERE predicate
    accepts rows where only ats_apply_url is non-jobright (so we don't
    starve when primary_url is the jobright aggregator URL).
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn([[]])
    run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    select_query = next(q for q, _ in conn.executed if q.lstrip().startswith("SELECT"))
    assert "ats_apply_url" in select_query, (
        "SELECT must return ats_apply_url so the loop can fall back to it"
    )
    # Predicate: at least one of (primary_url non-jobright) OR (ats_apply_url non-jobright)
    assert (
        "primary_url IS NOT NULL AND primary_url NOT LIKE 'https://jobright.ai/%%'"
        in select_query
    ), "primary_url must still be considered when it is a real employer URL"
    assert (
        "ats_apply_url IS NOT NULL AND ats_apply_url NOT LIKE 'https://jobright.ai/%%'"
        in select_query
    ), (
        "ats_apply_url must be considered as a fallback — without this, jobright-"
        "sourced rows with resolved ats_apply_url stay invisible to Stage 2b"
    )


def test_jobright_primary_url_falls_back_to_ats_apply_url(monkeypatch) -> None:
    """When primary_url is jobright-style, the loop must fetch ats_apply_url.

    This is the core P7-L unblocker: 5,389 rows already have a usable
    ats_apply_url and were unreachable before this change.
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "3" * 64,
                "company_name": "JobrightSourcedCo",
                "role_title": "SWE",
                "primary_url": "https://jobright.ai/jobs/info/abc?utm=Sales",
                "ats_apply_url": "https://boards.greenhouse.io/jobrightsourcedco/jobs/777",
            }
        ], []]
    )

    fetched_urls: list[str] = []

    def _capture_fetch(url: str):
        fetched_urls.append(url)
        return build_ats_job_data(
            source="greenhouse",
            description_plain="Resolved via ats_apply_url fallback.",
        )

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        _capture_fetch,
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["processed"] == 1
    assert stats["failed"] == 0
    assert fetched_urls == [
        "https://boards.greenhouse.io/jobrightsourcedco/jobs/777"
    ], (
        "When primary_url is jobright-aggregator-style, the loop must use "
        "ats_apply_url instead — fetching the jobright URL would just hit "
        "the aggregator and yield no JD."
    )


def test_real_primary_url_preferred_over_ats_apply_url(monkeypatch) -> None:
    """When primary_url is already a real employer URL, prefer it.

    Some senior-scraper rows write primary_url=<employer URL> directly.
    ats_apply_url may also be populated (cached). Prefer primary_url so we
    don't double-resolve.
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "4" * 64,
                "company_name": "DirectScrapedCo",
                "role_title": "EM",
                "primary_url": "https://boards.greenhouse.io/directco/jobs/100",
                "ats_apply_url": "https://other-cached.example.com/x",
            }
        ], []]
    )

    fetched_urls: list[str] = []

    def _capture_fetch(url: str):
        fetched_urls.append(url)
        return build_ats_job_data(source="greenhouse", description_plain="ok")

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        _capture_fetch,
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["processed"] == 1
    assert fetched_urls == ["https://boards.greenhouse.io/directco/jobs/100"], (
        "Real primary_url must be preferred over cached ats_apply_url"
    )


def test_no_fetchable_url_skips_without_burning_attempt() -> None:
    """If both primary_url and ats_apply_url are missing/jobright, skip.

    This is a defensive branch — the SELECT clause should already filter
    these out, but if a row slips through (e.g. concurrent paBackfill run
    cleared ats_apply_url between SELECT and loop), skip gracefully rather
    than crash or burn a bogus fetch attempt.
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            {
                "job_id": "5" * 64,
                "company_name": "BothJobrightCo",
                "role_title": "x",
                "primary_url": "https://jobright.ai/jobs/info/p",
                "ats_apply_url": "https://jobright.ai/jobs/info/p",
            }
        ], []]
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["processed"] == 0
    assert stats["skipped"] == 1
    assert stats["failed"] == 0
    # No UPDATE — we did not burn a write attempt
    update_params = [
        params for query, params in conn.executed if query.lstrip().startswith("UPDATE")
    ]
    assert update_params == []


def test_missing_ats_apply_url_key_does_not_raise(monkeypatch) -> None:
    """Real psycopg rows return dict-like objects; test fixtures may omit
    the column. row.get() handles both. Pin that with a fixture that omits
    ats_apply_url entirely.
    """
    from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment

    conn = _FakeConn(
        [[
            # Note: no "ats_apply_url" key at all
            {
                "job_id": "6" * 64,
                "company_name": "Acme",
                "role_title": "Backend Engineer",
                "primary_url": "https://boards.greenhouse.io/acme/jobs/123",
            }
        ], []]
    )

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        lambda url: build_ats_job_data(source="greenhouse", description_plain="ok"),
    )

    stats = run_jd_enrichment(
        conn=conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
    )

    assert stats["processed"] == 1
