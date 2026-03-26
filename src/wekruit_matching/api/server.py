"""FastAPI HTTP layer for the WeKruit Matching Engine.

Exposes three endpoints for VALET integration:
  POST /match      — returns ranked job matches for a user profile
  POST /feedback   — records a user reaction to a job listing
  GET  /jobs/stats — returns job counts grouped by source_repo and status

All endpoints use synchronous (def) handlers so that psycopg3's synchronous
ConnectionPool is called from a thread (FastAPI runs sync endpoints in a
threadpool automatically), avoiding event-loop blocking.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from wekruit_matching.matching.matcher import get_matches
from wekruit_matching.feedback.handler import record_feedback
from wekruit_matching.models.user_profile import UserProfile
from wekruit_matching.models.feedback import ReactionType
from wekruit_matching.db.connection import get_connection

app = FastAPI(title="WeKruit Matching API", version="0.1.0")


# ---------------------------------------------------------------------------
# Request model for POST /feedback
# ---------------------------------------------------------------------------

class FeedbackRequest(BaseModel):
    user_id: str
    job_id: str
    reaction: ReactionType


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/match")
def match(profile: UserProfile) -> dict:
    """Return a ranked list of job matches for the given user profile.

    Body: UserProfile JSON (user_id required; all other fields optional).
    Response: {"matches": [<job dicts with score and signals>, ...]}
    """
    try:
        matches = get_matches(profile)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"matches": matches}


@app.post("/feedback")
def feedback(body: FeedbackRequest) -> dict:
    """Record a user reaction to a job listing and update their profile state.

    Body: {user_id, job_id, reaction} where reaction is "like" | "dislike" | "applied".
    Response: {"status": "ok"}
    """
    try:
        record_feedback(body.user_id, body.job_id, body.reaction)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"status": "ok"}


@app.get("/jobs/stats")
def jobs_stats() -> dict:
    """Return job counts grouped by source_repo and status.

    Response: {"stats": [{"source_repo": str, "status": str, "count": int}, ...]}
    """
    try:
        with get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT source_repo, status, COUNT(*) AS count
                FROM jobs
                GROUP BY source_repo, status
                ORDER BY source_repo, status
                """
            )
            rows = cursor.fetchall()
            stats = [
                {
                    "source_repo": row["source_repo"],
                    "status": row["status"],
                    "count": row["count"],
                }
                for row in rows
            ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"stats": stats}
