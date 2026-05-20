"""Track D — embedding/sync gating contract tests.

Matching-quality launch blocker (2026-05-20): even when the JD enrichment
stage fails gracefully, ``enriched_at`` gets stamped. Without an explicit
gate on JD body length + skills cardinality, the embedding worker would
produce a near-useless title-only embedding for those rows, and the
Firestore sync would copy them into the active matching pool. Track D
pins the gate at two layers:

  1. ``embedding/worker.py::embed_pending`` — SELECT excludes rows whose
     enrichment didn't actually populate JD + skills.
  2. ``pipeline/job_sync.py::_fetch_active_jobs`` — SELECT excludes rows
     missing JD + skills even if a stale embedding exists from before the
     worker gate landed (belt + suspenders).

These tests are intentionally SQL-string assertions rather than DB
integration tests: the DB-backed embedding/sync tests already skip when
``DATABASE_URL`` is unset, so they can't pin the contract in CI. The
string check is brittle in proportion to the change it's pinning — if the
SQL is rewritten the test must be updated to match the new clauses.
"""
from __future__ import annotations


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Captures SQL queries without executing them.

    The embedding worker calls ``register_vector`` on the conn before its
    SELECT — we stub that path on import via monkeypatch in the test.
    """

    def __init__(self):
        self.executed: list[str] = []

    def execute(self, query: str, params=None):  # noqa: ARG002 — params unused
        self.executed.append(query)
        if query.lstrip().startswith("SELECT"):
            return _FakeResult([])
        return _FakeResult([])

    def commit(self):
        pass


def test_embed_pending_select_gates_on_jd_and_skills(monkeypatch) -> None:
    """The embedding worker must only embed rows with real JD + skills."""
    # Stub pgvector register_vector — it tries to read pg_type from a real conn.
    monkeypatch.setattr(
        "wekruit_matching.embedding.worker.register_vector",
        lambda _conn: None,
    )

    from wekruit_matching.embedding.worker import embed_pending

    conn = _FakeConn()
    embed_pending(conn)

    select = next(q for q in conn.executed if q.lstrip().startswith("SELECT"))
    assert "job_description IS NOT NULL" in select, (
        "embed_pending must skip rows with NULL job_description — without "
        "this, jobright-only docs whose JD enrichment failed silently still "
        "get a title-only embedding and ride into the matching pool"
    )
    assert "length(job_description) >= 200" in select, (
        "200-char minimum guards against rows whose JD is a heading-only "
        "stub (e.g. just a role title scraped from a 404 redirect page)"
    )
    assert "required_skills IS NOT NULL" in select, (
        "embed_pending must skip rows with NULL required_skills"
    )
    assert "cardinality(required_skills) > 0" in select, (
        "Empty-skills rows would embed to '{title} at {company}. Skills: ' "
        "which is functionally a title-only embedding"
    )


def test_sync_active_select_gates_on_jd_and_skills(monkeypatch) -> None:
    """The Firestore sync must not copy rows missing JD or skills.

    Belt-and-suspenders with the embedding worker gate: a stale embedding
    left over from before the worker gate landed could otherwise ride into
    Firestore active on the next sync.
    """
    from wekruit_matching.pipeline import job_sync

    captured: dict[str, str] = {}

    class _Conn:
        def execute(self, query: str, params=None):  # noqa: ARG002
            if query.lstrip().startswith("SELECT") and "WHERE status = 'active'" in query:
                captured["active_sql"] = query
            return _FakeResult([])

    job_sync._fetch_active_jobs(_Conn(), since=None)

    sql = captured.get("active_sql", "")
    assert "job_description IS NOT NULL" in sql, (
        "active sync must exclude rows with NULL job_description"
    )
    assert "length(job_description) >= 200" in sql, (
        "active sync must require a meaningful JD body"
    )
    assert "required_skills IS NOT NULL" in sql, (
        "active sync must exclude rows with NULL required_skills"
    )
    assert "cardinality(required_skills) > 0" in sql, (
        "active sync must exclude rows with empty required_skills"
    )
