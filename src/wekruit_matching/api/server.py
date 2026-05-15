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

from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from wekruit_matching.config import get_settings
from wekruit_matching.matching.matcher import get_matches
from wekruit_matching.feedback.handler import record_feedback
from wekruit_matching.models.user_profile import UserProfile
from wekruit_matching.models.feedback import ReactionType
from wekruit_matching.db.connection import get_connection
from wekruit_matching.api.internal_ui import router as internal_router

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="WeKruit Matching API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.state.limiter = limiter
app.include_router(internal_router)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})

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
@limiter.limit("60/minute")
def match(request: Request, profile: UserProfile, _: None = Depends(verify_api_key)) -> dict:
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
@limiter.limit("60/minute")
def jobx_recommendations(request: Request, body: JobXMatchRequest, _: None = Depends(verify_api_key)) -> dict:
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


# ---------------------------------------------------------------------------
# POST /analyze-url — On-demand job URL analysis (no DB write)
# ---------------------------------------------------------------------------

class AnalyzeUrlRequest(BaseModel):
    """Request for on-demand job URL analysis."""
    url: str = Field(..., description="Job posting URL to analyze")
    user_skills: list[str] = Field(default_factory=list, description="User's skills for matching")


@app.post("/analyze-url")
@limiter.limit("30/minute")
def analyze_url(request: Request, body: AnalyzeUrlRequest, _: None = Depends(verify_api_key)) -> dict:
    """Scrape a job URL and classify it on-demand without storing to DB.

    Returns: job title, company, required skills, matched skills, match score.
    Uses the same Claude Haiku classifier as batch enrichment — cheap & structured.
    """
    import httpx
    from wekruit_matching.enrichment.classifier import classify_job  # KNOWN_SKILLS deleted (P7-C 2026-05-08)
    from wekruit_matching.models.job import Job

    # Step 1: Fetch the job page
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(body.url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            })
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        logger.warning("Failed to fetch {}: {}", body.url, e)
        raise HTTPException(status_code=422, detail=f"Could not fetch URL: {e}") from e

    # Step 2: Extract text from HTML (strip scripts/styles, keep text)
    import re
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.IGNORECASE)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()[:6000]

    # Step 3: Extract title/company from HTML meta tags or page text
    title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    page_title = title_match.group(1).strip() if title_match else ""

    # Try og:title for cleaner job title
    og_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', html, re.IGNORECASE)
    job_title = og_match.group(1).strip() if og_match else page_title.split("|")[0].split("-")[0].strip()

    # Try og:site_name for company
    site_match = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\'](.*?)["\']', html, re.IGNORECASE)
    company = site_match.group(1).strip() if site_match else ""

    # Step 4: Classify using existing enrichment pipeline (Claude Haiku — cheap)
    fake_job = Job(
        job_id=f"analyze-{hash(body.url) % 10**8}",
        source_repo="on-demand",
        company_name=company or "Unknown",
        role_title=job_title or "Unknown Role",
        primary_url=body.url,
        location_raw="",
    )
    enrichment = classify_job(fake_job)

    # Step 5: Match user skills against required skills using the SAME scorer as /match
    from wekruit_matching.matching.scorer import score_skills_overlap, WEIGHTS
    user_skills_lower = {s.lower() for s in body.user_skills}
    matched = [s for s in enrichment.required_skills if s.lower() in user_skills_lower]

    # Use the same coverage-dominant skill overlap scorer as batch /match
    skills_signal = score_skills_overlap(body.user_skills, enrichment.required_skills)
    # For signals we can't compute (title embedding, location, etc.), use neutral 0.5
    # This produces a score comparable to /match — not inflated by skill-only counting
    neutral = 0.5
    match_score = round(
        (
            WEIGHTS["skills_overlap"] * skills_signal
            + WEIGHTS["title_similarity"] * neutral
            + WEIGHTS["industry_match"] * (1.0 if enrichment.industry else neutral)
            + WEIGHTS["company_size_match"] * neutral
            + WEIGHTS["location_fit"] * neutral
            + WEIGHTS["recency"] * 0.8  # assume recent since user just found it
            + WEIGHTS["feedback_boost"] * neutral
        )
        * 100
    )

    return {
        "jobTitle": job_title or fake_job.role_title,
        "company": company or fake_job.company_name,
        "jobDescription": text[:500],
        "requiredSkills": enrichment.required_skills,
        "preferredSkills": [],
        "matchedSkills": matched,
        "matchScore": match_score,
        "industry": enrichment.industry,
        "companySize": enrichment.company_size,
        "sponsorship": enrichment.sponsorship,
    }


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


# ---------------------------------------------------------------------------
# V3.3 — Firecrawl proxy endpoint
# ---------------------------------------------------------------------------
# Exposes the macmini-local Firecrawl Docker service (localhost:3002) to
# WeKruit cloud-functions over the existing cloudflare tunnel
# (matching.wekruit.com). Inbound auth = X-API-Key (same as /match);
# outbound auth = Bearer firecrawl_api_key (from .env).

import httpx as _httpx_for_fc


class FirecrawlScrapeRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2_000)
    formats: list[str] = Field(default_factory=lambda: ["markdown"])
    only_main_content: bool = True
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)


@app.post("/firecrawl/scrape")
@limiter.limit("30/minute")
def firecrawl_scrape(
    request: Request,
    body: FirecrawlScrapeRequest,
    _: None = Depends(verify_api_key),
) -> dict:
    settings = get_settings()
    if not settings.firecrawl_api_key:
        raise HTTPException(status_code=503, detail="firecrawl_api_key_missing")
    base = settings.firecrawl_base_url.rstrip("/")
    if not base.endswith(("/v1", "/v2")):
        base = base + "/v1"
    target = f"{base}/scrape"
    payload: dict = {
        "url": body.url,
        "formats": body.formats,
        "onlyMainContent": body.only_main_content,
    }
    headers = {
        "authorization": f"Bearer {settings.firecrawl_api_key}",
        "content-type": "application/json",
    }
    try:
        with _httpx_for_fc.Client(timeout=body.timeout_seconds) as client:
            r = client.post(target, headers=headers, json=payload)
        if r.status_code >= 500:
            raise HTTPException(status_code=502, detail=f"firecrawl_upstream_{r.status_code}: {r.text[:200]}")
        data = r.json()
        return {"ok": True, "result": data, "status": r.status_code}
    except _httpx_for_fc.TimeoutException as e:
        raise HTTPException(status_code=504, detail=f"firecrawl_timeout: {e}") from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("firecrawl proxy error: {}", e)
        raise HTTPException(status_code=500, detail=f"firecrawl_proxy_error: {e}") from e
