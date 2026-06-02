"""Offline unit tests for the Stage-0 dependency/credential preflight.

These are FULLY OFFLINE: no DB, no real network. The google auth path is
monkeypatched at its seams so the live token-mint RPC never fires. NOT marked
@integration — they must run in the default suite.

Covered:
  * missing required core secret (DATABASE_URL) -> hard_fail True, sync_ok False
  * Firestore token mint raising RefreshError (expired/revoked key) -> sync_ok
    False but hard_fail False (the night DEGRADES, does not abort)
  * no Firestore creds configured -> sync_ok True (nothing to validate)
  * happy path (creds parse, network probe off) -> ok True
  * require_sync escalates a sync-credential problem to a hard fail
  * CLI exit codes: 0 / 2 / 1
"""

from __future__ import annotations

import google.auth.exceptions as gae

from wekruit_matching.pipeline import preflight as pf


class _FakeSettings:
    """Stand-in for the pydantic Settings object."""

    def __init__(
        self,
        *,
        database_url="postgresql://u:p@h:5432/db",
        anthropic_api_key="sk-ant-x",
        openai_api_key="sk-openai-x",
        firebase_service_account_json="",
        firestore_project_id="",
    ):
        self.database_url = database_url
        self.anthropic_api_key = anthropic_api_key
        self.openai_api_key = openai_api_key
        self.firebase_service_account_json = firebase_service_account_json
        self.firestore_project_id = firestore_project_id


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr(
        "wekruit_matching.config.get_settings", lambda: settings
    )


# ---------------------------------------------------------------------------
# Core env
# ---------------------------------------------------------------------------
def test_missing_database_url_is_hard_fail(monkeypatch):
    """A blank DATABASE_URL -> hard_fail True, sync_ok False, ok False."""
    _patch_settings(monkeypatch, _FakeSettings(database_url=""))
    # Ensure no Firestore probe is even reached.
    monkeypatch.delenv("WEKRUIT_PREFLIGHT_PROBE_FIRESTORE", raising=False)

    result = pf.run_preflight()

    assert result.hard_fail is True
    assert result.sync_ok is False
    assert result.ok is False
    assert any("DATABASE_URL" in p for p in result.problems), result.problems


def test_config_unloadable_is_hard_fail(monkeypatch):
    """If get_settings() itself raises (missing required env -> ValidationError),
    that is a hard fail, not a crash."""

    def _boom():
        raise RuntimeError("missing required env")

    monkeypatch.setattr("wekruit_matching.config.get_settings", _boom)

    result = pf.run_preflight()
    assert result.hard_fail is True
    assert result.ok is False


# ---------------------------------------------------------------------------
# Firestore credential liveness (the IL-5 degrade path)
# ---------------------------------------------------------------------------
def test_refresh_error_degrades_not_aborts(monkeypatch):
    """An expired/revoked SA (token mint -> RefreshError) sets sync_ok False but
    NOT hard_fail: the night degrades (skip sync), it does not no-op."""
    # Core env present, ADC path (no SA JSON), probe ENABLED.
    _patch_settings(monkeypatch, _FakeSettings())
    monkeypatch.setenv("WEKRUIT_PREFLIGHT_PROBE_FIRESTORE", "1")
    # creds are "configured" so the probe runs (ADC env present).
    monkeypatch.setattr(pf, "_firestore_creds_configured", lambda: True)

    class _FakeCreds:
        def refresh(self, _request):
            raise gae.RefreshError("invalid_grant: account disabled")

    # google.auth.default() loads the credential object WITHOUT network; we
    # return a fake whose refresh() raises (the network-mint failure).
    monkeypatch.setattr(
        "google.auth.default", lambda scopes=None: (_FakeCreds(), "proj")
    )
    # Request() must construct without network.
    monkeypatch.setattr(
        "google.auth.transport.requests.Request", lambda: object()
    )

    result = pf.run_preflight()

    assert result.hard_fail is False, result.problems
    assert result.sync_ok is False, result.problems
    assert result.ok is False
    assert any("expired/revoked" in p or "token mint" in p for p in result.problems), \
        result.problems


def test_no_firestore_creds_configured_keeps_sync_ok(monkeypatch):
    """No Firestore creds at all -> nothing to validate -> sync_ok True (the sync
    stage itself gates on FIREBASE_SYNC_URL later)."""
    _patch_settings(monkeypatch, _FakeSettings())
    monkeypatch.setattr(pf, "_firestore_creds_configured", lambda: False)

    result = pf.run_preflight()
    assert result.hard_fail is False
    assert result.sync_ok is True
    assert result.ok is True


def test_creds_parse_with_probe_off_is_ok(monkeypatch):
    """Creds present + network probe OFF (default) -> credentials construct
    offline, treated as usable -> ok True."""
    _patch_settings(monkeypatch, _FakeSettings())
    monkeypatch.delenv("WEKRUIT_PREFLIGHT_PROBE_FIRESTORE", raising=False)
    monkeypatch.setattr(pf, "_firestore_creds_configured", lambda: True)

    # ADC path; default() loads creds object offline. refresh() must NOT be
    # called because the probe flag is off.
    class _Creds:
        def refresh(self, _r):  # pragma: no cover - must not be called
            raise AssertionError("refresh() called with probe disabled")

    monkeypatch.setattr("google.auth.default", lambda scopes=None: (_Creds(), "p"))

    result = pf.run_preflight()
    assert result.hard_fail is False
    assert result.sync_ok is True
    assert result.ok is True


def test_default_credentials_error_degrades(monkeypatch):
    """Absent/malformed ADC -> DefaultCredentialsError on load -> sync_ok False,
    hard_fail False (degrade)."""
    _patch_settings(monkeypatch, _FakeSettings())
    monkeypatch.setattr(pf, "_firestore_creds_configured", lambda: True)

    def _raise(scopes=None):
        raise gae.DefaultCredentialsError("no ADC found")

    monkeypatch.setattr("google.auth.default", _raise)

    result = pf.run_preflight()
    assert result.hard_fail is False
    assert result.sync_ok is False
    assert any("malformed/unloadable" in p for p in result.problems), result.problems


def test_require_sync_escalates_to_hard_fail(monkeypatch):
    """With require_sync=True, a sync-credential problem becomes a HARD FAIL."""
    _patch_settings(monkeypatch, _FakeSettings())
    monkeypatch.setattr(pf, "_firestore_creds_configured", lambda: True)

    def _raise(scopes=None):
        raise gae.DefaultCredentialsError("no ADC found")

    monkeypatch.setattr("google.auth.default", _raise)

    result = pf.run_preflight(require_sync=True)
    assert result.hard_fail is True
    assert result.sync_ok is False
    assert result.ok is False


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------
def test_cli_exit_0_all_live(monkeypatch):
    _patch_settings(monkeypatch, _FakeSettings())
    monkeypatch.setattr(pf, "_firestore_creds_configured", lambda: False)
    assert pf.main([]) == 0


def test_cli_exit_2_sync_down(monkeypatch):
    _patch_settings(monkeypatch, _FakeSettings())
    monkeypatch.setattr(pf, "_firestore_creds_configured", lambda: True)

    def _raise(scopes=None):
        raise gae.DefaultCredentialsError("no ADC")

    monkeypatch.setattr("google.auth.default", _raise)
    assert pf.main([]) == 2


def test_cli_exit_1_hard_fail(monkeypatch):
    _patch_settings(monkeypatch, _FakeSettings(database_url=""))
    assert pf.main([]) == 1
