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

    def execute(self, sql: str, params: dict | None = None) -> _FakeResult:
        self.calls.append((sql, params))
        if "WHERE status = 'active'" in sql:
            rows = self.active_rows
            if params and "offset" in params:
                rows = rows[params["offset"] :]
            if params and "limit" in params:
                rows = rows[: params["limit"]]
            return _FakeResult(rows)
        if "WHERE status = 'inactive'" in sql:
            return _FakeResult(self.inactive_rows)
        raise AssertionError(f"Unexpected SQL: {sql}")


def _load_sync_jobs_bulk_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "sync_jobs_bulk.py"
    spec = importlib.util.spec_from_file_location("sync_jobs_bulk", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    active_sql, active_params = conn.calls[0]
    assert "embedded_at >= %(since)s" in active_sql
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
