"""Unit tests for the JD enrichment orchestrator."""
from __future__ import annotations

from types import SimpleNamespace

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
