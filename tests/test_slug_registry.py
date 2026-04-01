"""Tests for the slug registry — no DB required.

The registry is loaded from bundled JSON files (no network calls). These tests
confirm that the data files are present, have the expected volume of entries,
and that lookups work correctly.

Run with: uv run pytest tests/test_slug_registry.py -v
"""
import pytest

from wekruit_matching.scraper.slug_registry import SlugRegistry, load_registry
from wekruit_matching.scraper.url_classifier import ATSTier


# ---------------------------------------------------------------------------
# Module-scope fixture — loads registry once for the entire test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> SlugRegistry:
    return load_registry()


# ---------------------------------------------------------------------------
# Loading tests
# ---------------------------------------------------------------------------


def test_load_registry_returns_registry(registry: SlugRegistry) -> None:
    """load_registry() must return a SlugRegistry instance."""
    assert isinstance(registry, SlugRegistry)


# ---------------------------------------------------------------------------
# Entry count tests — verify bundled data files have expected volume
# ---------------------------------------------------------------------------


def test_registry_greenhouse_entries(registry: SlugRegistry) -> None:
    """Greenhouse registry should have ≥100 entries (Feashliaa data: 4,516)."""
    count = len(registry._data.get("greenhouse", {}))
    assert count >= 100, f"Expected ≥100 greenhouse entries, got {count}"


def test_registry_lever_entries(registry: SlugRegistry) -> None:
    """Lever registry should have ≥50 entries."""
    count = len(registry._data.get("lever", {}))
    assert count >= 50, f"Expected ≥50 lever entries, got {count}"


def test_registry_ashby_entries(registry: SlugRegistry) -> None:
    """Ashby registry should have ≥50 entries."""
    count = len(registry._data.get("ashby", {}))
    assert count >= 50, f"Expected ≥50 ashby entries, got {count}"


def test_registry_workday_entries(registry: SlugRegistry) -> None:
    """Workday registry should have ≥100 entries."""
    count = len(registry._data.get("workday", {}))
    assert count >= 100, f"Expected ≥100 workday entries, got {count}"


# ---------------------------------------------------------------------------
# Lookup tests
# ---------------------------------------------------------------------------


def test_lookup_unknown_company_returns_none(registry: SlugRegistry) -> None:
    """lookup() for a nonexistent company must return None, not raise.

    'zzznomatch' is chosen deliberately — it does not appear as an exact or
    prefix-containment match in the Feashliaa data set.
    """
    result = registry.lookup("zzznomatch", ATSTier.GREENHOUSE)
    assert result is None


def test_lookup_all_ats_returns_dict(registry: SlugRegistry) -> None:
    """lookup_all_ats() must return a dict (empty is fine for uncommon names)."""
    result = registry.lookup_all_ats("stripe")
    assert isinstance(result, dict)


def test_normalize_is_case_insensitive(registry: SlugRegistry) -> None:
    """lookup() must be case-insensitive — 'Stripe' and 'stripe' must agree."""
    lower = registry.lookup("stripe", ATSTier.GREENHOUSE)
    upper = registry.lookup("Stripe", ATSTier.GREENHOUSE)
    assert lower == upper, (
        f"Case mismatch: lookup('stripe') → {lower!r}, lookup('Stripe') → {upper!r}"
    )
