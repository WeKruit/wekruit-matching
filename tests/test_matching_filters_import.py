"""Temporary RED-phase import test for Task 1 (deleted after GREEN commit)."""
import pytest


def test_filters_module_importable():
    from wekruit_matching.matching.filters import (  # noqa: F401
        apply_hard_filters,
        LOCATION_ALIASES,
        normalize_location,
    )


def test_all_required_exports_present():
    import wekruit_matching.matching.filters as m
    assert hasattr(m, "apply_hard_filters")
    assert hasattr(m, "LOCATION_ALIASES")
    assert hasattr(m, "normalize_location")
    assert hasattr(m, "filter_by_job_type")
    assert hasattr(m, "filter_by_sponsorship")
    assert hasattr(m, "filter_by_location")
