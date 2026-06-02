"""Offline unit test for the daily-update.sh dirty-working-tree SHA-pin guard.

This exercises the CID-02 "working-tree-is-prod" guard logic in *isolation*:

  * We DO NOT run the real ``scripts/daily-update.sh`` end to end — past the
    guard it would source the prod ``.env``, run ``alembic upgrade head``, take
    the Firestore lock, and run the pipeline against the live PROD database.
    None of that is acceptable in a unit test.
  * Instead we write a tiny stub script into a throwaway ``git`` repo created in
    ``tmp_path``. The stub contains the *same* guard snippet that lives at the
    top of ``scripts/daily-update.sh`` (capture RUN_SHA -> ``git status
    --porcelain`` -> refuse with exit 3 unless ``ALLOW_DIRTY=1``), followed by a
    sentinel echo so we can assert whether execution proceeded *past* the guard.

The guard text below is kept byte-for-byte aligned with the real script's
behavior; if the real guard changes, this stub should be updated in lockstep.

stdlib + subprocess only. No DB, no network, no real-repo mutation, not an
integration test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# The guard snippet under test. This mirrors the dirty-tree / ALLOW_DIRTY logic
# at the top of scripts/daily-update.sh. We deliberately stop before the
# best-effort `git fetch` (network) and emit a sentinel so the test can detect
# whether control flowed past the guard.
_GUARD_STUB = r"""#!/bin/bash
set -u
RUN_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"

DIRTY="$(git status --porcelain 2>/dev/null)"
if [[ -n "$DIRTY" && "$ALLOW_DIRTY" != "1" ]]; then
  echo "[daily-update] ERROR: working tree is dirty — refusing to run as prod." >&2
  echo "$DIRTY" | sed 's/^/[daily-update]   /' >&2
  exit 3
fi

echo "[daily-update] runSha=${RUN_SHA} allowDirty=${ALLOW_DIRTY}"
# Sentinel: reaching here means we passed the guard. The real script would now
# source .env / migrate / lock / run the pipeline — which the test must NEVER do.
echo "GUARD_PASSED"
"""


def _git(repo: Path, *args: str) -> None:
    """Run a git command in ``repo`` with a deterministic, isolated identity."""
    env = dict(os.environ)
    # Pin a committer/author identity so `git commit` works on a fresh CI box
    # without relying on the developer's global gitconfig.
    env.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Create a throwaway git repo containing the guard stub + one commit."""
    repo = tmp_path / "throwaway-repo"
    repo.mkdir()
    _git(repo, "init", "-q")

    stub = repo / "guard.sh"
    stub.write_text(_GUARD_STUB)
    stub.chmod(0o755)

    # Commit the stub so the working tree is CLEAN to start with.
    _git(repo, "add", "guard.sh")
    _git(repo, "commit", "-q", "-m", "add guard stub")
    return repo


def _run_guard(repo: Path, *, allow_dirty: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if allow_dirty is not None:
        env["ALLOW_DIRTY"] = allow_dirty
    else:
        env.pop("ALLOW_DIRTY", None)
    return subprocess.run(
        ["bash", "guard.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_clean_tree_passes_guard(tmp_path: Path) -> None:
    """A clean, committed working tree flows past the guard (exit 0, sentinel)."""
    repo = _make_repo(tmp_path)

    result = _run_guard(repo)

    assert result.returncode == 0, (
        f"clean tree should pass guard; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "GUARD_PASSED" in result.stdout
    # SHA line is emitted on the happy path.
    assert "runSha=" in result.stdout
    assert "allowDirty=0" in result.stdout


def test_dirty_tree_refuses_with_exit_3(tmp_path: Path) -> None:
    """An untracked file makes the tree dirty -> guard refuses with exit 3."""
    repo = _make_repo(tmp_path)
    # Make the working tree dirty with an untracked file.
    (repo / "uncommitted.txt").write_text("local edit not committed\n")

    result = _run_guard(repo)

    assert result.returncode == 3, (
        f"dirty tree must exit 3; got {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # We must NOT have proceeded past the guard.
    assert "GUARD_PASSED" not in result.stdout
    assert "refusing to run as prod" in result.stderr


def test_dirty_tree_with_allow_dirty_proceeds(tmp_path: Path) -> None:
    """ALLOW_DIRTY=1 is the documented dev escape hatch -> proceeds past guard."""
    repo = _make_repo(tmp_path)
    (repo / "uncommitted.txt").write_text("local edit not committed\n")

    result = _run_guard(repo, allow_dirty="1")

    assert result.returncode == 0, (
        f"ALLOW_DIRTY=1 should bypass the dirty check; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "GUARD_PASSED" in result.stdout
    assert "allowDirty=1" in result.stdout


def test_modified_tracked_file_is_dirty(tmp_path: Path) -> None:
    """Modifying a tracked (committed) file also trips the guard (exit 3)."""
    repo = _make_repo(tmp_path)
    # Modify the already-committed stub itself -> porcelain reports it dirty.
    (repo / "guard.sh").write_text(_GUARD_STUB + "\n# local tweak\n")

    result = _run_guard(repo)

    assert result.returncode == 3
    assert "GUARD_PASSED" not in result.stdout
