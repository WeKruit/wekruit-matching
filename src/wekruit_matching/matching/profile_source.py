"""Read-only PA profile patch loader for matching ranker signals."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from loguru import logger

from wekruit_matching.config import Settings, get_settings
from wekruit_matching.models.user_profile import UserProfile

_RANKER_PROFILE_FIELDS = (
    "totalYearsExperience",
    "derivedExperience",
    "derivedExperienceVersion",
    "derivedExperienceContentHash",
)


def _build_firestore_client(settings: Settings) -> Any:
    from google.cloud import firestore

    if settings.firebase_service_account_json:
        from google.oauth2 import service_account

        service_account_info = json.loads(settings.firebase_service_account_json)
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info
        )
        project_id = settings.firestore_project_id or service_account_info.get(
            "project_id"
        )
        return firestore.Client(project=project_id, credentials=credentials)

    project_id = settings.firestore_project_id or None
    return firestore.Client(project=project_id)


@lru_cache(maxsize=1)
def get_firestore_client() -> Any:
    """Return a cached Firestore client for the process."""
    return _build_firestore_client(get_settings())


def _skill_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    seen: set[str] = set()
    names: list[str] = []
    for item in value:
        raw_name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(raw_name, str):
            continue

        name = raw_name.strip()
        if not name:
            continue

        key = name.lower()
        if key in seen:
            continue

        seen.add(key)
        names.append(name)

    return names


def _ranker_skills(data: dict[str, Any]) -> list[str]:
    top_level_skills = _skill_names(data.get("skills"))
    if top_level_skills:
        return top_level_skills

    tags = data.get("tags")
    if not isinstance(tags, dict):
        return []
    return _skill_names(tags.get("skills"))


def fetch_pa_user_profile_patch(user_id: str, client: Any | None = None) -> dict[str, Any]:
    """Fetch the ranker-readable subset of pa-users/{user_id}.

    The matching side only reads these fields. It never mutates
    derivedExperience; PA's extractor trigger owns that write path.
    """
    if not user_id:
        return {}

    try:
        active_client = client or get_firestore_client()
        snapshot = active_client.collection("pa-users").document(user_id).get()
    except Exception as exc:
        logger.warning("Failed to read pa-users/{} derivedExperience: {}", user_id, exc)
        return {}

    if not getattr(snapshot, "exists", False):
        return {}

    data = snapshot.to_dict() or {}
    patch = {
        field: data[field]
        for field in _RANKER_PROFILE_FIELDS
        if field in data and data[field] is not None
    }
    skills = _ranker_skills(data)
    if skills:
        patch["skills"] = skills
    return patch


def merge_pa_user_profile_patch(profile: UserProfile, patch: dict[str, Any]) -> UserProfile:
    """Merge a Firestore PA profile patch into a request UserProfile.

    Request-provided preferences remain authoritative. Firestore fills only the
    global candidate fields matching needs for derivedExperience and legacy
    fallback scoring.
    """
    if not patch:
        return profile

    merged = profile.model_dump(by_alias=True)
    if not profile.skills and patch.get("skills"):
        merged["skills"] = patch["skills"]
    if (
        profile.total_years_experience is None
        and patch.get("totalYearsExperience") is not None
    ):
        merged["totalYearsExperience"] = patch["totalYearsExperience"]
    if patch.get("derivedExperience") is not None:
        merged["derivedExperience"] = patch["derivedExperience"]
    if patch.get("derivedExperienceVersion") is not None:
        merged["derivedExperienceVersion"] = patch["derivedExperienceVersion"]
    if patch.get("derivedExperienceContentHash") is not None:
        merged["derivedExperienceContentHash"] = patch["derivedExperienceContentHash"]

    return UserProfile.model_validate(merged)
