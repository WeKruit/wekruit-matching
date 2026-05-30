"""Unit tests for Phase 21 job sync to Firebase."""
from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx


def _dt(hour: int) -> datetime:
    return datetime(2026, 4, 1, hour, 0, tzinfo=UTC)


def _job_row(*, job_id: str, status: str, content_hash: str) -> dict:
    return {
        "job_id": job_id,
        "source_repo": "Summer2026-Internships",
        "company_name": "Acme",
        "role_title": "Software Engineer Intern",
        "primary_url": f"https://jobs.example/{job_id}",
        "location_raw": "Remote",
        "date_posted_raw": "1d",
        "status": status,
        "content_hash": content_hash,
        "job_description": "Build product features",
        "core_responsibilities": ["Ship code"],
        "salary_range": "$100k-$120k",
        "seniority_level": "entry",
        "benefits": ["Health"],
        "qualifications": ["Python"],
        "industry": "Software",
        "company_size": "startup",
        "required_skills": ["Python", "SQL"],
        "sponsorship": True,
        "embedding": [0.1, 0.2, 0.3],
        "embedding_model": "text-embedding-3-small",
        "first_seen_at": _dt(1),
        "last_seen_at": _dt(2),
        "enriched_at": _dt(3),
        "embedded_at": _dt(4),
    }


class _FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows


class _FakeConn:
    def __init__(self, *, active_rows: list[dict], inactive_rows: list[dict]):
        self.active_rows = active_rows
        self.inactive_rows = inactive_rows
        self.calls: list[tuple[str, dict | None]] = []
        self.recorded_hashes: list[str] = []

    def execute(self, sql: str, params: dict | None = None) -> _FakeResult:
        self.calls.append((sql, params))
        lowered = sql.lower()
        # The active SELECT is checked FIRST: it LEFT JOINs pipeline_synced_hashes
        # (fix #4), so a naive "pipeline_synced_hashes in sql" branch would
        # shadow it. Match on the aliased predicate the active SELECT carries.
        if "j.status = 'active'" in lowered:
            rows = self.active_rows
            if params and "offset" in params:
                rows = rows[params["offset"] :]
            if params and "limit" in params:
                rows = rows[: params["limit"]]
            return _FakeResult(rows)
        if "status = 'inactive'" in lowered:
            return _FakeResult(self.inactive_rows)
        # Durable sync-watermark statements: ensure-table / read / upsert.
        # This fake has no stored watermark, so reads return empty and the
        # effective ``since`` is unchanged (preserving legacy assertions).
        if "pipeline_sync_state" in lowered:
            return _FakeResult([])
        # Fix #4 content_hash ledger: CREATE TABLE IF NOT EXISTS + per-job
        # INSERT ... ON CONFLICT. Record the upserted job_id; return empty.
        if "pipeline_synced_hashes" in lowered:
            if "insert" in lowered and params and "j" in params:
                self.recorded_hashes.append(params["j"])
            return _FakeResult([])
        raise AssertionError(f"Unexpected SQL: {sql}")


def _load_sync_jobs_bulk_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "sync_jobs_bulk.py"
    spec = importlib.util.spec_from_file_location("sync_jobs_bulk", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _WatermarkConn:
    """Fake connection that also models the durable sync watermark table.

    Routes:
      * matchable SELECT   -> active_rows filtered by ``embedded_at >= since``
        (mirrors the real ``embedded_at >= %(since)s`` predicate so the
        resume-from-watermark behaviour is observable).
      * inactive SELECT    -> inactive_rows
      * watermark SELECT    -> current stored watermark (one row or empty)
      * watermark UPSERT    -> records the advanced watermark value
      * ensure-table DDL    -> no-op
    """

    def __init__(
        self,
        *,
        active_rows: list[dict],
        inactive_rows: list[dict],
        stored_watermark: datetime | None,
    ):
        self.active_rows = active_rows
        self.inactive_rows = inactive_rows
        self.stored_watermark = stored_watermark
        self.calls: list[tuple[str, dict | None]] = []
        self.committed = 0
        self.recorded_hashes: list[str] = []
        # job_ids already recorded as synced-with-current-hash. Empty by
        # default so the fix #4 OR-clause includes every row (worst case);
        # tests can pre-seed it to model an already-synced row.
        self.synced_ledger: set[str] = set()

    def execute(self, sql: str, params: dict | None = None) -> _FakeResult:
        self.calls.append((sql, params))
        lowered = sql.lower()
        # The active SELECT is checked FIRST: it LEFT JOINs pipeline_synced_hashes
        # (fix #4), so a naive "pipeline_synced_hashes in sql" branch would
        # shadow it. Match on the aliased predicate the active SELECT carries.
        if "j.status = 'active'" in lowered:
            rows = self.active_rows
            since = params.get("since") if params else None
            if since is not None:
                # Mirror the real fix #4 predicate: embedded_at window OR a
                # content_hash differing from the ledger. When the SELECT
                # carries the content_hash clause, widen accordingly.
                if "content_hash is distinct from" in lowered:
                    rows = [
                        r
                        for r in rows
                        if r["embedded_at"] >= since
                        or r.get("job_id") not in self.synced_ledger
                    ]
                else:
                    rows = [r for r in rows if r["embedded_at"] >= since]
            return _FakeResult(rows)
        if "j.status = 'inactive'" in lowered or "where status = 'inactive'" in lowered:
            return _FakeResult(self.inactive_rows)
        # Durable sync-watermark statements (ensure-table / read / upsert).
        if "pipeline_sync_state" in lowered:
            if "create table" in lowered:
                return _FakeResult([])
            if lowered.strip().startswith("select"):
                if self.stored_watermark is None:
                    return _FakeResult([])
                return _FakeResult([{"watermark": self.stored_watermark}])
            # INSERT ... ON CONFLICT (advance)
            if params and "watermark" in params:
                self.stored_watermark = params["watermark"]
            return _FakeResult([])
        # Fix #4 content_hash ledger: CREATE TABLE / per-job INSERT ON CONFLICT.
        if "pipeline_synced_hashes" in lowered:
            if "insert" in lowered and params and "j" in params:
                self.recorded_hashes.append(params["j"])
            return _FakeResult([])
        raise AssertionError(f"Unexpected SQL: {sql}")

    def commit(self) -> None:
        self.committed += 1


def _patch_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        "wekruit_matching.pipeline.job_sync.get_settings",
        lambda: SimpleNamespace(
            firebase_sync_url="https://firebase-sync.example/api/sync/jobs",
            firebase_sync_api_key="sync-secret",
            firebase_sync_batch_size=50,
            firebase_sync_timeout_seconds=15.0,
            firebase_sync_collection="matching-jobs",
        ),
    )


def test_incremental_sync_resumes_from_durable_watermark_not_run_start(monkeypatch) -> None:
    """Regression: a job embedded BEFORE this run's ``since`` but AFTER the last
    successfully-synced watermark must still be selected.

    Root cause (live data, 2026-05-29): daily.py passes ``since=run_started_at``.
    If a prior run embedded a job and then the Firestore push partially failed,
    that job's ``embedded_at`` is in the past; the next run's ``since`` has moved
    forward, so the job is *silently* never re-synced and the live matcher never
    sees it. Resuming from the durable watermark (advanced only on success)
    recovers it. Idempotent upserts make the overlapping re-send safe.
    """
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    # Job embedded at hour 2 — older than this run's since (hour 5) but newer
    # than the last successful watermark (hour 1). It MUST be re-synced.
    dropped = _job_row(job_id="job-dropped", status="active", content_hash="d" * 64)
    dropped["embedded_at"] = _dt(2)
    fresh = _job_row(job_id="job-fresh", status="active", content_hash="f" * 64)
    fresh["embedded_at"] = _dt(6)

    conn = _WatermarkConn(
        active_rows=[dropped, fresh],
        inactive_rows=[],
        stored_watermark=_dt(1),
    )
    pushed_ids: list[str] = []

    @contextmanager
    def _fake_get_connection():
        yield conn

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: float):
        for job in json["jobs"]:
            pushed_ids.append(job["job_id"])

        class _Response:
            is_success = True

            def raise_for_status(self) -> None:
                return None

        return _Response()

    _patch_settings(monkeypatch)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.get_connection", _fake_get_connection)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.httpx.post", _fake_post)

    # Caller passes run-start (hour 5); without the watermark the dropped job is lost.
    sync_jobs_to_firebase(since=_dt(5), full_sync=False)

    assert "job-dropped" in pushed_ids, (
        "job embedded after the last successful watermark but before run-start "
        "was silently dropped from the Firestore sync"
    )
    assert "job-fresh" in pushed_ids
    # Watermark must advance to the max embedded_at that was successfully synced.
    assert conn.stored_watermark == _dt(6)


def test_incremental_sync_does_not_advance_watermark_when_push_fails(monkeypatch) -> None:
    """If the Firestore push fails, the durable watermark must NOT advance, so
    the next run re-covers the same window (self-healing retry)."""
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    job = _job_row(job_id="job-1", status="active", content_hash="a" * 64)
    job["embedded_at"] = _dt(6)
    conn = _WatermarkConn(active_rows=[job], inactive_rows=[], stored_watermark=_dt(1))

    @contextmanager
    def _fake_get_connection():
        yield conn

    def _failing_post(url: str, *, headers: dict, json: dict, timeout: float):
        class _Response:
            status_code = 500
            text = '{"ok":false,"error":"boom"}'
            is_success = False

            def raise_for_status(self) -> None:
                raise httpx.HTTPStatusError(
                    "boom",
                    request=httpx.Request("POST", url),
                    response=httpx.Response(500, text=self.text),
                )

        return _Response()

    _patch_settings(monkeypatch)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.get_connection", _fake_get_connection)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.httpx.post", _failing_post)

    import pytest

    with pytest.raises(httpx.HTTPStatusError):
        sync_jobs_to_firebase(since=_dt(5), full_sync=False)

    # Watermark must stay at its pre-run value so the failed window is retried.
    assert conn.stored_watermark == _dt(1)


def test_sync_jobs_to_firebase_posts_batched_payload_with_content_hash(monkeypatch) -> None:
    """Incremental sync must send active + inactive jobs, preserving content_hash and status."""
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    active_rows = [
        _job_row(job_id="job-active-1", status="active", content_hash="a" * 64),
        _job_row(job_id="job-active-2", status="active", content_hash="b" * 64),
    ]
    inactive_rows = [
        _job_row(job_id="job-inactive-1", status="inactive", content_hash="c" * 64),
    ]
    conn = _FakeConn(active_rows=active_rows, inactive_rows=inactive_rows)
    requests: list[dict] = []

    @contextmanager
    def _fake_get_connection():
        yield conn

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: float):
        requests.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )

        class _Response:
            def raise_for_status(self) -> None:
                return None

        return _Response()

    monkeypatch.setattr(
        "wekruit_matching.pipeline.job_sync.get_settings",
        lambda: SimpleNamespace(
            firebase_sync_url="https://firebase-sync.example/jobs:batchUpsert",
            firebase_sync_api_key="sync-secret",
            firebase_sync_batch_size=2,
            firebase_sync_timeout_seconds=15.0,
            firebase_sync_collection="matching-jobs",
        ),
    )
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.get_connection", _fake_get_connection)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.httpx.post", _fake_post)

    stats = sync_jobs_to_firebase(since=_dt(0), full_sync=False)

    assert stats["active_jobs"] == 2
    assert stats["inactive_jobs"] == 1
    assert stats["batches"] == 2
    assert len(requests) == 2

    first_payload = requests[0]["json"]
    assert first_payload["collection"] == "matching-jobs"
    assert first_payload["mode"] == "incremental"
    assert first_payload["jobs"][0]["content_hash"] == "a" * 64
    assert first_payload["jobs"][0]["status"] == "active"
    assert requests[0]["headers"]["X-API-Key"] == "sync-secret"

    second_payload = requests[1]["json"]
    assert second_payload["jobs"][0]["job_id"] == "job-inactive-1"
    assert second_payload["jobs"][0]["status"] == "inactive"

    # Incremental sync now issues durable-watermark queries (ensure table +
    # read) before the active fetch, so locate the active call explicitly
    # instead of assuming it is conn.calls[0].
    # The active SELECT now aliases the jobs table as "j" and LEFT JOINs the
    # synced-hash ledger (fix #4).
    active_calls = [c for c in conn.calls if "j.status = 'active'" in c[0]]
    active_sql, active_params = active_calls[0]
    assert "j.embedded_at >= %(since)s" in active_sql
    # Fix #4: incremental selection also catches content_hash-only changes.
    assert "content_hash IS DISTINCT FROM" in active_sql
    assert active_params == {"since": _dt(0)}


def test_sync_jobs_to_firebase_splits_oversized_batches_until_they_fit(monkeypatch) -> None:
    """Oversized Firestore writes should split recursively instead of failing the full sync."""
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    active_rows = [
        _job_row(job_id="job-active-1", status="active", content_hash="a" * 64),
        _job_row(job_id="job-active-2", status="active", content_hash="b" * 64),
        _job_row(job_id="job-active-3", status="active", content_hash="c" * 64),
    ]
    conn = _FakeConn(active_rows=active_rows, inactive_rows=[])
    request_sizes: list[int] = []

    @contextmanager
    def _fake_get_connection():
        yield conn

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: float):
        jobs = json["jobs"]
        request_sizes.append(len(jobs))

        class _Response:
            def __init__(self, size: int):
                self.status_code = 500 if size > 1 else 200
                self.text = (
                    '{"ok":false,"error":"3 INVALID_ARGUMENT: Transaction too big. Decrease transaction size."}'
                    if size > 1
                    else '{"ok":true}'
                )

            @property
            def is_success(self) -> bool:
                return self.status_code == 200

            def raise_for_status(self) -> None:
                if self.is_success:
                    return None
                raise httpx.HTTPStatusError(
                    "batch failed",
                    request=httpx.Request("POST", url),
                    response=httpx.Response(self.status_code, text=self.text),
                )

        return _Response(len(jobs))

    monkeypatch.setattr(
        "wekruit_matching.pipeline.job_sync.get_settings",
        lambda: SimpleNamespace(
            firebase_sync_url="https://firebase-sync.example/api/sync/jobs",
            firebase_sync_api_key="sync-secret",
            firebase_sync_batch_size=3,
            firebase_sync_timeout_seconds=15.0,
            firebase_sync_collection="matching-jobs",
        ),
    )
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.get_connection", _fake_get_connection)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.httpx.post", _fake_post)

    stats = sync_jobs_to_firebase(full_sync=True, include_inactive=False)

    assert stats == {
        "active_jobs": 3,
        "inactive_jobs": 0,
        "synced": 3,
        "batches": 3,
    }
    assert request_sizes == [3, 1, 2, 1, 1]


def test_serialize_job_converts_pgvector_string_embedding_to_number_array() -> None:
    from wekruit_matching.pipeline.job_sync import _serialize_job

    payload = _serialize_job(
        {
            "job_id": "job-1",
            "embedding": "[0.1,0.2,-0.3]",
            "embedded_at": _dt(4),
        }
    )

    assert payload["embedding"] == [0.1, 0.2, -0.3]
    assert payload["embedded_at"] == _dt(4).isoformat()


def test_sync_jobs_to_firebase_bulk_load_queries_all_active_embedded_jobs(monkeypatch) -> None:
    """Bulk load must query all active embedded jobs without a since filter."""
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    conn = _FakeConn(
        active_rows=[_job_row(job_id="job-active-1", status="active", content_hash="a" * 64)],
        inactive_rows=[],
    )

    @contextmanager
    def _fake_get_connection():
        yield conn

    monkeypatch.setattr(
        "wekruit_matching.pipeline.job_sync.get_settings",
        lambda: SimpleNamespace(
            firebase_sync_url="https://firebase-sync.example/jobs:batchUpsert",
            firebase_sync_api_key="sync-secret",
            firebase_sync_batch_size=100,
            firebase_sync_timeout_seconds=15.0,
            firebase_sync_collection="matching-jobs",
        ),
    )
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.get_connection", _fake_get_connection)
    monkeypatch.setattr(
        "wekruit_matching.pipeline.job_sync.httpx.post",
        lambda *args, **kwargs: type("_Response", (), {"raise_for_status": lambda self: None})(),
    )

    stats = sync_jobs_to_firebase(full_sync=True)

    assert stats["active_jobs"] == 1
    active_sql, active_params = conn.calls[0]
    assert "embedded_at >= %(since)s" not in active_sql
    assert active_params is None


def test_sync_jobs_to_firebase_full_sync_can_stage_active_backfill(monkeypatch) -> None:
    """Full sync should support active-job slicing and skipping inactive rows."""
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    conn = _FakeConn(
        active_rows=[
            _job_row(job_id="job-active-1", status="active", content_hash="a" * 64),
            _job_row(job_id="job-active-2", status="active", content_hash="b" * 64),
            _job_row(job_id="job-active-3", status="active", content_hash="c" * 64),
        ],
        inactive_rows=[
            _job_row(job_id="job-inactive-1", status="inactive", content_hash="d" * 64),
        ],
    )
    requests: list[dict] = []

    @contextmanager
    def _fake_get_connection():
        yield conn

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: float):
        requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})

        class _Response:
            def raise_for_status(self) -> None:
                return None

        return _Response()

    monkeypatch.setattr(
        "wekruit_matching.pipeline.job_sync.get_settings",
        lambda: SimpleNamespace(
            firebase_sync_url="https://firebase-sync.example/api/sync/jobs",
            firebase_sync_api_key="sync-secret",
            firebase_sync_batch_size=10,
            firebase_sync_timeout_seconds=15.0,
            firebase_sync_collection="matching-jobs",
        ),
    )
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.get_connection", _fake_get_connection)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.httpx.post", _fake_post)

    stats = sync_jobs_to_firebase(
        full_sync=True,
        active_limit=2,
        active_offset=1,
        include_inactive=False,
    )

    assert stats == {
        "active_jobs": 2,
        "inactive_jobs": 0,
        "synced": 2,
        "batches": 1,
    }
    assert [job["job_id"] for job in requests[0]["json"]["jobs"]] == [
        "job-active-2",
        "job-active-3",
    ]

    active_sql, active_params = conn.calls[0]
    assert "LIMIT %(limit)s" in active_sql
    assert "OFFSET %(offset)s" in active_sql
    assert active_params == {"limit": 2, "offset": 1}
    assert len(conn.calls) == 1


def test_content_hash_only_change_is_reselected_incrementally(monkeypatch) -> None:
    """Fix #4: a row whose embedded_at is OLDER than the watermark (so the
    embedded_at window would skip it) but whose content_hash changed (ATS url
    resolved by Stage 2.5) MUST still be selected and pushed, and its new
    content_hash recorded in the ledger so it is not re-sent forever.
    """
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    resolved = _job_row(job_id="job-resolved", status="active", content_hash="new" + "0" * 61)
    resolved["embedded_at"] = _dt(1)

    conn = _WatermarkConn(
        active_rows=[resolved],
        inactive_rows=[],
        stored_watermark=_dt(1),
    )
    pushed_ids: list[str] = []

    @contextmanager
    def _fake_get_connection():
        yield conn

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: float):
        for job in json["jobs"]:
            pushed_ids.append(job["job_id"])

        class _Response:
            is_success = True

            def raise_for_status(self) -> None:
                return None

        return _Response()

    _patch_settings(monkeypatch)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.get_connection", _fake_get_connection)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.httpx.post", _fake_post)

    sync_jobs_to_firebase(since=_dt(5), full_sync=False)

    assert "job-resolved" in pushed_ids, (
        "a content_hash-only change (resolved ATS url, embedded_at unchanged) "
        "must be re-selected by the incremental sync (fix #4)"
    )
    active_sqls = [c[0] for c in conn.calls if "j.status = 'active'" in c[0]]
    assert active_sqls and "content_hash IS DISTINCT FROM" in active_sqls[0]
    assert "job-resolved" in conn.recorded_hashes


def test_already_synced_hash_not_reselected_when_unchanged(monkeypatch) -> None:
    """Fix #4 (no flood): a row past the watermark whose content_hash already
    matches the ledger and whose embedded_at is below `since` must NOT be
    re-sent — otherwise the whole corpus would re-sync every run."""
    from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

    stable = _job_row(job_id="job-stable", status="active", content_hash="s" * 64)
    stable["embedded_at"] = _dt(1)  # below run-start hour 5

    conn = _WatermarkConn(
        active_rows=[stable],
        inactive_rows=[],
        stored_watermark=_dt(5),
    )
    conn.synced_ledger = {"job-stable"}
    pushed_ids: list[str] = []

    @contextmanager
    def _fake_get_connection():
        yield conn

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: float):
        for job in json["jobs"]:
            pushed_ids.append(job["job_id"])

        class _Response:
            is_success = True

            def raise_for_status(self) -> None:
                return None

        return _Response()

    _patch_settings(monkeypatch)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.get_connection", _fake_get_connection)
    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.httpx.post", _fake_post)

    sync_jobs_to_firebase(since=_dt(5), full_sync=False)

    assert "job-stable" not in pushed_ids, (
        "an unchanged, already-synced row below the watermark must NOT be "
        "re-sent (fix #4 must not flood the corpus)"
    )


def test_sync_jobs_bulk_main_forwards_staged_backfill_flags(monkeypatch) -> None:
    """Bulk sync script should expose safe staged-backfill flags instead of only full blast mode."""
    module = _load_sync_jobs_bulk_module()
    captured: dict = {}

    def _fake_sync_jobs_to_firebase(**kwargs):
        captured.update(kwargs)
        return {"active_jobs": 10, "inactive_jobs": 0, "synced": 10, "batches": 1}

    monkeypatch.setattr("wekruit_matching.pipeline.job_sync.sync_jobs_to_firebase", _fake_sync_jobs_to_firebase)

    module.main(["--active-limit", "1000", "--active-offset", "2000", "--skip-inactive"])

    assert captured == {
        "full_sync": True,
        "active_limit": 1000,
        "active_offset": 2000,
        "include_inactive": False,
    }
