"""Git-clone-backed scraper for jobright-ai GitHub repos.

PURE-DIFF MODE (2026-05-13, v3)
-------------------------------
Pre-v3 the delta path still parsed the full local README so that
``mark_stale_jobs`` could see the full active set. With ``JOBRIGHT_USE_GIT_DELTA=1``
the pipeline now treats the GitHub commit history *itself* as the source
of truth:

  * ``+ | ... |``  rows in HEAD~1..HEAD diff → new jobs (to upsert)
  * ``- | ... |``  rows in HEAD~1..HEAD diff → stale jobs (to mark inactive)

No full README parse on the steady-state daily run. Bootstrap (fresh
clone with no prior commit) still parses the full README once.

Why this is safe
----------------
Stable v2 ``generate_job_id(source_repo, company, role)`` makes the
``+`` and ``-`` rows hash to the same ``job_id`` for the same logical
job. jobright-ai's daily push pattern is "add new rows, remove sold/
filled rows" — exactly the delta semantics we want.

Force-push guard: ``MAX_DELTA_LINES`` caps trustworthy diff size. Beyond
that we treat the diff as a force-push / mass-rewrite and fall back to
a full README parse + full stale set for that run.

Environment
-----------
``JOBRIGHT_USE_GIT_DELTA=1``  flips ``jobright_github._scrape_repos`` from
                              HTTP fetch + full parse to pure-diff.
``JOBRIGHT_CLONE_ROOT``       optional clone root override.
                              Default ``/Users/Shared/wekruit/jobright-repos``.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

JOBRIGHT_ORG = "jobright-ai"
CLONE_ROOT = Path(os.environ.get("JOBRIGHT_CLONE_ROOT", "/Users/Shared/wekruit/jobright-repos"))
MAX_DELTA_LINES = 1000


@dataclass
class RepoSnapshot:
    """Result of one git fetch.

    Attributes
    ----------
    repo_name      jobright-ai repo slug
    full_readme    README contents iff `used_delta=False` (bootstrap or force-push fallback).
                   None when the diff is trustworthy and the caller should only consume
                   ``added_rows`` / ``removed_rows``.
    added_rows     Markdown table rows that appeared in HEAD~1..HEAD.
    removed_rows   Markdown table rows that disappeared in HEAD~1..HEAD.
    used_delta     True iff caller should treat (added_rows, removed_rows) as the full
                   change set. False iff caller must re-parse full_readme.
    """
    repo_name: str
    full_readme: str | None
    added_rows: list[str] = field(default_factory=list)
    removed_rows: list[str] = field(default_factory=list)
    used_delta: bool = False


def _run(args: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def ensure_clone(repo_name: str) -> tuple[Path, bool]:
    """Clone repo if missing, else fetch + reset to origin/HEAD.

    Returns
    -------
    (repo_dir, just_cloned)
        ``just_cloned`` is True iff this call actually created a new local clone.
        Callers use it to skip the HEAD~1..HEAD diff (no prior commit to compare).
    """
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    repo_dir = CLONE_ROOT / repo_name
    url = f"https://github.com/{JOBRIGHT_ORG}/{repo_name}.git"
    just_cloned = False
    if not (repo_dir / ".git").exists():
        logger.info("git clone %s", url)
        _run(["git", "clone", "--depth", "30", url, str(repo_dir)])
        just_cloned = True
    else:
        _run(["git", "fetch", "origin", "--depth", "30"], cwd=repo_dir)
        head_ref = _run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=repo_dir,
        ).strip() or "origin/main"
        _run(["git", "reset", "--hard", head_ref], cwd=repo_dir)
    return repo_dir, just_cloned


def _diff_table_rows(
    repo_dir: Path, file_path: str = "README.md"
) -> tuple[list[str], list[str]] | None:
    """Return (added_rows, removed_rows) from HEAD~1..HEAD diff of file_path.

    Returns ``None`` when:
      * No prior commit exists (caller falls back to full parse), or
      * Either side of the diff exceeds ``MAX_DELTA_LINES`` (force-push guard).

    A "table row" is any non-header diff line whose stripped content
    starts with ``|`` and ends with ``|`` — matching the markdown pipe
    table format that ``_parse_markdown_table`` consumes.
    """
    try:
        diff = _run(
            ["git", "diff", "HEAD~1..HEAD", "--unified=0", "--", file_path],
            cwd=repo_dir,
        )
    except RuntimeError as e:
        logger.warning("git diff failed for %s: %s", repo_dir.name, e)
        return None

    added: list[str] = []
    removed: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            row = line[1:].strip()
            if row.startswith("|") and row.endswith("|"):
                added.append(row)
        elif line.startswith("-"):
            row = line[1:].strip()
            if row.startswith("|") and row.endswith("|"):
                removed.append(row)
        if len(added) > MAX_DELTA_LINES or len(removed) > MAX_DELTA_LINES:
            logger.warning(
                "%s: delta exceeded %d lines (added=%d, removed=%d) — force-push fallback",
                repo_dir.name, MAX_DELTA_LINES, len(added), len(removed),
            )
            return None
    return added, removed


def fetch_repo(repo_name: str) -> RepoSnapshot:
    """Fetch a single repo + return its delta snapshot.

    Pure-diff mode when the diff is trustworthy. Falls back to
    ``full_readme=<contents>, used_delta=False`` on bootstrap or force-push;
    caller then re-parses the full README and computes its own stale set.
    """
    repo_dir, just_cloned = ensure_clone(repo_name)
    readme_path = repo_dir / "README.md"
    if not readme_path.exists():
        raise RuntimeError(f"{repo_name}: README.md missing after clone")

    if just_cloned:
        logger.info("fetch_repo %s: bootstrap (fresh clone) — full parse", repo_name)
        return RepoSnapshot(
            repo_name=repo_name,
            full_readme=readme_path.read_text(encoding="utf-8"),
            used_delta=False,
        )

    diff_result = _diff_table_rows(repo_dir)
    if diff_result is None:
        logger.info("fetch_repo %s: diff unavailable — full parse fallback", repo_name)
        return RepoSnapshot(
            repo_name=repo_name,
            full_readme=readme_path.read_text(encoding="utf-8"),
            used_delta=False,
        )

    added, removed = diff_result
    logger.info(
        "fetch_repo %s: pure-diff added=%d removed=%d",
        repo_name, len(added), len(removed),
    )
    return RepoSnapshot(
        repo_name=repo_name,
        full_readme=None,
        added_rows=added,
        removed_rows=removed,
        used_delta=True,
    )


def is_enabled() -> bool:
    return os.environ.get("JOBRIGHT_USE_GIT_DELTA") == "1"
