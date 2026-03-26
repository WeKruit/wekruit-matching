"""Feedback data model.

Records a user's reaction to a job match result.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


class ReactionType(str, Enum):
    LIKE = "like"
    DISLIKE = "dislike"
    APPLIED = "applied"


class Feedback(BaseModel):
    """A single user reaction to a job listing."""

    feedback_id: Optional[str] = None  # Set by DB on insert
    user_id: str
    job_id: str
    reaction: ReactionType
    recorded_at: datetime = Field(default_factory=_utcnow)
