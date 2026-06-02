"""Single source of truth for "is a job matching-ready?" (reliability audit
2026-06-01, rank 3).

The recurring matchable-drop incidents were all the STAMP_WITHOUT_VERIFY class:
each stage re-derived "has a usable JD" / "has skills" with its OWN inline
literal (``>= 200``, ``cardinality(skills) > 0``), and any stage that drifted
weaker than its downstream consumer created a permanent lockout seam. This module
defines that predicate ONCE so every writer's stamp decision and every reader's
gate reference the identical rule.

Python callers import these helpers. SQL gates that cannot import a Python
constant reference ``MATCHABLE_SQL_PREDICATE`` / ``USABLE_JD_SQL`` so the literal
lives in one place and drift becomes a diff in this file.
"""
from __future__ import annotations

from collections.abc import Sized
from typing import Optional

# The minimum JD body length (chars, stripped) for a job description to be
# "usable" — i.e. long enough that the LLM can extract real skills rather than
# hallucinate from a title. Below this, skill extraction is unreliable, so a row
# must NOT be marked enriched/embedded as if it were complete.
MIN_JD_CHARS = 200


def jd_usable(jd: Optional[str]) -> bool:
    """True iff ``jd`` is a real, extractable job-description body (>= MIN_JD_CHARS
    after stripping). NULL/empty/whitespace/thin -> False."""
    return bool(jd) and len(jd.strip()) >= MIN_JD_CHARS


def has_skills(skills: Optional[Sized]) -> bool:
    """True iff ``skills`` is a non-empty collection."""
    return bool(skills) and len(skills) > 0


def is_matchable_ready(jd: Optional[str], skills: Optional[Sized]) -> bool:
    """The canonical matchable-readiness predicate: a usable JD AND >=1 skill.

    A writer must only stamp a terminal/done flag (enriched_at, a real
    jd_fetch_source, embedded_at) when this holds; a reader gate (embed, sync,
    health) admits a row only when this holds. Equal predicate on both sides =
    no lockout seam.
    """
    return jd_usable(jd) and has_skills(skills)


# SQL fragments for the gates that run in Postgres (cannot import the Python
# constant). Keep the literal here so the embed gate, sync gate, and health gate
# all reference ONE definition; a change to MIN_JD_CHARS must be mirrored here
# (guarded by tests/test_readiness.py).
USABLE_JD_SQL = "job_description IS NOT NULL AND length(job_description) >= 200"
HAS_SKILLS_SQL = "required_skills IS NOT NULL AND cardinality(required_skills) > 0"
# The full matchable predicate shared by embed_pending, job_sync._fetch_active_jobs,
# and health_gate.matchable_corpus (status/embedding clauses added by each caller).
MATCHABLE_SQL_PREDICATE = f"({USABLE_JD_SQL}) AND ({HAS_SKILLS_SQL})"
