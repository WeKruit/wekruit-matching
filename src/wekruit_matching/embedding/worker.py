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
    EMBED_BATCH_SIZE,
    EMBEDDING_MODEL,
    compose_embedding_text,
    embed_text,
    embed_texts,
)
from wekruit_matching.models.job import Job


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EmbeddingModelMismatchError(RuntimeError):
    """Raised when DB-stored embeddings use a different model than the running config.

    Mixing vectors from different OpenAI embedding models in the same column is
    silently wrong: cosine distances become incomparable, matching scores drift,
    and there is no in-band signal to the matching engine that the data is bad.
    The right behaviour is fail-fast at the next embed call — operators get an
    explicit "you changed EMBEDDING_MODEL but haven't migrated the column"
    error instead of subtly-broken match results.
    """


def assert_embedding_model_consistency(conn: psycopg.Connection) -> None:
    """Fail loudly if DB-stored embeddings use a different model than the running config.

    Runs a single SELECT DISTINCT against ``embedding_model`` for rows with a
    non-NULL embedding. NULL ``embedding_model`` rows are tolerated (they
    pre-date the column or were written before the model was stamped) — only
    explicit-string mismatches raise.

    Idempotent: read-only check. Safe to call on every embed_pending invocation
    without side effects.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT embedding_model
        FROM jobs
        WHERE embedding IS NOT NULL
          AND embedding_model IS NOT NULL
          AND embedding_model <> ''
        """
    ).fetchall()
    if not rows:
        return
    stored = {row["embedding_model"] for row in rows}
    if stored == {EMBEDDING_MODEL}:
        return
    raise EmbeddingModelMismatchError(
        f"DB embedding_model={sorted(stored)} but running config EMBEDDING_MODEL={EMBEDDING_MODEL}. "
        "Mixing embedding models produces incomparable vectors. Migrate the column or revert config."
    )


def embed_pending(conn: psycopg.Connection) -> dict[str, int]:
    """Embed all enriched-but-unembedded active jobs and write vectors to the DB.

    Query: WHERE embedded_at IS NULL AND enriched_at IS NOT NULL AND status = 'active'

    Jobs are embedded in BATCHES (one OpenAI request per ``EMBED_BATCH_SIZE``
    inputs) rather than one request per job — the embeddings endpoint accepts
    many inputs per call, and the per-job serial pattern was the drain
    bottleneck. Order is preserved, so each job still receives its own correct
    vector. If a batch request fails after retries, that batch falls back to
    per-job embedding so one bad input cannot sink its whole batch.

    For each job (within a batch):
      1. Compose embedding text: "{role_title} at {company_name}. Skills: {skills}"
      2. Obtain its vector from the batch (or per-job fallback)
      3. UPDATE jobs SET embedding, embedding_model, embedded_at
      4. commit() after each successful write

    Returns {"embedded": N, "failed": M, "skipped": 0}
    """
    register_vector(conn)
    # Matching-quality launch blocker (2026-05-20): fail fast on model drift
    # before computing any new vectors against a contaminated column.
    assert_embedding_model_consistency(conn)

    # Matching-quality launch blocker (Track D, 2026-05-20):
    # enriched_at IS NOT NULL only signals "enrichment ran" — it does NOT
    # signal "enrichment produced usable signal". A job whose JD fetch failed
    # gracefully (Firecrawl 500 / Workday SPA) and whose skill extraction
    # therefore yielded [] still gets enriched_at stamped. Without the JD +
    # skills gate below, we'd compute an embedding from
    # "{title} at {company}. Skills: " — a near-useless title-only vector
    # that would then sync to Firestore active and ride into the matching
    # pool. The result: 6,888 active docs with NULL JD and 2,425 zombie
    # active docs with both NULL JD and empty skills. Cap that here.
    rows = conn.execute(
        """
        SELECT job_id, source_repo, company_name, role_title, location_raw,
               required_skills, content_hash
        FROM jobs
        WHERE embedded_at IS NULL
          AND enriched_at IS NOT NULL
          AND status = 'active'
          AND job_description IS NOT NULL
          AND length(job_description) >= 200
          AND required_skills IS NOT NULL
          AND cardinality(required_skills) > 0
        ORDER BY first_seen_at ASC
        LIMIT 3000
        """
    ).fetchall()

    if not rows:
        logger.info("No jobs to embed — nothing to do")
        return {"embedded": 0, "failed": 0, "skipped": 0}

    logger.info("Found {} enriched job(s) to embed", len(rows))
    embedded = failed = 0

    def _write_vector(job: Job, vector: list[float]) -> None:
        """Persist one job's vector + embedded_at, committing immediately.

        Kept per-job (not a bulk UPDATE) so the embedded_at write/commit
        semantics — and crash-resumability of the drain — are identical to the
        pre-batch worker.
        """
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

    def _embed_one(job: Job, text: str) -> bool:
        """Embed + persist a single job. Returns True on success, False on failure.

        Per-job isolation: any exception (embed or write) rolls back and is
        swallowed so the rest of the queue proceeds.
        """
        nonlocal embedded, failed
        try:
            vector = embed_text(text)
            _write_vector(job, vector)
            embedded += 1
            logger.debug("Embedded {}", job.job_id[:8])
            return True
        except Exception as exc:
            failed += 1
            logger.warning("Failed to embed job {}: {}", job.job_id[:8], exc)
            conn.rollback()
            return False

    # Build the full (job, text) list once, preserving SELECT order so the
    # i-th composed text always belongs to the i-th job — this is what keeps
    # the job_id<->vector mapping correct across batching.
    jobs: list[Job] = []
    texts: list[str] = []
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
        jobs.append(job)
        texts.append(compose_embedding_text(job))

    # Embed in batches of EMBED_BATCH_SIZE — one OpenAI request per batch.
    for start in range(0, len(jobs), EMBED_BATCH_SIZE):
        batch_jobs = jobs[start : start + EMBED_BATCH_SIZE]
        batch_texts = texts[start : start + EMBED_BATCH_SIZE]
        try:
            vectors = embed_texts(batch_texts)
        except Exception as exc:
            # Whole-batch API failure after retries — degrade to per-job so a
            # single offending input doesn't drop the entire batch. This is
            # also the path the per-job isolation tests exercise.
            logger.warning(
                "Batch embed failed ({} job(s)): {} — falling back to per-job",
                len(batch_jobs),
                exc,
            )
            for job, text in zip(batch_jobs, batch_texts):
                _embed_one(job, text)
            continue

        # Count-parity guard: embed_texts documents a strict 1:1 contract
        # (result[i] is the vector for texts[i]). If the response is short
        # (a dropped/partial entry), the order-aligned zip() below would
        # SILENTLY truncate — trailing jobs would be neither embedded nor
        # counted as failed, vanishing from the matchable corpus with no
        # signal (the silent-corruption class of "matches were fine yesterday,
        # wrong today"). Rather than write a misaligned batch, recover the same
        # way we do on a batch exception: fall back to per-job embedding (which
        # is inherently 1:1) so every job still gets its own correct vector.
        if len(vectors) != len(batch_jobs):
            logger.warning(
                "Batch embed returned {} vector(s) for {} input(s) — count "
                "mismatch; falling back to per-job to avoid misalignment",
                len(vectors),
                len(batch_jobs),
            )
            for job, text in zip(batch_jobs, batch_texts):
                _embed_one(job, text)
            continue

        # Batch succeeded — write each job's own vector (order-aligned).
        for job, vector in zip(batch_jobs, vectors):
            try:
                _write_vector(job, vector)
                embedded += 1
                logger.debug("Embedded {}", job.job_id[:8])
            except Exception as exc:
                failed += 1
                logger.warning("Failed to write embedding for job {}: {}", job.job_id[:8], exc)
                conn.rollback()

    logger.info("Embedding complete: embedded={} failed={}", embedded, failed)
    return {"embedded": embedded, "failed": failed, "skipped": 0}
