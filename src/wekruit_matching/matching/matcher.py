"""Matching engine entry point: ANN retrieval + hard filters + scoring.

Fetches top-N * 4 ANN candidates from pgvector, applies hard filters in Python,
scores each with score_job(), and returns the top-N ranked list.

Public API:
    get_matches(profile, conn, top_n, openai_client) -> list[dict]
"""
from __future__ import annotations

import psycopg
import openai
from loguru import logger
from pgvector.psycopg import register_vector

from wekruit_matching.db.connection import get_connection
from wekruit_matching.embedding.embedder import embed_text, EMBEDDING_MODEL
from wekruit_matching.matching.filters import apply_hard_filters
from wekruit_matching.matching.scorer import score_job
from wekruit_matching.models.user_profile import UserProfile


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compose_query_text(profile: UserProfile) -> str:
    """Build the query text for embed_text from the user's skill list.

    Falls back to "software engineer" when skills list is empty (cold-start).
    """
    if profile.skills:
        return ", ".join(profile.skills)
    return "software engineer"


def _fetch_ann_candidates(
    conn: psycopg.Connection,
    query_embedding: list[float],
    limit: int,
) -> list[dict]:
    """Fetch ANN candidates from the jobs table using pgvector cosine similarity.

    Registers the pgvector type handler on the connection before executing the
    query, so the embedding column is decoded to a list[float] automatically.

    Args:
        conn: Active psycopg3 connection (dict_row factory assumed).
        query_embedding: 1536-dim user query vector.
        limit: Maximum number of ANN candidates to fetch (typically top_n * 4).

    Returns:
        List of job dicts ordered by embedding <=> query_embedding ascending
        (closest first). Each dict includes all job fields.
    """
    register_vector(conn)

    cursor = conn.execute(
        """
        SELECT
            job_id, source_repo, company_name, role_title, primary_url,
            location_raw, date_posted_raw, status, first_seen_at, last_seen_at,
            industry, company_size, required_skills, sponsorship,
            embedding, embedding_model
        FROM jobs
        WHERE status = 'active'
          AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (query_embedding, limit),
    )
    rows = cursor.fetchall()

    # rows are already dicts when dict_row factory is set on the connection.
    # Convert to plain Python dicts to decouple from psycopg row objects.
    result: list[dict] = []
    for row in rows:
        job = dict(row)
        # Ensure embedding is a list[float] rather than a pgvector type object
        if job.get("embedding") is not None and not isinstance(
            job["embedding"], list
        ):
            job["embedding"] = list(job["embedding"])
        result.append(job)

    return result


_JOB_TYPE_TO_REPO = {
    "intern": "Summer2026-Internships",
    "new_grad": "New-Grad-Positions",
}


def _fetch_recent_candidates(
    conn: psycopg.Connection,
    limit: int,
    job_type: str = "any",
) -> list[dict]:
    """Fallback: fetch recent active jobs when embeddings are not available.

    Applies job_type filter in SQL to avoid loading irrelevant rows.
    Returns jobs ordered by last_seen_at descending (most recently updated first).
    """
    repo = _JOB_TYPE_TO_REPO.get(job_type)
    if repo:
        cursor = conn.execute(
            """
            SELECT
                job_id, source_repo, company_name, role_title, primary_url,
                location_raw, date_posted_raw, status, first_seen_at, last_seen_at,
                industry, company_size, required_skills, sponsorship,
                embedding, embedding_model
            FROM jobs
            WHERE status = 'active' AND source_repo = %s
            ORDER BY last_seen_at DESC NULLS LAST
            LIMIT %s
            """,
            (repo, limit),
        )
    else:
        cursor = conn.execute(
            """
            SELECT
                job_id, source_repo, company_name, role_title, primary_url,
                location_raw, date_posted_raw, status, first_seen_at, last_seen_at,
                industry, company_size, required_skills, sponsorship,
                embedding, embedding_model
            FROM jobs
            WHERE status = 'active'
            ORDER BY last_seen_at DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
    return [dict(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_matches(
    profile: UserProfile,
    conn: psycopg.Connection | None = None,
    top_n: int = 30,
    openai_client: openai.OpenAI | None = None,
) -> list[dict]:
    """Return the top-N ranked job matches for a user profile.

    Pipeline:
      1. Determine query embedding (affinity bypass or embed_text).
      2. Fetch top_n * 4 ANN candidates from pgvector.
      3. Apply hard filters (job_type, sponsorship, location) in Python.
      4. Score each candidate with score_job().
      5. Sort descending by score.
      6. Return first top_n results.

    Args:
        profile: User preferences and feedback state.
        conn: Optional psycopg3 connection. When None, uses get_connection()
              context manager from the pool.
        top_n: Maximum number of results to return (default 30).
        openai_client: Optional OpenAI client for embed_text (test-injectable).
                       When None, embed_text uses its cached default client.

    Returns:
        List of dicts, each containing all job DB fields plus:
          - "score": float (composite match score, 0.0-1.0)
          - "signals": dict[str, float] with 7 signal values
        Sorted descending by "score". Length <= top_n.
    """
    # ------------------------------------------------------------------
    # Step 1: Determine query embedding (fallback to None if unavailable)
    # ------------------------------------------------------------------
    query_embedding: list[float] | None = None
    if profile.affinity_embedding is not None:
        query_embedding = profile.affinity_embedding
    else:
        try:
            query_text = _compose_query_text(profile)
            query_embedding = embed_text(query_text, client=openai_client)
        except Exception as e:
            logger.warning("Embedding failed, falling back to non-vector matching: {}", e)

    # ------------------------------------------------------------------
    # Step 2: ANN retrieval or recency fallback
    # ------------------------------------------------------------------
    ann_limit = top_n * 4

    def _run(active_conn: psycopg.Connection) -> list[dict]:
        if query_embedding is not None:
            ann_candidates = _fetch_ann_candidates(
                active_conn, query_embedding, ann_limit
            )
        else:
            ann_candidates = []

        # Fallback: if ANN returned nothing (no embeddings), fetch recent jobs
        if not ann_candidates:
            ann_candidates = _fetch_recent_candidates(
                active_conn, ann_limit, profile.preferred_job_type.value
            )

        # ------------------------------------------------------------------
        # Step 3: Hard filters (pure Python — no DB call)
        # ------------------------------------------------------------------
        filtered = apply_hard_filters(ann_candidates, profile)

        # ------------------------------------------------------------------
        # Step 4: Score each candidate
        # ------------------------------------------------------------------
        scored: list[dict] = []
        for job in filtered:
            score_result = score_job(job, profile, query_embedding or [])
            # Merge job fields + score/signals into one dict
            scored.append({**job, **score_result})

        # ------------------------------------------------------------------
        # Step 5: Sort descending by composite score
        # ------------------------------------------------------------------
        scored.sort(key=lambda d: d["score"], reverse=True)

        logger.info(
            "get_matches: {} candidates -> {} after filters -> {} returned",
            len(ann_candidates),
            len(filtered),
            min(top_n, len(scored)),
        )

        # ------------------------------------------------------------------
        # Step 6: Return top_n (strip embedding to avoid serialization issues)
        # ------------------------------------------------------------------
        results = scored[:top_n]
        for r in results:
            r.pop("embedding", None)
            r.pop("embedding_model", None)
        return results

    if conn is not None:
        return _run(conn)

    with get_connection() as managed_conn:
        return _run(managed_conn)
