"""Embedding worker: reads enriched unembedded jobs, calls embed_text, writes vectors.

Gating: embedded_at IS NULL AND enriched_at IS NOT NULL
When a job's content_hash changes, upsert.py clears enriched_at — so changed jobs
re-enter the enrichment queue first; embedding follows automatically after re-enrichment.

Per-job failure isolation: an embed_text() exception logs a warning and increments
the failed counter — the batch continues to the next job.
"""
from datetime import datetime, timezone

import psycopg
from loguru import logger
from pgvector.psycopg import register_vector

from wekruit_matching.embedding.embedder import (
    EMBEDDING_MODEL,
    compose_embedding_text,
    embed_text,
)
from wekruit_matching.models.job import Job


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def embed_pending(conn: psycopg.Connection) -> dict[str, int]:
    """Embed all enriched-but-unembedded active jobs and write vectors to the DB.

    Query: WHERE embedded_at IS NULL AND enriched_at IS NOT NULL AND status = 'active'

    For each job:
      1. Compose embedding text: "{role_title} at {company_name}. Skills: {skills}"
      2. Call embed_text() — raises on permanent failure
      3. UPDATE jobs SET embedding, embedding_model, embedded_at
      4. commit() after each successful write

    Returns {"embedded": N, "failed": M, "skipped": 0}
    """
    register_vector(conn)

    rows = conn.execute(
        """
        SELECT job_id, source_repo, company_name, role_title, location_raw,
               required_skills, content_hash
        FROM jobs
        WHERE embedded_at IS NULL
          AND enriched_at IS NOT NULL
          AND status = 'active'
        ORDER BY first_seen_at ASC
        """
    ).fetchall()

    if not rows:
        logger.info("No jobs to embed — nothing to do")
        return {"embedded": 0, "failed": 0, "skipped": 0}

    logger.info("Found {} enriched job(s) to embed", len(rows))
    embedded = failed = 0

    for row in rows:
        job = Job(
            job_id=row["job_id"],
            source_repo=row["source_repo"],
            company_name=row["company_name"],
            role_title=row["role_title"],
            location_raw=row["location_raw"] or "",
            required_skills=list(row["required_skills"] or []),
            content_hash=row["content_hash"],
        )
        try:
            text = compose_embedding_text(job)
            vector = embed_text(text)
            conn.execute(
                """
                UPDATE jobs SET
                    embedding       = %(embedding)s,
                    embedding_model = %(embedding_model)s,
                    embedded_at     = %(embedded_at)s
                WHERE job_id = %(job_id)s
                """,
                {
                    "job_id": job.job_id,
                    "embedding": vector,
                    "embedding_model": EMBEDDING_MODEL,
                    "embedded_at": _utcnow(),
                },
            )
            conn.commit()
            embedded += 1
            logger.debug("Embedded {}", job.job_id[:8])
        except Exception as exc:
            failed += 1
            logger.warning("Failed to embed job {}: {}", job.job_id[:8], exc)
            # Roll back any partial writes so the connection is clean for the next job
            conn.rollback()
            # Continue to next job — per-job isolation

    logger.info("Embedding complete: embedded={} failed={}", embedded, failed)
    return {"embedded": embedded, "failed": failed, "skipped": 0}
