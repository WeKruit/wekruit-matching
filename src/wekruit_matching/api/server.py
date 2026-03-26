"""FastAPI HTTP layer for the WeKruit Matching Engine.

Exposes three endpoints for VALET integration:
  POST /match      — returns ranked job matches for a user profile
  POST /feedback   — records a user reaction to a job listing
  GET  /jobs/stats — returns job counts grouped by source_repo and status

All endpoints use synchronous (def) handlers so that psycopg3's synchronous
ConnectionPool is called from a thread (FastAPI runs sync endpoints in a
threadpool automatically), avoiding event-loop blocking.

Authentication: all endpoints except GET / require an X-API-Key header that
matches the API_SECRET_KEY environment variable (read via Settings).
"""
import hmac

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, Field

from wekruit_matching.config import get_settings
from wekruit_matching.matching.matcher import get_matches
from wekruit_matching.feedback.handler import record_feedback
from wekruit_matching.models.user_profile import UserProfile
from wekruit_matching.models.feedback import ReactionType
from wekruit_matching.db.connection import get_connection

app = FastAPI(
    title="WeKruit Matching API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """Dependency that validates the X-API-Key header against Settings.api_secret_key.

    Raises HTTP 401 if the header is missing or does not match.
    Applied to all endpoints except GET / (health check).
    """
    expected = get_settings().api_secret_key
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
def root() -> dict:
    """Health check / API info."""
    return {"status": "ok", "service": "wekruit-matching"}


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
def match(profile: UserProfile, _: None = Depends(verify_api_key)) -> dict:
    """Return a ranked list of job matches for the given user profile.

    Body: UserProfile JSON (user_id required; all other fields optional).
    Response: {"matches": [<job dicts with score and signals>, ...]}
    """
    try:
        matches = get_matches(profile)
    except Exception as e:
        logger.exception("Unhandled error in POST /match: {}", e)
        raise HTTPException(status_code=500, detail="Internal server error") from e
    return {"matches": matches}


@app.post("/feedback")
def feedback(body: FeedbackRequest, _: None = Depends(verify_api_key)) -> dict:
    """Record a user reaction to a job listing and update their profile state.

    Body: {user_id, job_id, reaction} where reaction is "like" | "dislike" | "applied".
    Response: {"status": "ok"}
    """
    try:
        record_feedback(body.user_id, body.job_id, body.reaction)
    except Exception as e:
        logger.exception("Unhandled error in POST /feedback: {}", e)
        raise HTTPException(status_code=500, detail="Internal server error") from e
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# JobX-compatible endpoint (VALET integration)
# POST /api/v1/matching/recommendations
# Accepts CandidateProfile, returns MatchResponse in VALET's expected format
# ---------------------------------------------------------------------------

class JobXMatchRequest(BaseModel):
    """Request shape expected by VALET's JobXClient."""
    candidate: dict  # CandidateProfile — skills, experience, education, etc.
    top_n: int = Field(default=20, ge=1, le=100)
    min_cosine_score: float = Field(default=0.3, ge=0.0, le=1.0)
    excludeJobIds: list[str] = Field(default_factory=list, max_length=500)
    preferredCountryCode: str | None = None
    top_k: int = Field(default=100, ge=1, le=500)
    enable_llm_rerank: bool = False


@app.post("/api/v1/matching/recommendations")
def jobx_recommendations(body: JobXMatchRequest, _: None = Depends(verify_api_key)) -> dict:
    """JobX-compatible matching endpoint for VALET integration.

    Translates VALET's CandidateProfile into our UserProfile, runs matching,
    and returns results in VALET's expected MatchResponse format.
    """
    try:
        candidate = body.candidate

        # Build UserProfile from CandidateProfile
        skills = candidate.get("skills", [])
        skill_names = [s.get("name", s) if isinstance(s, dict) else str(s) for s in skills]

        profile = UserProfile(
            user_id=candidate.get("id", "jobx-anonymous"),
            skills=skill_names,
            preferred_locations=[body.preferredCountryCode] if body.preferredCountryCode else [],
            preferred_job_type="any",
        )

        matches = get_matches(profile, top_n=body.top_n)

        # Transform to VALET's MatchResultItem format
        results = []
        for m in matches:
            if m.get("job_id") in body.excludeJobIds:
                continue
            signals = m.get("signals", {})
            results.append({
                "job_id": m.get("job_id", ""),
                "source": m.get("source_repo", ""),
                "title": m.get("role_title", ""),
                "apply_url": m.get("primary_url", ""),
                "locations": [{"display_name": m.get("location_raw", ""), "is_primary": True}],
                "department": None,
                "team": None,
                "employment_type": "internship" if "Intern" in m.get("source_repo", "") else "full_time",
                "cosine_score": signals.get("title_similarity", 0),
                "skill_overlap_score": signals.get("skills_overlap", 0),
                "domain_match_score": signals.get("industry_match", 0),
                "seniority_match_score": 0.5,
                "experience_gap": 0,
                "education_gap": 0,
                "penalties": {},
                "company": {
                    "name": m.get("company_name", ""),
                    "industry": m.get("industry", "unknown"),
                    "size_category": m.get("company_size", "unknown"),
                },
                "combined_score": m.get("score", 0),
            })

        meta = {
            "needs_sponsorship": profile.requires_sponsorship,
            "user_total_years_experience": 0,
            "user_degree_rank": 0,
            "user_skill_count": len(skill_names),
            "user_domain": candidate.get("domain", "software_engineering"),
            "user_seniority": candidate.get("seniority", "entry"),
            "top_k": body.top_k,
            "top_n": body.top_n,
            "results_returned": len(results),
        }

        return {"meta": meta, "results": results}
    except Exception as e:
        logger.exception("Unhandled error in POST /api/v1/matching/recommendations: {}", e)
        raise HTTPException(status_code=500, detail="Internal server error") from e


@app.get("/jobs/stats")
def jobs_stats(_: None = Depends(verify_api_key)) -> dict:
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
        logger.exception("Unhandled error in GET /jobs/stats: {}", e)
        raise HTTPException(status_code=500, detail="Internal server error") from e
    return {"stats": stats}
