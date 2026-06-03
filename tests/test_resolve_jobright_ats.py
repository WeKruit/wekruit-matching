"""Unit tests for scripts/resolve_jobright_ats.py (Fix #2A + Fix #4 Option B).

No network and no live DB: Serper is replaced by stubbing the module-level
``_serper``/``_best_url`` helpers (mirroring how test_pipeline_job_sync.py
monkeypatches ``httpx.post``), and the DB is a fake psycopg3-style connection
that records every ``(sql, params)`` executed against its cursor.

Covered:
  (a) a RESOLVED row's _flush UPDATE sets ats_apply_url, a bumped content_hash,
      AND embedded_at=now() (Fix #4 Option B — so the embedded_at-keyed Stage 4
      sync watermark re-selects the row).
  (b) a MISS is stamped jd_fetch_source='serper_miss' and is NOT given
      embedded_at.
  (c) idempotency: the SELECT excludes already-resolved (ats_apply_url set) and
      previously-missed (serper_miss) rows; every UPDATE carries the
      "(ats_apply_url IS NULL OR ats_apply_url='')" guard so a re-run is a no-op.
  (d) resolve_jobright_pending returns the {'resolved','missed','skipped',
      'errors'} counts dict; the missing-key path raises RuntimeError.
"""
from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_resolver():
    """Import scripts/resolve_jobright_ats.py by path (it is not a package)."""
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "resolve_jobright_ats.py"
    spec = importlib.util.spec_from_file_location("resolve_jobright_ats", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCursor:
    def __init__(self, sink: list[tuple[str, dict | None]]):
        self._sink = sink

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql: str, params: dict | None = None) -> None:
        self._sink.append((sql, params))


class _FakeConn:
    """psycopg3-style fake: ``conn.execute(...).fetchall()`` for the SELECT,
    ``with conn.cursor() as cur: cur.execute(sql, params)`` for writes."""

    def __init__(self, *, select_rows: list[dict]):
        self.select_rows = select_rows
        self.cursor_calls: list[tuple[str, dict | None]] = []
        self.select_calls: list[str] = []
        self.committed = 0

    def execute(self, sql: str, params: dict | None = None) -> "_FakeResult":
        self.select_calls.append(sql)
        return _FakeResult(self.select_rows)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.cursor_calls)

    def commit(self) -> None:
        self.committed += 1


class _FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows


@contextmanager
def _fake_get_connection_factory(conn: _FakeConn):
    @contextmanager
    def _cm():
        yield conn

    yield _cm


def _patch_common(monkeypatch, module, *, conn: _FakeConn, serper_key: str = "k" * 12) -> None:
    """Patch settings (serper key present), get_connection, and disable network."""
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(serper_api_key=serper_key))

    @contextmanager
    def _fake_get_connection():
        yield conn

    monkeypatch.setattr(module, "get_connection", _fake_get_connection)
    # No network: _serper must never actually hit Serper. Default to empty;
    # individual tests override _best_url to decide resolve vs miss per job_id.
    monkeypatch.setattr(module, "_serper", lambda *a, **k: [{"link": "stub"}])


def test_resolved_flush_sets_url_bumped_hash_and_embedded_at(monkeypatch) -> None:
    """(a) RESOLVED row UPDATE includes ats_apply_url, content_hash, embedded_at=now()."""
    module = _load_resolver()
    resolved_url = "https://boards.greenhouse.io/acme/jobs/123"
    conn = _FakeConn(
        select_rows=[{"job_id": "job-1", "company_name": "Acme", "role_title": "SWE Intern"}]
    )
    _patch_common(monkeypatch, module, conn=conn)
    # Force a successful resolve.
    monkeypatch.setattr(module, "_best_url", lambda organic, client, verify: (resolved_url, "serper"))

    counts = module.resolve_jobright_pending(limit=10, workers=1, dry_run=False, verify=False)

    assert counts["resolved"] == 1
    assert counts["missed"] == 0

    # Exactly one UPDATE issued against the cursor; it is the resolved-row write.
    assert len(conn.cursor_calls) == 1
    sql, params = conn.cursor_calls[0]
    assert "ats_apply_url = %(u)s" in sql
    # 2026-06-03: the resolver no longer stamps a jd_fetch_source on a hit —
    # resolving an apply URL is not a JD fetch, and a non-sentinel source on a
    # thin JD violates ck_jd_source_requires_usable_jd (alembic 0010).
    assert "jd_fetch_source" not in sql
    assert "content_hash = %(ch)s" in sql
    # embedded_at is bumped only when the row is already embedded (guards
    # ck_embedded_requires_vector on unembedded rows).
    assert "embedded_at = CASE WHEN embedding IS NOT NULL THEN now()" in sql
    # Idempotency guard present.
    assert "(ats_apply_url IS NULL OR ats_apply_url='')" in sql
    assert params["u"] == resolved_url
    assert params["j"] == "job-1"
    # content_hash is bumped to include the resolved URL (not the bare company|role hash).
    import hashlib

    assert params["ch"] == hashlib.sha256(b"job-1|" + resolved_url.encode()).hexdigest()
    assert conn.committed >= 1


def test_miss_is_stamped_skip_no_url_without_embedded_at(monkeypatch) -> None:
    """(b) A miss -> jd_fetch_source='skip_no_url' (constraint-legal sentinel),
    NO embedded_at bump. (Was 'serper_miss' until 2026-06-03 — that value is not
    in the 0010 allow-list and crashed Stage 2.5 on thin-JD rows.)"""
    module = _load_resolver()
    conn = _FakeConn(
        select_rows=[{"job_id": "job-miss", "company_name": "Acme", "role_title": "SWE Intern"}]
    )
    _patch_common(monkeypatch, module, conn=conn)
    # Force a miss.
    monkeypatch.setattr(module, "_best_url", lambda organic, client, verify: (None, "none"))

    counts = module.resolve_jobright_pending(limit=10, workers=1, dry_run=False, verify=False)

    assert counts["missed"] == 1
    assert counts["resolved"] == 0

    assert len(conn.cursor_calls) == 1
    sql, params = conn.cursor_calls[0]
    assert "jd_fetch_source = 'skip_no_url'" in sql
    assert "embedded_at" not in sql  # misses must NOT touch embedded_at
    assert "ats_apply_url = " not in sql  # misses must not set a URL
    assert params["j"] == "job-miss"


def test_select_excludes_already_resolved_and_prior_misses(monkeypatch) -> None:
    """(c) Idempotency at the SELECT layer + per-UPDATE WHERE guard.

    The pending SELECT must filter out rows that already have an ats_apply_url
    and rows previously stamped serper_miss, so a re-run does not re-resolve
    them. With zero pending rows the function performs no writes.
    """
    module = _load_resolver()
    conn = _FakeConn(select_rows=[])  # nothing pending => already-resolved/missed excluded
    _patch_common(monkeypatch, module, conn=conn)
    monkeypatch.setattr(module, "_best_url", lambda organic, client, verify: (None, "none"))

    counts = module.resolve_jobright_pending(limit=10, workers=1, dry_run=False, verify=False)

    assert counts == {"resolved": 0, "missed": 0, "skipped": 0, "errors": 0}
    # No cursor writes when there is nothing pending.
    assert conn.cursor_calls == []

    # The SELECT itself encodes the idempotency predicates.
    assert conn.select_calls, "expected a pending SELECT to be issued"
    select_sql = conn.select_calls[0]
    assert "ats_apply_url IS NULL OR ats_apply_url=''" in select_sql
    assert "jd_fetch_source NOT IN ('skip_no_url', 'serper_miss')" in select_sql


def test_resolve_jobright_pending_returns_counts_dict(monkeypatch) -> None:
    """(d) resolve_jobright_pending returns the {'resolved','missed','skipped','errors'} dict."""
    module = _load_resolver()
    conn = _FakeConn(
        select_rows=[
            {"job_id": "job-hit", "company_name": "Acme", "role_title": "SWE Intern"},
            {"job_id": "job-no", "company_name": "Beta", "role_title": "Data Intern"},
        ]
    )
    _patch_common(monkeypatch, module, conn=conn)

    # job-hit resolves, job-no misses (decide by company in the row's query string
    # would be brittle; instead decide by a counter on call order).
    def _best_url_alternating(organic, client, verify):
        # First call (job-hit) resolves; remaining miss. Use a mutable default.
        _best_url_alternating.calls += 1
        if _best_url_alternating.calls == 1:
            return "https://jobs.lever.co/acme/abc", "serper"
        return None, "none"

    _best_url_alternating.calls = 0
    monkeypatch.setattr(module, "_best_url", _best_url_alternating)

    counts = module.resolve_jobright_pending(limit=10, workers=1, dry_run=False, verify=False)

    assert set(counts.keys()) == {"resolved", "missed", "skipped", "errors"}
    assert counts["resolved"] == 1
    assert counts["missed"] == 1
    assert counts["skipped"] == 0
    assert counts["errors"] == 0


def test_missing_serper_key_raises_runtimeerror(monkeypatch) -> None:
    """The missing-key path must raise RuntimeError (main() maps it to exit 2)."""
    module = _load_resolver()
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(serper_api_key=""))

    with pytest.raises(RuntimeError, match="serper_api_key not configured"):
        module.resolve_jobright_pending(limit=1, workers=1, dry_run=True, verify=False)


def test_dry_run_performs_no_writes(monkeypatch) -> None:
    """Bonus safety: --dry-run resolves but never writes to the DB."""
    module = _load_resolver()
    conn = _FakeConn(
        select_rows=[{"job_id": "job-1", "company_name": "Acme", "role_title": "SWE Intern"}]
    )
    _patch_common(monkeypatch, module, conn=conn)
    monkeypatch.setattr(
        module, "_best_url", lambda organic, client, verify: ("https://x.example/apply", "serper")
    )

    counts = module.resolve_jobright_pending(limit=10, workers=1, dry_run=True, verify=False)

    assert counts["resolved"] == 1
    assert conn.cursor_calls == []  # zero writes in dry-run
