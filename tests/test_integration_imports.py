"""Integration smoke tests: verifies the public API surface of wekruit_matching.

All tests are pure import/introspection — no DB calls, no network access.
"""
import inspect

import wekruit_matching
from wekruit_matching import get_matches, record_feedback, __version__


def test_public_imports() -> None:
    """Public symbols are importable without error."""
    # Import was performed at module level; reaching here means it succeeded.
    assert get_matches is not None
    assert record_feedback is not None
    assert __version__ is not None


def test_get_matches_is_callable() -> None:
    """get_matches is callable."""
    assert callable(get_matches)


def test_record_feedback_is_callable() -> None:
    """record_feedback is callable."""
    assert callable(record_feedback)


def test_version_is_string() -> None:
    """__version__ is a non-empty string."""
    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_all_exports() -> None:
    """__all__ contains exactly the documented public symbols."""
    assert set(wekruit_matching.__all__) == {"get_matches", "record_feedback", "__version__"}


def test_get_matches_signature() -> None:
    """get_matches has the expected parameter names."""
    sig = inspect.signature(get_matches)
    assert "profile" in sig.parameters
    assert "conn" in sig.parameters
    assert "top_n" in sig.parameters
    assert "openai_client" in sig.parameters


def test_record_feedback_signature() -> None:
    """record_feedback has the expected parameter names."""
    sig = inspect.signature(record_feedback)
    assert "user_id" in sig.parameters
    assert "job_id" in sig.parameters
    assert "reaction" in sig.parameters
    assert "conn" in sig.parameters
