"""Regression + gate tests for sponsorship-filter reliability.

Root cause being guarded: the sponsorship hard filter used to keep ONLY rows
where ``sponsorship is True`` when ``requires_sponsorship=True``. On the live
corpus ~80%+ of active jobs have ``sponsorship = NULL`` (unknown, not yet
enriched), so an international-student user (sponsorship_needed=true) had their
candidate pool silently collapsed to the ~10% of jobs explicitly flagged True —
the bulk of genuine matches (unknown sponsorship) were dropped as if they were
confirmed no-sponsorship.

Fix: treat unknown (None) as eligible. Drop ONLY rows explicitly known to NOT
sponsor (``sponsorship is False``).

This module also carries:
  * a runtime-gate test proving apply_hard_filters warns when the sponsorship
    filter wipes out (nearly) the whole pool, and
  * a small GOLDEN-SET eval (>=5 profiles -> expected hard-filter outcome) that
    acts as a regression gate so future filter changes that re-introduce the
    over-filtering are caught automatically.
"""

from __future__ import annotations

import pytest

from wekruit_matching.matching.filters import (
    apply_hard_filters,
    filter_by_sponsorship,
)
from wekruit_matching.models.user_profile import JobType, UserProfile

# ---------------------------------------------------------------------------
# Helpers (mirror the job-dict shape produced by the DB query)
# ---------------------------------------------------------------------------


def _job(job_id: str, sponsorship=None, source_repo="Summer2026-Internships",
         location_raw="Remote") -> dict:
    return {
        "job_id": job_id,
        "source_repo": source_repo,
        "company_name": "Acme",
        "role_title": "SWE Intern",
        "location_raw": location_raw,
        "sponsorship": sponsorship,
    }


def _profile(**kw) -> UserProfile:
    base = dict(user_id="u-test")
    base.update(kw)
    return UserProfile(**base)


# ---------------------------------------------------------------------------
# Core regression: unknown (None) sponsorship must be KEPT
# ---------------------------------------------------------------------------


def test_unknown_sponsorship_is_kept_when_required():
    jobs = [_job("t", True), _job("f", False), _job("u", None)]
    out = filter_by_sponsorship(jobs, requires_sponsorship=True)
    ids = {j["job_id"] for j in out}
    assert ids == {"t", "u"}, "unknown(None) must be kept; only explicit False dropped"


def test_only_explicit_false_dropped_when_required():
    jobs = [_job("f1", False), _job("f2", False)]
    assert filter_by_sponsorship(jobs, requires_sponsorship=True) == []


def test_no_requirement_keeps_everything():
    jobs = [_job("t", True), _job("f", False), _job("u", None)]
    assert filter_by_sponsorship(jobs, requires_sponsorship=False) == jobs


def test_apply_hard_filters_keeps_unknown_sponsorship():
    jobs = [_job("t", True), _job("f", False), _job("u", None)]
    out = apply_hard_filters(jobs, _profile(requires_sponsorship=True))
    ids = {j["job_id"] for j in out}
    assert ids == {"t", "u"}


def test_majority_unknown_corpus_not_collapsed():
    """Mirror the live ratio: ~83% unknown, ~11% True, ~6% False.

    Before the fix this returned ~11% of the corpus; after, it returns ~94%
    (everything except explicit False).
    """
    jobs = (
        [_job(f"u{i}", None) for i in range(83)]
        + [_job(f"t{i}", True) for i in range(11)]
        + [_job(f"f{i}", False) for i in range(6)]
    )
    out = apply_hard_filters(jobs, _profile(requires_sponsorship=True))
    assert len(out) == 94  # 83 unknown + 11 True kept; 6 False dropped
    assert all(j["sponsorship"] is not False for j in out)


# ---------------------------------------------------------------------------
# Runtime gate: warn when the sponsorship filter wipes out (nearly) everything
# ---------------------------------------------------------------------------


def test_gate_warns_when_all_candidates_filtered():
    """apply_hard_filters must emit a WARNING when the chain empties the pool."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        # All explicit-False -> sponsorship filter empties the pool -> must warn.
        jobs = [_job(f"f{i}", False) for i in range(5)]
        out = apply_hard_filters(jobs, _profile(requires_sponsorship=True))
    finally:
        logger.remove(sink_id)

    assert out == []
    assert any("eliminated ALL" in m for m in messages), (
        f"expected an 'eliminated ALL' warning, got: {messages}"
    )


def test_gate_warns_when_sponsorship_filter_removes_most():
    """Warn when sponsorship filter removes >90% of an otherwise-populated pool."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        # 1 kept (unknown) out of 20 -> 95% removed -> must warn (but not empty).
        jobs = [_job("u", None)] + [_job(f"f{i}", False) for i in range(19)]
        out = apply_hard_filters(jobs, _profile(requires_sponsorship=True))
    finally:
        logger.remove(sink_id)

    assert len(out) == 1
    assert any("Sponsorship hard filter removed" in m for m in messages), (
        f"expected a sponsorship-removal warning, got: {messages}"
    )


# ---------------------------------------------------------------------------
# GOLDEN-SET eval gate: >=5 profiles -> expected surviving-id set
# ---------------------------------------------------------------------------

GOLDEN_CORPUS = [
    _job("intern_unknown", sponsorship=None, source_repo="Summer2026-Internships"),
    _job("intern_true", sponsorship=True, source_repo="Summer2026-Internships"),
    _job("intern_false", sponsorship=False, source_repo="Summer2026-Internships"),
    _job("newgrad_unknown", sponsorship=None, source_repo="New-Grad-Positions"),
    _job("newgrad_true", sponsorship=True, source_repo="New-Grad-Positions"),
    _job("newgrad_false", sponsorship=False, source_repo="New-Grad-Positions"),
]

ALL_IDS = {j["job_id"] for j in GOLDEN_CORPUS}

# (label, profile_kwargs, expected_surviving_ids)
GOLDEN_CASES = [
    (
        "no_constraints_keeps_all",
        dict(),
        ALL_IDS,
    ),
    (
        "sponsorship_required_drops_only_explicit_false",
        dict(requires_sponsorship=True),
        {"intern_unknown", "intern_true", "newgrad_unknown", "newgrad_true"},
    ),
    (
        "intern_only_keeps_all_intern_repos",
        dict(preferred_job_type=JobType.INTERN),
        {"intern_unknown", "intern_true", "intern_false"},
    ),
    (
        "newgrad_only_keeps_all_newgrad_repos",
        dict(preferred_job_type=JobType.NEW_GRAD),
        {"newgrad_unknown", "newgrad_true", "newgrad_false"},
    ),
    (
        "intern_and_sponsorship_required",
        dict(preferred_job_type=JobType.INTERN, requires_sponsorship=True),
        {"intern_unknown", "intern_true"},
    ),
    (
        "newgrad_and_sponsorship_required",
        dict(preferred_job_type=JobType.NEW_GRAD, requires_sponsorship=True),
        {"newgrad_unknown", "newgrad_true"},
    ),
]


@pytest.mark.parametrize(
    "label,profile_kwargs,expected", GOLDEN_CASES,
    ids=[c[0] for c in GOLDEN_CASES],
)
def test_golden_set_hard_filter(label, profile_kwargs, expected):
    out = apply_hard_filters(GOLDEN_CORPUS, _profile(**profile_kwargs))
    got = {j["job_id"] for j in out}
    assert got == expected, f"{label}: expected {expected}, got {got}"


def test_golden_set_has_at_least_five_profiles():
    assert len(GOLDEN_CASES) >= 5
