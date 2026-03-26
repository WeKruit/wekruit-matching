"""Feedback handler: record user reactions and update profile state.

record_feedback(user_id, job_id, reaction, conn) — public API

Internal flow for each call:
  1. INSERT INTO feedback (idempotent via ON CONFLICT DO NOTHING)
  2. SELECT job's company_name and embedding
  3. Branch on reaction:
     - like:    append to liked_companies; blend affinity_embedding (70/30)
     - dislike: append to disliked_companies
     - applied: no profile update (feedback row already inserted)

Commit is the caller's responsibility — matches the pattern in
enrichment/worker.py and embedding/worker.py.
"""
from __future__ import annotations

import psycopg
import numpy as np
from loguru import logger
from pgvector.psycopg import register_vector

from wekruit_matching.db.connection import get_connection
from wekruit_matching.models.feedback import ReactionType


def record_feedback(
    user_id: str,
    job_id: str,
    reaction: str | ReactionType,
    conn: psycopg.Connection | None = None,
) -> None:
    """Record a user's reaction to a job listing and update their profile.

    Args:
        user_id:  Opaque user identifier — must match a row in user_profiles.
        job_id:   Job identifier — must match a row in jobs.
        reaction: "like", "dislike", or "applied" (or ReactionType enum value).
        conn:     Optional psycopg3 connection. If None, get_connection() is used.

    Returns:
        None. Caller is responsible for committing the transaction.
    """
    if conn is not None:
        _run(user_id, job_id, reaction, conn)
        # Caller owns the connection — caller commits.
    else:
        with get_connection() as _conn:
            _run(user_id, job_id, reaction, _conn)
            _conn.commit()  # We own this connection — commit before pool returns it.


def _run(
    user_id: str,
    job_id: str,
    reaction: str | ReactionType,
    conn: psycopg.Connection,
) -> None:
    """Execute the feedback recording logic on the provided connection."""
    # Normalize reaction to string value for DB storage and branching
    if isinstance(reaction, ReactionType):
        reaction_str = reaction.value
    else:
        reaction_str = str(reaction)

    # Register pgvector codec for this connection so vector columns are usable
    register_vector(conn)

    # ------------------------------------------------------------------
    # Step 0: Ensure user_profiles row exists (lazy creation for VALET users)
    # ------------------------------------------------------------------
    conn.execute(
        """
        INSERT INTO user_profiles (user_id, skills, liked_companies, disliked_companies, updated_at)
        VALUES (%s, '{}', '{}', '{}', NOW())
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
    )

    # ------------------------------------------------------------------
    # Step 1: Insert feedback row (idempotent)
    # ------------------------------------------------------------------
    # TODO(W8): ON CONFLICT DO NOTHING has no effect without a unique constraint on
    # (user_id, job_id). A DB migration adding UNIQUE(user_id, job_id) to the feedback
    # table is required to make duplicate-suppression work correctly.
    conn.execute(
        """
        INSERT INTO feedback (user_id, job_id, reaction, recorded_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT DO NOTHING
        """,
        (user_id, job_id, reaction_str),
    )

    # ------------------------------------------------------------------
    # Step 2: Fetch job's company_name and embedding
    # ------------------------------------------------------------------
    cursor = conn.execute(
        "SELECT company_name, embedding FROM jobs WHERE job_id = %s",
        (job_id,),
    )
    job_row = cursor.fetchone()
    if job_row is None:
        logger.warning(
            "record_feedback: job_id={} not found — skipping profile update",
            job_id,
        )
        return

    company_name = job_row["company_name"]
    job_embedding = job_row["embedding"]

    # ------------------------------------------------------------------
    # Step 3: Branch on reaction
    # ------------------------------------------------------------------
    if reaction_str == ReactionType.LIKE.value:
        _handle_like(user_id, company_name, job_embedding, conn)

    elif reaction_str == ReactionType.DISLIKE.value:
        _handle_dislike(user_id, company_name, conn)

    # "applied" — feedback row inserted, no profile update needed


_COMPANY_ARRAY_CAP = 100


def _handle_like(
    user_id: str,
    company_name: str,
    job_embedding: list[float] | None,
    conn: psycopg.Connection,
) -> None:
    """Append company to liked_companies and update affinity_embedding."""
    # a. Append company_name to liked_companies, capped at _COMPANY_ARRAY_CAP entries.
    # If the array already has _COMPANY_ARRAY_CAP entries, drop the oldest before appending.
    conn.execute(
        """
        UPDATE user_profiles
        SET liked_companies = array_append(
                CASE
                    WHEN array_length(liked_companies, 1) >= %s
                    THEN liked_companies[2:]
                    ELSE liked_companies
                END,
                %s
            ),
            updated_at = NOW()
        WHERE user_id = %s
        """,
        (_COMPANY_ARRAY_CAP, company_name, user_id),
    )

    # b. Update affinity_embedding only if job has an embedding
    if job_embedding is None:
        return

    # Fetch current affinity
    affinity_cursor = conn.execute(
        "SELECT affinity_embedding FROM user_profiles WHERE user_id = %s",
        (user_id,),
    )
    profile_row = affinity_cursor.fetchone()
    current_affinity = profile_row["affinity_embedding"] if profile_row else None

    if current_affinity is None:
        # First like — set affinity directly to the job's embedding
        # Job embeddings from OpenAI are already unit-norm; no normalization needed
        new_affinity = list(job_embedding)
    else:
        # Subsequent like — blend 70% existing + 30% new signal, then re-normalize
        existing = np.array(current_affinity, dtype=np.float64)
        new_signal = np.array(job_embedding, dtype=np.float64)
        blended = 0.7 * existing + 0.3 * new_signal
        norm = np.linalg.norm(blended)
        new_affinity = (blended / (norm + 1e-9)).tolist()

    conn.execute(
        """
        UPDATE user_profiles
        SET affinity_embedding = %s::vector,
            updated_at = NOW()
        WHERE user_id = %s
        """,
        (new_affinity, user_id),
    )


def _handle_dislike(
    user_id: str,
    company_name: str,
    conn: psycopg.Connection,
) -> None:
    """Append company to disliked_companies, capped at _COMPANY_ARRAY_CAP entries."""
    conn.execute(
        """
        UPDATE user_profiles
        SET disliked_companies = array_append(
                CASE
                    WHEN array_length(disliked_companies, 1) >= %s
                    THEN disliked_companies[2:]
                    ELSE disliked_companies
                END,
                %s
            ),
            updated_at = NOW()
        WHERE user_id = %s
        """,
        (_COMPANY_ARRAY_CAP, company_name, user_id),
    )
