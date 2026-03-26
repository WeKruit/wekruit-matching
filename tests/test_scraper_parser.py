"""Tests for the SimplifyJobs README parser.

Covers all edge cases from the SimplifyJobs format:
- Standard rows (Acme Corp)
- HTML multi-location cells (<details><summary> blocks)
- Continuation rows (↳) inheriting company from prior row
- Closed rows (🔒 in Company column) excluded from output
- Determinism: same input -> same output on repeated calls
"""
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def internships_content() -> bytes:
    return (FIXTURES / "internships_snapshot.md").read_bytes()


@pytest.fixture
def new_grad_content() -> bytes:
    return (FIXTURES / "new_grad_snapshot.md").read_bytes()


# ---------------------------------------------------------------------------
# Lazy import — parser doesn't exist yet during RED phase
# ---------------------------------------------------------------------------
def get_parse_readme():
    from wekruit_matching.scraper.parser import parse_readme  # noqa: PLC0415
    return parse_readme


# ---------------------------------------------------------------------------
# Test 1: parse_readme returns exactly 4 Job objects for internships fixture
#         (Acme Corp, Globex Data Science, Globex ML continuation, Initech)
#         🔒 Locked Inc must be excluded
# ---------------------------------------------------------------------------
def test_internships_returns_four_jobs(internships_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs = parse_readme(internships_content, "Summer2026-Internships")
    assert len(jobs) == 4, (
        f"Expected 4 jobs, got {len(jobs)}: {[j.company_name for j in jobs]}"
    )


# ---------------------------------------------------------------------------
# Test 2: Globex Data Science job has HTML-stripped location_raw
#         Must contain Austin, Seattle, Remote — NO raw HTML tags
# ---------------------------------------------------------------------------
def test_globex_location_has_no_html(internships_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs = parse_readme(internships_content, "Summer2026-Internships")
    globex_ds = next(
        (j for j in jobs if j.company_name == "globex" and "data science" in j.role_title.lower()),
        None,
    )
    assert globex_ds is not None, "Globex Data Science Intern job not found"
    assert "Austin, TX" in globex_ds.location_raw, f"Austin, TX missing from {globex_ds.location_raw!r}"
    assert "Seattle, WA" in globex_ds.location_raw, f"Seattle, WA missing from {globex_ds.location_raw!r}"
    assert "Remote" in globex_ds.location_raw, f"Remote missing from {globex_ds.location_raw!r}"
    assert "<details>" not in globex_ds.location_raw, "Raw <details> tag found in location_raw"
    assert "<br>" not in globex_ds.location_raw, "Raw <br> tag found in location_raw"
    assert "<summary>" not in globex_ds.location_raw, "Raw <summary> tag found in location_raw"
    assert "<strong>" not in globex_ds.location_raw, "Raw <strong> tag found in location_raw"


# ---------------------------------------------------------------------------
# Test 3: Continuation row (ML Intern ↳) has company_name == "globex"
#         (inherited from Globex parent, NOT "↳" or "")
# ---------------------------------------------------------------------------
def test_continuation_row_inherits_company(internships_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs = parse_readme(internships_content, "Summer2026-Internships")
    ml_intern = next(
        (j for j in jobs if "machine learning" in j.role_title.lower()),
        None,
    )
    assert ml_intern is not None, "Machine Learning Intern (continuation row) not found"
    assert ml_intern.company_name == "globex", (
        f"Continuation row should have company_name='globex', got {ml_intern.company_name!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: No job has company_name == "↳" or company_name == ""
# ---------------------------------------------------------------------------
def test_no_continuation_marker_as_company(internships_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs = parse_readme(internships_content, "Summer2026-Internships")
    bad = [j for j in jobs if j.company_name in ("\u21b3", "")]
    assert not bad, f"Found jobs with bad company_name: {[(j.company_name, j.role_title) for j in bad]}"


# ---------------------------------------------------------------------------
# Test 5: No job has 🔒 in company_name (lock rows must be excluded entirely)
# ---------------------------------------------------------------------------
def test_no_lock_emoji_in_company_name(internships_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs = parse_readme(internships_content, "Summer2026-Internships")
    locked = [j for j in jobs if "\U0001F512" in j.company_name]
    assert not locked, f"Found lock emoji in company_name: {[j.company_name for j in locked]}"


# ---------------------------------------------------------------------------
# Test 6: Acme Corp job_id and content_hash are both 64-char hex strings
# ---------------------------------------------------------------------------
def test_acme_job_has_valid_hashes(internships_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs = parse_readme(internships_content, "Summer2026-Internships")
    acme = next((j for j in jobs if "acme" in j.company_name.lower()), None)
    assert acme is not None, "Acme Corp job not found"
    assert re.fullmatch(r"[0-9a-f]{64}", acme.job_id), (
        f"job_id is not a 64-char hex string: {acme.job_id!r}"
    )
    assert acme.content_hash is not None, "content_hash should not be None"
    assert re.fullmatch(r"[0-9a-f]{64}", acme.content_hash), (
        f"content_hash is not a 64-char hex string: {acme.content_hash!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: Acme Corp job has source_repo == "Summer2026-Internships"
# ---------------------------------------------------------------------------
def test_acme_source_repo(internships_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs = parse_readme(internships_content, "Summer2026-Internships")
    acme = next((j for j in jobs if "acme" in j.company_name.lower()), None)
    assert acme is not None, "Acme Corp job not found"
    assert acme.source_repo == "Summer2026-Internships", (
        f"Expected source_repo='Summer2026-Internships', got {acme.source_repo!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: parse_readme called twice on same bytes returns identical job_ids
#         (deterministic — no random/time-based IDs)
# ---------------------------------------------------------------------------
def test_parse_readme_is_deterministic(internships_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs_first = parse_readme(internships_content, "Summer2026-Internships")
    jobs_second = parse_readme(internships_content, "Summer2026-Internships")
    assert len(jobs_first) == len(jobs_second), "Different job counts on second call"
    for j1, j2 in zip(jobs_first, jobs_second):
        assert j1.job_id == j2.job_id, (
            f"Non-deterministic job_id: first={j1.job_id!r}, second={j2.job_id!r}"
        )


# ---------------------------------------------------------------------------
# Test 9: New grad fixture parses 2 jobs; Piedpiper location_raw has no HTML
# ---------------------------------------------------------------------------
def test_new_grad_parses_correctly(new_grad_content: bytes) -> None:
    parse_readme = get_parse_readme()
    jobs = parse_readme(new_grad_content, "New-Grad-Positions")
    assert len(jobs) == 2, f"Expected 2 new grad jobs, got {len(jobs)}: {[j.company_name for j in jobs]}"

    piedpiper = next(
        (j for j in jobs if "piedpiper" in j.company_name.lower()),
        None,
    )
    assert piedpiper is not None, "Piedpiper job not found"
    assert "<" not in piedpiper.location_raw, (
        f"HTML tag found in Piedpiper location_raw: {piedpiper.location_raw!r}"
    )
    assert "New York, NY" in piedpiper.location_raw or "Remote" in piedpiper.location_raw, (
        f"Expected location content not found in: {piedpiper.location_raw!r}"
    )
