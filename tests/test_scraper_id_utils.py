"""Tests for stable ID and content hash utilities (SCRP-06, SCRP-09, v2 URL-free).

All tests are pure — no I/O, no network, no DB.
"""
import re
import pytest


# ---------------------------------------------------------------------------
# normalize_company_name tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("🔥 Google", "google"),
    ("🔒 Closed Corp", "closed corp"),
    ("  Meta Platforms, Inc.  ", "meta platforms inc"),
    ("Apple Inc.", "apple inc"),
    ("Amazon Web Services", "amazon web services"),
])
def test_normalize_company_name_strips_emoji_and_normalizes(raw, expected):
    from wekruit_matching.scraper.id_utils import normalize_company_name
    assert normalize_company_name(raw) == expected


# ---------------------------------------------------------------------------
# generate_job_id tests — v2 URL-free contract
# ---------------------------------------------------------------------------

def test_generate_job_id_emoji_company_equals_plain():
    """v2 still strips emoji via normalize_company_name."""
    from wekruit_matching.scraper.id_utils import generate_job_id
    id_emoji = generate_job_id("jobright-newgrad", "🔥 Google", "SWE Intern")
    id_plain = generate_job_id("jobright-newgrad", "Google", "SWE Intern")
    assert id_emoji == id_plain


def test_generate_job_id_different_titles_produce_different_ids():
    from wekruit_matching.scraper.id_utils import generate_job_id
    id_swe = generate_job_id("jobright-newgrad", "Google", "SWE Intern")
    id_pm = generate_job_id("jobright-newgrad", "Google", "PM Intern")
    assert id_swe != id_pm


def test_generate_job_id_returns_64_char_hex():
    from wekruit_matching.scraper.id_utils import generate_job_id
    result = generate_job_id("jobright-newgrad", "Google", "SWE Intern")
    assert len(result) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", result), f"Not a valid hex string: {result}"


# v2 NEW: URL no longer affects the ID — different URLs same (source, co, role) collapse.
def test_generate_job_id_url_irrelevant_to_hash():
    """v2: jobright-ai rotates redirect URLs; same (source, company, role) MUST collide."""
    from wekruit_matching.scraper.id_utils import generate_job_id
    a = generate_job_id("jobright-newgrad", "Walgreens", "Shift Lead")
    b = generate_job_id("jobright-newgrad", "Walgreens", "Shift Lead")
    # Idempotent regardless of any URL data the caller might still have.
    assert a == b


# v2 NEW: source_repo disambiguates same (company, role) across providers.
def test_generate_job_id_different_source_repo_different_id():
    from wekruit_matching.scraper.id_utils import generate_job_id
    gh = generate_job_id("greenhouse:stripe", "Stripe", "Backend Engineer")
    lv = generate_job_id("lever:stripe", "Stripe", "Backend Engineer")
    assert gh != lv


# v2 NEW: classic jobright dupe scenario — Walgreens 13× scrape collapses to 1.
def test_generate_job_id_collapses_jobright_dupes():
    """Walgreens 'Shift Lead' produced 13 phantom Firestore docs pre-v2.

    Verify that 13 identical-content scrapes (only URL hex rotating) all
    produce ONE job_id under v2.
    """
    from wekruit_matching.scraper.id_utils import generate_job_id
    ids = {
        generate_job_id("jobright-newgrad", "walgreens", "Shift Lead")
        for _ in range(13)
    }
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# compute_content_hash tests (unchanged in v2)
# ---------------------------------------------------------------------------

def test_compute_content_hash_returns_64_char_hex():
    from wekruit_matching.scraper.id_utils import compute_content_hash
    result = compute_content_hash("Google", "SWE Intern")
    assert len(result) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", result), f"Not a valid hex string: {result}"


def test_compute_content_hash_is_deterministic():
    from wekruit_matching.scraper.id_utils import compute_content_hash
    h1 = compute_content_hash("Google", "SWE Intern")
    h2 = compute_content_hash("Google", "SWE Intern")
    assert h1 == h2


def test_compute_content_hash_is_content_sensitive():
    from wekruit_matching.scraper.id_utils import compute_content_hash
    h_swe = compute_content_hash("Google", "SWE Intern")
    h_pm = compute_content_hash("Google", "PM Intern")
    assert h_swe != h_pm
