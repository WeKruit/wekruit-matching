"""Tests for the shared matchable-readiness module (reliability audit rank 3)
+ a guard test that no stage re-introduces a divergent inline JD-length floor.
"""
from __future__ import annotations

import re
from pathlib import Path

from wekruit_matching.enrichment import readiness as r


# --- helper semantics -------------------------------------------------------

def test_min_jd_chars_is_200():
    assert r.MIN_JD_CHARS == 200


def test_jd_usable_boundary():
    assert r.jd_usable("x" * 200) is True
    assert r.jd_usable("x" * 199) is False
    assert r.jd_usable("x" * 201) is True


def test_jd_usable_strips_whitespace():
    # 199 real chars padded with whitespace must still be "thin".
    assert r.jd_usable("  " + "x" * 199 + "   \n") is False
    assert r.jd_usable("  " + "x" * 200 + "   \n") is True


def test_jd_usable_none_and_empty():
    assert r.jd_usable(None) is False
    assert r.jd_usable("") is False
    assert r.jd_usable("   ") is False


def test_has_skills():
    assert r.has_skills(["python"]) is True
    assert r.has_skills([]) is False
    assert r.has_skills(None) is False


def test_is_matchable_ready_requires_both():
    long_jd = "x" * 250
    assert r.is_matchable_ready(long_jd, ["python"]) is True
    assert r.is_matchable_ready(long_jd, []) is False          # no skills
    assert r.is_matchable_ready("short", ["python"]) is False  # thin JD
    assert r.is_matchable_ready(None, None) is False


def test_classifier_constant_sources_from_readiness():
    from wekruit_matching.enrichment.classifier import MIN_JD_CHARS_FOR_SKILLS
    assert MIN_JD_CHARS_FOR_SKILLS == r.MIN_JD_CHARS


def test_sql_predicate_uses_the_same_threshold():
    # The SQL fragment must carry the same literal as MIN_JD_CHARS so the
    # in-Postgres gates cannot silently drift from the Python helpers.
    assert str(r.MIN_JD_CHARS) in r.USABLE_JD_SQL
    assert "cardinality(required_skills) > 0" in r.HAS_SKILLS_SQL


# --- drift guard ------------------------------------------------------------

def test_no_rogue_jd_length_literal_outside_readiness():
    """Every JD-length floor must reference MIN_JD_CHARS (Python) or live in
    readiness.py (SQL). Fail if a NEW bare ``length(job_description) >= 200`` /
    ``< 200`` literal appears in Python expressions outside the allowed set, so a
    future stage cannot silently re-introduce a divergent threshold (the rank-3
    invariant).

    NOTE: SQL string gates (embed_pending, job_sync, health_gate, dead_backfill)
    legitimately carry the literal inside a SQL string; those files are on the
    allowlist and tracked to be migrated to readiness.MATCHABLE_SQL_PREDICATE
    incrementally. This guard pins that the list does not GROW.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "wekruit_matching"
    # Files permitted to contain a 200 JD-length literal today (SQL gates +
    # the definition itself). Adding a new file here must be a conscious choice.
    allow = {
        "enrichment/readiness.py",
        "enrichment/classifier.py",          # aliases MIN_JD_CHARS
        "pipeline/health_gate.py",           # SQL gates
        "pipeline/job_sync.py",              # SQL gate
        "pipeline/dead_backfill.py",         # SQL gate
        "embedding/worker.py",               # SQL gate
        "pipeline/ats_enricher.py",          # data_quality_score banding
        "scraper/yc.py",                     # unrelated 200-char field check
    }
    pattern = re.compile(r"length\(job_description\)\s*[<>]=?\s*200|len\([^)]*\)\s*[<>]=?\s*200")
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        rel = path.relative_to(src).as_posix()
        if rel in allow:
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(line) and "200" in line:
                offenders.append(f"{rel}:{i}: {stripped}")
    assert not offenders, (
        "New JD-length literal(s) outside readiness.py — route through "
        "readiness.jd_usable / MIN_JD_CHARS instead:\n" + "\n".join(offenders)
    )
