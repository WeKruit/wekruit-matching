"""Tests for the Firestore <-> Postgres reconciliation gate (fix #5).

``firestore_reconcile`` compares the PG matchable set (the EXACT
``job_sync._fetch_active_jobs`` predicate) against the live Firestore
matching-jobs collection. It is injectable (``client_factory`` / ``project_id``
/ ``collection``) so these tests run without google-cloud-firestore or creds.
It NEVER raises on a Firestore read error — it degrades to ``skipped``.
"""

from __future__ import annotations

from wekruit_matching.pipeline import dead_backfill as db


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Returns a canned PG matchable count for the single COUNT(*) query."""

    def __init__(self, pg_matchable: int):
        self._pg = pg_matchable
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        return _FakeResult({"n": self._pg})


class _FakeAggResult:
    def __init__(self, value: int):
        self.value = value


class _FakeAgg:
    def __init__(self, value: int):
        self._value = value

    def get(self):
        return [[_FakeAggResult(self._value)]]


class _FakeQuery:
    def __init__(self, count: int):
        self._count = count

    def where(self, *a, **k):
        return self

    def count(self):
        return _FakeAgg(self._count)

    def stream(self):  # pragma: no cover - aggregation path preferred
        return iter([object()] * self._count)


class _FakeCollection:
    def __init__(self, *, total: int, active: int):
        self._total = total
        self._active = active

    def where(self, field, op, value):
        return _FakeQuery(self._active)

    def count(self):
        return _FakeAgg(self._total)

    def stream(self):  # pragma: no cover
        return iter([object()] * self._total)


class _FakeClient:
    def __init__(self, *, total: int, active: int):
        self._total = total
        self._active = active

    def collection(self, name):
        return _FakeCollection(total=self._total, active=self._active)


def test_reconcile_ok_when_counts_match():
    conn = _FakeConn(pg_matchable=100)
    out = db.firestore_reconcile(
        conn,
        threshold=0.05,
        project_id="proj",
        collection="matching-jobs",
        client_factory=lambda: _FakeClient(total=110, active=100),
    )
    assert out["skipped"] is False
    assert out["ok"] is True
    assert out["pg_matchable"] == 100
    assert out["fs_active"] == 100
    assert out["fs_total"] == 110
    assert out["divergence"] == 0.0


def test_reconcile_degraded_on_real_divergence():
    conn = _FakeConn(pg_matchable=100)
    out = db.firestore_reconcile(
        conn,
        threshold=0.05,
        project_id="proj",
        collection="matching-jobs",
        client_factory=lambda: _FakeClient(total=140, active=130),
    )
    assert out["skipped"] is False
    assert out["ok"] is False
    assert out["pg_matchable"] == 100
    assert out["fs_active"] == 130
    assert abs(out["divergence"] - 0.30) < 1e-9
    assert "diverge" in out["reason"]


def test_reconcile_small_divergence_is_ok():
    conn = _FakeConn(pg_matchable=100)
    out = db.firestore_reconcile(
        conn,
        threshold=0.05,
        project_id="proj",
        collection="matching-jobs",
        client_factory=lambda: _FakeClient(total=110, active=104),
    )
    assert out["ok"] is True
    assert out["skipped"] is False


def test_reconcile_skips_when_client_unavailable():
    conn = _FakeConn(pg_matchable=42)
    out = db.firestore_reconcile(
        conn,
        threshold=0.05,
        project_id="proj",
        collection="matching-jobs",
        client_factory=lambda: None,
    )
    assert out["skipped"] is True
    assert out["ok"] is True
    assert out["pg_matchable"] == 42
    assert out["fs_active"] == 0


def test_reconcile_skips_on_firestore_read_error():
    conn = _FakeConn(pg_matchable=50)

    class _BoomClient:
        def collection(self, name):
            raise RuntimeError("firestore unavailable")

    out = db.firestore_reconcile(
        conn,
        threshold=0.05,
        project_id="proj",
        collection="matching-jobs",
        client_factory=lambda: _BoomClient(),
    )
    assert out["skipped"] is True
    assert out["ok"] is True
    assert out["pg_matchable"] == 50
    assert "error" in out["reason"]


def test_reconcile_pg_matchable_predicate_runs():
    conn = _FakeConn(pg_matchable=7)
    db.firestore_reconcile(
        conn,
        project_id="proj",
        collection="matching-jobs",
        client_factory=lambda: None,
    )
    assert any("count(*)" in q.lower() for q in conn.queries)
    joined = " ".join(conn.queries).lower()
    assert "embedding is not null" in joined
    assert "cardinality(required_skills) > 0" in joined
