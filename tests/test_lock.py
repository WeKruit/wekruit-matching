"""Tests for the Firestore distributed lock used by the daily scrape.

Covers:
  * ACQUIRED — fresh day, no existing doc → create succeeds.
  * CONTENDED — doc exists, not released, not stale → quiet exit path.
  * ALREADY_RUN — doc exists with releasedAt + outcome=success.
  * STOLEN_STALE — doc exists with expiresAtMs < now and no releasedAt.
  * release() round-trips outcome + stats.
  * Context manager marks "failed" on exception, "success" on clean exit.

We don't touch real Firestore. A small in-memory fake provides just enough
shape (``create``, ``get``, ``set``, ``delete``) for the lock's code paths.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from google.api_core.exceptions import AlreadyExists

from wekruit_matching.lock import (
    LOCK_COLLECTION,
    STALE_AFTER_SECONDS,
    DailyScrapeLock,
    LockState,
)


# ---------------------------------------------------------------------------
# Minimal Firestore stand-in. Only the methods lock.py actually calls are
# implemented — everything else would be dead code complicating the fake.
# ---------------------------------------------------------------------------


@dataclass
class _FakeSnap:
    _data: dict[str, Any] | None

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


@dataclass
class _FakeDoc:
    _store: dict[str, dict[str, Any]]
    _doc_id: str

    def create(self, payload: dict[str, Any]) -> None:
        if self._doc_id in self._store:
            raise AlreadyExists(f"{self._doc_id} already exists")
        self._store[self._doc_id] = dict(payload)

    def get(self) -> _FakeSnap:
        return _FakeSnap(self._store.get(self._doc_id))

    def set(self, payload: dict[str, Any], merge: bool = False) -> None:
        if merge and self._doc_id in self._store:
            self._store[self._doc_id].update(payload)
        else:
            self._store[self._doc_id] = dict(payload)

    def delete(self) -> None:
        self._store.pop(self._doc_id, None)


@dataclass
class _FakeCollection:
    _store: dict[str, dict[str, Any]]

    def document(self, doc_id: str) -> _FakeDoc:
        return _FakeDoc(self._store, doc_id)


@dataclass
class _FakeFirestore:
    collections: dict[str, dict[str, Any]] = field(default_factory=dict)

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self.collections.setdefault(name, {}))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


FIXED_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


def _make_lock(
    client: _FakeFirestore,
    now: datetime = FIXED_NOW,
    state_file: Path | None = None,
    acquired_by: str = "test-runner",
) -> DailyScrapeLock:
    return DailyScrapeLock(
        acquired_by=acquired_by,
        now_fn=lambda: now,
        client=client,  # type: ignore[arg-type]
        state_file=state_file or Path("/tmp/wekruit-scrape-lock-test.json"),
    )


@pytest.fixture
def fake_client() -> _FakeFirestore:
    return _FakeFirestore()


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "lock-state.json"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_acquire_creates_new_lock(fake_client: _FakeFirestore, state_file: Path) -> None:
    lock = _make_lock(fake_client, state_file=state_file)
    result = lock.acquire()

    assert result.state is LockState.ACQUIRED
    assert result.lock_id == "scrape-daily-2026-05-27"
    assert result.holder == "test-runner"
    docs = fake_client.collections[LOCK_COLLECTION]
    assert "scrape-daily-2026-05-27" in docs
    assert docs["scrape-daily-2026-05-27"]["acquiredBy"] == "test-runner"
    # State file written so a sibling `release` invocation can find it.
    persisted = json.loads(state_file.read_text())
    assert persisted["lock_id"] == "scrape-daily-2026-05-27"


def test_acquire_contended_when_active_lock_exists(
    fake_client: _FakeFirestore, state_file: Path
) -> None:
    # A different runner just acquired.
    fake_client.collections[LOCK_COLLECTION] = {
        "scrape-daily-2026-05-27": {
            "acquiredBy": "macmini-launchd",
            "acquiredAtMs": int(FIXED_NOW.timestamp() * 1000),
            "expiresAtMs": int(FIXED_NOW.timestamp() * 1000) + STALE_AFTER_SECONDS * 1000,
            "releasedAt": None,
            "outcome": None,
        }
    }
    lock = _make_lock(fake_client, state_file=state_file)
    result = lock.acquire()

    assert result.state is LockState.CONTENDED
    assert result.holder == "macmini-launchd"


def test_acquire_returns_already_run_when_today_released_success(
    fake_client: _FakeFirestore, state_file: Path
) -> None:
    fake_client.collections[LOCK_COLLECTION] = {
        "scrape-daily-2026-05-27": {
            "acquiredBy": "github-actions",
            "acquiredAtMs": int(FIXED_NOW.timestamp() * 1000) - 7200_000,  # 2h ago
            "expiresAtMs": int(FIXED_NOW.timestamp() * 1000) + STALE_AFTER_SECONDS * 1000,
            "releasedAt": FIXED_NOW - timedelta(hours=1),
            "outcome": "success",
        }
    }
    lock = _make_lock(fake_client, state_file=state_file)
    result = lock.acquire()

    assert result.state is LockState.ALREADY_RUN
    assert result.holder == "github-actions"


def test_acquire_steals_stale_lock(fake_client: _FakeFirestore, state_file: Path) -> None:
    # Previous holder acquired 5h ago and never released — past the 4h threshold.
    stale_ms = int(FIXED_NOW.timestamp() * 1000) - 5 * 3600_000
    fake_client.collections[LOCK_COLLECTION] = {
        "scrape-daily-2026-05-27": {
            "acquiredBy": "macmini-launchd",
            "acquiredAtMs": stale_ms,
            "expiresAtMs": stale_ms + STALE_AFTER_SECONDS * 1000,  # < now
            "releasedAt": None,
            "outcome": None,
        }
    }
    lock = _make_lock(fake_client, state_file=state_file)
    result = lock.acquire()

    assert result.state is LockState.STOLEN_STALE
    assert result.holder == "test-runner"
    new_doc = fake_client.collections[LOCK_COLLECTION]["scrape-daily-2026-05-27"]
    assert new_doc["stolenFrom"] == "macmini-launchd"


def test_release_writes_outcome_and_stats(fake_client: _FakeFirestore, state_file: Path) -> None:
    lock = _make_lock(fake_client, state_file=state_file)
    lock.acquire()

    lock.release(outcome="success", stats={"jobsScraped": 1234, "costUsd": 4.20})

    doc = fake_client.collections[LOCK_COLLECTION]["scrape-daily-2026-05-27"]
    assert doc["outcome"] == "success"
    assert doc["releasedAt"] == FIXED_NOW
    assert doc["stats"]["jobsScraped"] == 1234


def test_context_manager_marks_failed_on_exception(
    fake_client: _FakeFirestore, state_file: Path
) -> None:
    with pytest.raises(RuntimeError):
        with _make_lock(fake_client, state_file=state_file) as lock:
            assert lock.state is LockState.ACQUIRED
            raise RuntimeError("pipeline crashed")

    doc = fake_client.collections[LOCK_COLLECTION]["scrape-daily-2026-05-27"]
    assert doc["outcome"] == "failed"


def test_context_manager_marks_success_on_clean_exit(
    fake_client: _FakeFirestore, state_file: Path
) -> None:
    with _make_lock(fake_client, state_file=state_file) as lock:
        assert lock.state is LockState.ACQUIRED
    doc = fake_client.collections[LOCK_COLLECTION]["scrape-daily-2026-05-27"]
    assert doc["outcome"] == "success"


def test_context_manager_skips_release_when_contended(
    fake_client: _FakeFirestore, state_file: Path
) -> None:
    # Pre-seed a contending lock.
    fake_client.collections[LOCK_COLLECTION] = {
        "scrape-daily-2026-05-27": {
            "acquiredBy": "macmini-launchd",
            "acquiredAtMs": int(FIXED_NOW.timestamp() * 1000),
            "expiresAtMs": int(FIXED_NOW.timestamp() * 1000) + STALE_AFTER_SECONDS * 1000,
            "releasedAt": None,
            "outcome": None,
        }
    }
    with _make_lock(fake_client, state_file=state_file) as lock:
        assert lock.state is LockState.CONTENDED

    # The original holder's doc is unchanged — we did NOT release someone
    # else's lock.
    doc = fake_client.collections[LOCK_COLLECTION]["scrape-daily-2026-05-27"]
    assert doc["acquiredBy"] == "macmini-launchd"
    assert doc["releasedAt"] is None


def test_cli_acquire_exit_codes(
    monkeypatch: pytest.MonkeyPatch, fake_client: _FakeFirestore, capsys: pytest.CaptureFixture
) -> None:
    # Patch the client factory so the CLI sees the fake.
    import wekruit_matching.lock as lock_mod

    monkeypatch.setattr(
        lock_mod.DailyScrapeLock,
        "_get_client",
        lambda self: fake_client,
    )
    # Stable "now" for the CLI path.
    monkeypatch.setattr(lock_mod, "DEFAULT_STATE_FILE", Path("/tmp/wekruit-cli-test.json"))

    # First call — should ACQUIRE → exit 0.
    rc = lock_mod._cli(["acquire", "--acquired-by", "test-cli"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["state"] == "acquired"

    # Second call same day → CONTENDED → exit 2.
    rc2 = lock_mod._cli(["acquire", "--acquired-by", "test-cli-2"])
    assert rc2 == 2
    out2 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out2["state"] == "contended"
