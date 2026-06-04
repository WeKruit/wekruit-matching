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

    assert counts == {
        "resolved": 0, "missed": 0, "skipped": 0, "errors": 0, "aborted": 0,
        "infra_error": 0, "infra_detail": "",
    }
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

    # Counts dict now also carries outage signals (aborted / infra_error /
    # infra_detail) so daily.py can distinguish a DOWN Serper from genuine misses.
    assert set(counts.keys()) == {
        "resolved", "missed", "skipped", "errors", "aborted",
        "infra_error", "infra_detail",
    }
    assert counts["resolved"] == 1
    assert counts["missed"] == 1
    assert counts["skipped"] == 0
    assert counts["errors"] == 0
    # Healthy run: breaker never tripped.
    assert counts["aborted"] == 0
    assert counts["infra_error"] == 0
    assert counts["infra_detail"] == ""


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


# ---------------------------------------------------------------------------
# Infra-vs-miss distinction (2026-06-04). A dead Serper (out of credits) went
# unnoticed for days because _serper swallowed the 400 as an empty result and
# the run stamped status=ok with zero alerts. These tests lock in: an infra
# failure RAISES (is not a miss), the run ABORTS without poisoning rows, and the
# outage signal is surfaced for alerting.
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status: int, body: str = "", organic=None):
        self.status_code = status
        self.text = body
        self._organic = organic or []

    def json(self) -> dict:
        return {"organic": self._organic}


class _FakeHttpClient:
    def __init__(self, resp: _FakeResp):
        self._resp = resp
        self.calls = 0

    def post(self, *a, **k) -> _FakeResp:
        self.calls += 1
        return self._resp


@pytest.mark.parametrize("status", [400, 401, 402, 403])
def test_serper_raises_infra_on_auth_credit_status(status: int) -> None:
    """A 400/401/402/403 means the dependency is DOWN (credit/auth/quota), not
    'no results' — _serper must raise SerperInfraError, not return []."""
    module = _load_resolver()
    body = '{"message":"Not enough credits","statusCode":400}' if status == 400 else f"err {status}"
    client = _FakeHttpClient(_FakeResp(status, body=body))
    with pytest.raises(module.SerperInfraError) as ei:
        module._serper(client, "key", "q")
    assert ei.value.status_code == status
    # Futile statuses are NOT retried (would just burn time against a down dep).
    assert client.calls == 1
    if status == 400:
        assert "credits" in str(ei.value).lower()


def test_serper_returns_empty_list_on_genuine_200_no_results() -> None:
    """A 200 with an empty organic list is a GENUINE miss — return [], do not raise."""
    module = _load_resolver()
    client = _FakeHttpClient(_FakeResp(200, organic=[]))
    assert module._serper(client, "key", "q") == []
    assert client.calls == 1


def test_serper_returns_organic_on_200_hit() -> None:
    module = _load_resolver()
    organic = [{"link": "https://boards.greenhouse.io/acme/jobs/1"}]
    client = _FakeHttpClient(_FakeResp(200, organic=organic))
    assert module._serper(client, "key", "q") == organic


def test_infra_error_aborts_run_without_poisoning_rows(monkeypatch) -> None:
    """When Serper is down, the run aborts: un-queried rows are NOT stamped
    skip_no_url (so they retry after credits refill), and infra_error/infra_detail
    are surfaced so daily.py can alert a human."""
    module = _load_resolver()
    conn = _FakeConn(
        select_rows=[
            {"job_id": "j1", "company_name": "Acme", "role_title": "SWE"},
            {"job_id": "j2", "company_name": "Beta", "role_title": "Data"},
        ]
    )
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(serper_api_key="k" * 12))

    @contextmanager
    def _fake_get_connection():
        yield conn

    monkeypatch.setattr(module, "get_connection", _fake_get_connection)

    def _boom(*a, **k):
        raise module.SerperInfraError("HTTP 400: Not enough credits", status_code=400)

    monkeypatch.setattr(module, "_serper", _boom)

    counts = module.resolve_jobright_pending(limit=10, workers=1, dry_run=False, verify=False)

    assert counts["infra_error"] == 1
    assert "credits" in str(counts["infra_detail"]).lower()
    assert counts["aborted"] == 2
    assert counts["missed"] == 0
    assert counts["resolved"] == 0
    # CRITICAL: no skip_no_url poison written for rows that were never queried.
    poison = [sql for (sql, _params) in conn.cursor_calls if "skip_no_url" in sql]
    assert poison == [], conn.cursor_calls
