"""Tests for stable ID and content hash utilities (SCRP-06, SCRP-09).

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
    """Test 1 & 2 & 3: normalize_company_name strips emoji, punctuation, collapses whitespace."""
    from wekruit_matching.scraper.id_utils import normalize_company_name
    assert normalize_company_name(raw) == expected


# ---------------------------------------------------------------------------
# generate_job_id tests
# ---------------------------------------------------------------------------

def test_generate_job_id_emoji_company_equals_plain(emoji_company="🔥 Google"):
    """Test 4: Emoji and plain company name produce the same job_id."""
    from wekruit_matching.scraper.id_utils import generate_job_id
    id_emoji = generate_job_id("🔥 Google", "SWE Intern", "https://simplify.jobs/x")
    id_plain = generate_job_id("Google", "SWE Intern", "https://simplify.jobs/x")
    assert id_emoji == id_plain, f"Emoji version '{id_emoji}' != plain version '{id_plain}'"


def test_generate_job_id_different_titles_produce_different_ids():
    """Test 5: Different role titles produce different IDs (same company, same URL)."""
    from wekruit_matching.scraper.id_utils import generate_job_id
    id_swe = generate_job_id("Google", "SWE Intern", "https://a.com")
    id_pm = generate_job_id("Google", "PM Intern", "https://a.com")
    assert id_swe != id_pm


def test_generate_job_id_returns_64_char_hex():
    """Test 6: generate_job_id returns a 64-character lowercase hex string."""
    from wekruit_matching.scraper.id_utils import generate_job_id
    result = generate_job_id("Google", "SWE Intern", "https://a.com")
    assert len(result) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", result), f"Not a valid hex string: {result}"


# ---------------------------------------------------------------------------
# compute_content_hash tests
# ---------------------------------------------------------------------------

def test_compute_content_hash_returns_64_char_hex():
    """Test 7: compute_content_hash returns a 64-char hex string."""
    from wekruit_matching.scraper.id_utils import compute_content_hash
    result = compute_content_hash("Google", "SWE Intern")
    assert len(result) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", result), f"Not a valid hex string: {result}"


def test_compute_content_hash_is_deterministic():
    """Test 8: compute_content_hash returns the same value for the same inputs."""
    from wekruit_matching.scraper.id_utils import compute_content_hash
    h1 = compute_content_hash("Google", "SWE Intern")
    h2 = compute_content_hash("Google", "SWE Intern")
    assert h1 == h2


def test_compute_content_hash_is_content_sensitive():
    """Test 9: Different role titles produce different content hashes."""
    from wekruit_matching.scraper.id_utils import compute_content_hash
    h_swe = compute_content_hash("Google", "SWE Intern")
    h_pm = compute_content_hash("Google", "PM Intern")
    assert h_swe != h_pm
