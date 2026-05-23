"""Tests for PA profile reads used by matching ranker integration."""
from __future__ import annotations

from typing import Any

from wekruit_matching.matching.profile_source import (
    fetch_pa_user_profile_patch,
    merge_pa_user_profile_patch,
)
from wekruit_matching.models.user_profile import UserProfile


class _FakeSnapshot:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.exists = payload is not None
        self._payload = payload

    def to_dict(self) -> dict[str, Any] | None:
        return self._payload


class _FakeDocument:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload

    def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self._payload)


class _FakeCollection:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload
        self.requested_id: str | None = None

    def document(self, document_id: str) -> _FakeDocument:
        self.requested_id = document_id
        return _FakeDocument(self._payload)


class _FakeClient:
    def __init__(self, payload: dict[str, Any] | None) -> None:
        self.collection_ref = _FakeCollection(payload)

    def collection(self, collection_name: str) -> _FakeCollection:
        assert collection_name == "pa-users"
        return self.collection_ref


def test_fetch_pa_user_profile_patch_reads_only_ranker_fields():
    payload = {
        "skills": ["python"],
        "totalYearsExperience": 5,
        "derivedExperience": {
            "version": "v1",
            "yearsTotal": 5,
            "yearsPerSkill": {"python": 5},
            "skillRecency": {"python": "present"},
            "titleTrajectory": ["Software Engineer"],
            "seniorityCurrent": "entry_level",
            "responsibilityCurrent": "individual_contributor",
            "industryHistory": {},
            "unverifiedSkills": [],
            "computedAt": "2026-05-22T12:00:00Z",
        },
        "derivedExperienceVersion": "v1",
        "phoneE164": "+15555550123",
    }
    client = _FakeClient(payload)

    patch = fetch_pa_user_profile_patch("pa-user-1", client=client)

    assert client.collection_ref.requested_id == "pa-user-1"
    assert patch == {
        "skills": ["python"],
        "totalYearsExperience": 5,
        "derivedExperience": payload["derivedExperience"],
        "derivedExperienceVersion": "v1",
    }


def test_fetch_pa_user_profile_patch_reads_nested_pa_tag_skills():
    payload = {
        "tags": {
            "skills": [
                {"name": "Python", "bucket": "programming_languages"},
                {"name": "React", "bucket": "frameworks_and_libraries"},
                {"name": "python", "bucket": "programming_languages"},
                {"bucket": "databases"},
            ],
            "phoneE164": "+15555550123",
        },
        "phoneE164": "+15555550123",
    }
    client = _FakeClient(payload)

    patch = fetch_pa_user_profile_patch("pa-user-1", client=client)

    assert patch == {"skills": ["Python", "React"]}


def test_fetch_pa_user_profile_patch_prefers_top_level_skills_over_tags():
    payload = {
        "skills": ["Go"],
        "tags": {"skills": [{"name": "Python"}]},
    }
    client = _FakeClient(payload)

    patch = fetch_pa_user_profile_patch("pa-user-1", client=client)

    assert patch == {"skills": ["Go"]}


def test_fetch_pa_user_profile_patch_missing_doc_returns_empty_patch():
    client = _FakeClient(None)

    assert fetch_pa_user_profile_patch("missing-user", client=client) == {}


def test_fetch_pa_user_profile_patch_client_failure_returns_empty_patch(monkeypatch):
    from wekruit_matching.matching import profile_source

    def fail_client():
        raise RuntimeError("no firestore credentials")

    monkeypatch.setattr(profile_source, "get_firestore_client", fail_client)

    assert fetch_pa_user_profile_patch("pa-user-1") == {}


def test_merge_pa_user_profile_patch_preserves_request_skills_when_present():
    profile = UserProfile(user_id="pa-user-1", skills=["typescript"])
    patch = {
        "skills": ["python"],
        "totalYearsExperience": 5,
        "derivedExperience": {
            "version": "v1",
            "yearsTotal": 5,
            "yearsPerSkill": {"python": 5},
            "skillRecency": {"python": "present"},
            "titleTrajectory": ["Software Engineer"],
            "seniorityCurrent": "entry_level",
            "responsibilityCurrent": "individual_contributor",
            "industryHistory": {},
            "unverifiedSkills": [],
            "computedAt": "2026-05-22T12:00:00Z",
        },
    }

    merged = merge_pa_user_profile_patch(profile, patch)

    assert merged.skills == ["typescript"]
    assert merged.total_years_experience == 5
    assert merged.derived_experience is not None
    assert merged.derived_experience.years_per_skill["python"] == 5


def test_merge_pa_user_profile_patch_fills_legacy_skills_when_request_has_none():
    profile = UserProfile(user_id="pa-user-1", skills=[])

    merged = merge_pa_user_profile_patch(profile, {"skills": ["python"]})

    assert merged.skills == ["python"]
