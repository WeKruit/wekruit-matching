"""Render-level tests for the internal jobs console UI."""
from contextlib import contextmanager

import wekruit_matching.api.internal_ui as internal_ui


class FakeResult:
    """Minimal DB result wrapper for fetchone/fetchall."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    """Dispatch canned responses by query fragment."""

    def __init__(self, handlers: dict[str, list[dict]]):
        self.handlers = handlers

    def execute(self, query: str, params=None):
        normalized = " ".join(str(query).split())
        for needle, rows in self.handlers.items():
            if needle in normalized:
                return FakeResult(rows)
        raise AssertionError(f"Unexpected query: {normalized} with params={params}")


@contextmanager
def fake_connection_ctx(handlers: dict[str, list[dict]]):
    """Context manager matching get_connection()."""
    yield FakeConnection(handlers)


def patch_jobs_queries(monkeypatch):
    """Patch job page queries and filter options."""
    handlers = {
        "SELECT COUNT(*) AS total FROM jobs WHERE": [{"total": 52}],
        "SELECT job_id, source_repo, company_name, role_title, primary_url,": [
            {
                "job_id": "job-1",
                "source_repo": "Summer2026-Internships",
                "company_name": "Acme",
                "role_title": "ML Intern",
                "primary_url": "https://example.com/jobs/1",
                "location_raw": "Remote",
                "industry": "software",
                "company_size": "startup",
                "required_skills": ["Python", "SQL", "ML"],
                "sponsorship": True,
                "status": "active",
                "first_seen_at": "2026-03-29 09:00:00",
                "last_seen_at": "2026-03-31 09:00:00",
                "enriched_at": None,
                "embedded_at": None,
            },
            {
                "job_id": "job-2",
                "source_repo": "New-Grad-Positions",
                "company_name": "Globex",
                "role_title": "Data Engineer",
                "primary_url": "https://example.com/jobs/2",
                "location_raw": "Austin, TX",
                "industry": "unknown",
                "company_size": "enterprise",
                "required_skills": [],
                "sponsorship": False,
                "status": "active",
                "first_seen_at": "2026-03-28 09:00:00",
                "last_seen_at": "2026-03-30 09:00:00",
                "enriched_at": "2026-03-30 12:00:00",
                "embedded_at": "2026-03-30 12:05:00",
            },
        ],
        "SELECT DISTINCT source_repo FROM jobs ORDER BY source_repo": [
            {"source_repo": "New-Grad-Positions"},
            {"source_repo": "Summer2026-Internships"},
        ],
        "SELECT DISTINCT industry FROM jobs WHERE industry IS NOT NULL ORDER BY industry": [
            {"industry": "software"},
            {"industry": "unknown"},
        ],
    }
    monkeypatch.setattr(internal_ui, "get_connection", lambda: fake_connection_ctx(handlers))
    internal_ui._filter_cache.update({"ts": 0, "sources": [], "industries": []})


def patch_stats_queries(monkeypatch):
    """Patch stats page queries."""
    handlers = {
        "COUNT(*) AS total,": [
            {
                "total": 120,
                "active": 95,
                "inactive": 25,
                "enriched": 80,
                "embedded": 70,
            }
        ],
        "SELECT source_repo, status, COUNT(*) AS count FROM jobs": [
            {"source_repo": "New-Grad-Positions", "status": "active", "count": 40},
            {"source_repo": "New-Grad-Positions", "status": "inactive", "count": 7},
            {"source_repo": "Summer2026-Internships", "status": "active", "count": 55},
            {"source_repo": "Summer2026-Internships", "status": "inactive", "count": 18},
        ],
        "SELECT industry, COUNT(*) AS count FROM jobs": [
            {"industry": "software", "count": 60},
            {"industry": "fintech", "count": 18},
        ],
        "SELECT DATE(first_seen_at) AS day, COUNT(*) AS count": [
            {"day": "2026-03-31", "count": 11},
            {"day": "2026-03-30", "count": 9},
        ],
    }
    monkeypatch.setattr(internal_ui, "get_connection", lambda: fake_connection_ctx(handlers))


def patch_pipeline_queries(monkeypatch):
    """Patch pipeline page query."""
    handlers = {
        "COUNT(*) FILTER ( WHERE enriched_at IS NULL AND status = 'active' ) AS pending_enrich,": [
            {
                "pending_enrich": 12,
                "pending_embed": 4,
                "last_scrape": "2026-03-31 06:00:00",
                "last_enriched": "2026-03-31 06:10:00",
                "last_embedded": "2026-03-31 06:20:00",
            }
        ]
    }
    monkeypatch.setattr(internal_ui, "get_connection", lambda: fake_connection_ctx(handlers))


def test_jobs_browser_renders_shared_shell_and_filters(monkeypatch):
    """Jobs page has shared shell, page heading, labels, and encoded pagination URLs."""
    patch_jobs_queries(monkeypatch)

    html = internal_ui.jobs_browser(
        page=1,
        status="active",
        source="Summer2026-Internships",
        industry="software",
        q="ml intern",
    )

    assert 'Skip to content' in html
    assert '<h1 class="page-title">Active Jobs</h1>' in html
    assert 'aria-current="page">Active Jobs</a>' in html
    assert '<label for="q">Search</label>' in html
    assert '<label for="status-select">Listing status</label>' in html
    assert 'surface-pill">internal</span>' in html
    assert 'href="/internal/jobs?page=2&amp;status=active&amp;source=Summer2026-Internships' in html
    assert 'q=ml+intern' in html


def test_jobs_browser_uses_text_status_and_mobile_cards(monkeypatch):
    """Jobs page exposes text-based state and a mobile card layout."""
    patch_jobs_queries(monkeypatch)

    html = internal_ui.jobs_browser(page=1, status="active", source="", industry="", q="")

    assert 'class="job-list-mobile"' in html
    assert "Sponsorship supported" in html
    assert "Pending enrichment" in html
    assert "Awaiting enrichment" in html
    assert "No sponsorship" in html
    assert "Embedded" in html
    assert "Industry unknown" in html


def test_jobs_browser_uses_real_disabled_pagination(monkeypatch):
    """Boundary pagination uses spans with aria-disabled, not dead links."""
    patch_jobs_queries(monkeypatch)

    html = internal_ui.jobs_browser(page=1, status="active", source="", industry="", q="")

    assert '<span class="pagination-link is-disabled" aria-disabled="true">Prev</span>' in html
    assert 'aria-current="page">1</span>' in html


def test_stats_dashboard_renders_consistent_sections(monkeypatch):
    """Stats page keeps the shared hierarchy and section naming."""
    patch_stats_queries(monkeypatch)

    html = internal_ui.stats_dashboard()

    assert '<h1 class="page-title">Stats</h1>' in html
    assert "Inventory at a glance" in html
    assert "Source mix" in html
    assert "Top industries" in html
    assert "Recent intake" in html
    assert "95" in html
    assert "70 embedded" in html


def test_pipeline_status_uses_friendly_operational_copy(monkeypatch):
    """Pipeline page explains processing state in product language."""
    patch_pipeline_queries(monkeypatch)

    html = internal_ui.pipeline_status()

    assert '<h1 class="page-title">Pipeline</h1>' in html
    assert "Processing backlog" in html
    assert "Jobs waiting for metadata" in html
    assert "Recent pipeline activity" in html
    assert "Latest time the source inventory was refreshed." in html
    assert "/tmp/matching-daily-update.log" in html
