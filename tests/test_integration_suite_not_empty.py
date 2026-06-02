"""Guard: the DB-correctness (integration) suite must not silently go dark.

This is a *non-integration* unit test — it has no DB dependency and runs in the
plain ``pytest -q -m "not integration"`` lane. It exists because the entire
reliability story here rests on a class of DB-backed integration tests
(CHECK-constraint tests, schema invariants, the pre-sync health gate) actually
running. If someone deletes the last ``@pytest.mark.integration`` test, or a
refactor strips the markers, the CI "integration suite not empty" step would
still pass on a *0-collected* technicality on some runners — and the
DB-correctness suite would quietly stop protecting production.

So we assert, by static filesystem scan (stdlib only — no imports of test
modules, no DB), that at least ``MIN_INTEGRATION_TESTS`` test functions in
``tests/`` carry the ``@pytest.mark.integration`` decorator.

Implementation note: we deliberately do NOT ``import`` the test modules (that
could pull in psycopg/network at collection time). We read the files as text and
count decorator occurrences immediately preceding a ``def test_*`` /
``async def test_*``.
"""
from __future__ import annotations

import re
from pathlib import Path

# Minimum number of @pytest.mark.integration-decorated test functions that must
# exist across tests/. Three is the floor agreed in the reliability contract:
# the constraint suite, the schema-guard suite, and the pre-sync health gate.
MIN_INTEGRATION_TESTS = 3

# A decorator line of the forms:
#   @pytest.mark.integration
#   @pytest.mark.integration(...)
#   @mark.integration            (from `from pytest import mark`)
_INTEGRATION_DECORATOR = re.compile(
    r"^\s*@(?:pytest\.)?mark\.integration\b",
)

# A test function definition (sync or async).
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_\w*\s*\(")

# A module-level marker that applies @integration to EVERY test in the file:
#   pytestmark = pytest.mark.integration
#   pytestmark = [pytest.mark.integration, ...]
# (this is what WS-B's pre-sync / migration suites use, so the decorator-only
# scan below would miss them — count every test function in such a module).
_MODULE_PYTESTMARK = re.compile(
    r"^\s*pytestmark\s*=\s*\[?[^\n]*?\bmark\.integration\b",
    re.MULTILINE,
)


def _tests_dir() -> Path:
    """Directory holding this test file == the tests/ package root."""
    return Path(__file__).resolve().parent


def _count_integration_marked_functions() -> tuple[int, list[str]]:
    """Return (count, sample_locations) of integration-marked test functions.

    A function counts if at least one ``@…mark.integration`` decorator appears in
    its decorator block — i.e. somewhere in the contiguous run of ``@``-lines (and
    blank/comment lines) immediately above the ``def test_…`` line.
    """
    count = 0
    locations: list[str] = []
    for path in sorted(_tests_dir().rglob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        # A module-level ``pytestmark = ...mark.integration`` marks EVERY test in
        # the file — count them all, not just decorator-annotated ones.
        module_marked = bool(_MODULE_PYTESTMARK.search(text))
        for i, line in enumerate(lines):
            if not _TEST_DEF.match(line):
                continue
            if module_marked:
                count += 1
                locations.append(f"{path.name}:{i + 1}")
                continue
            # Walk upward through the decorator/blank/comment block above this
            # def, looking for an integration marker. Stop at the first line
            # that is neither a decorator, blank, nor a comment.
            j = i - 1
            marked = False
            while j >= 0:
                above = lines[j]
                if _INTEGRATION_DECORATOR.match(above):
                    marked = True
                    break
                stripped = above.strip()
                if stripped.startswith("@") or stripped == "" or stripped.startswith("#"):
                    j -= 1
                    continue
                break
            if marked:
                count += 1
                locations.append(f"{path.name}:{i + 1}")
    return count, locations


def test_integration_suite_is_non_empty() -> None:
    """At least MIN_INTEGRATION_TESTS integration tests must exist in tests/.

    If this fails, the DB-correctness suite has gone dark — re-add (or stop
    deleting) ``@pytest.mark.integration`` tests rather than weakening this guard.
    """
    count, locations = _count_integration_marked_functions()
    assert count >= MIN_INTEGRATION_TESTS, (
        f"Expected at least {MIN_INTEGRATION_TESTS} @pytest.mark.integration "
        f"test functions in tests/, found {count}. The DB-correctness suite must "
        f"not silently go dark. Found: {locations}"
    )
