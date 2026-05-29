"""
git_tracker.py
==============
Git commit tracking utilities for commit-aware documentation runs.

Responsibilities:
  - Detect current HEAD commit SHA.
  - Persist last processed commit per project.
  - Resolve files changed in a specific commit, filtered by source paths and file types.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


class CommitState(TypedDict, total=False):
    """Persistent commit-state payload per project."""

    last_processed_commit: str


def get_head_commit(repo_dir: str = ".") -> str | None:
    """Return HEAD commit SHA if *repo_dir* is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    sha = result.stdout.strip()
    return sha or None


def load_commit_state(hash_store_dir: str, project_name: str) -> CommitState:
    """Load commit state from hash_store/{project_name}_commit_state.json."""
    state_path = Path(hash_store_dir) / f"{project_name}_commit_state.json"
    if not state_path.exists():
        return CommitState()

    try:
        with state_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning("Commit state unreadable at %s. Reinitializing.", state_path)
        return CommitState()

    if not isinstance(data, dict):
        return CommitState()
    return CommitState(**data)


def save_commit_state(hash_store_dir: str, project_name: str, commit_sha: str) -> None:
    """Persist latest processed commit SHA for a project."""
    store_dir = Path(hash_store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    state_path = store_dir / f"{project_name}_commit_state.json"

    payload: CommitState = {"last_processed_commit": commit_sha}
    with state_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def list_changed_files_in_commit(
    commit_sha: str,
    source_paths: list[str],
    file_types: list[str],
    repo_dir: str = ".",
) -> list[str]:
    """Return tracked files changed by *commit_sha*.

    Uses ``git show --name-only`` and filters paths to configured source roots
    and allowed extensions.
    """
    try:
        result = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", commit_sha],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        logger.warning("Unable to resolve changed files for commit %s", commit_sha)
        return []

    repo_root = Path(repo_dir).resolve()
    allowed_suffixes = set(file_types)
    source_roots = [Path(p).resolve() for p in source_paths]

    matched: list[str] = []
    for rel_path in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
        abs_path = (repo_root / rel_path).resolve()
        if abs_path.suffix not in allowed_suffixes:
            continue
        if any(_is_within(abs_path, root) for root in source_roots):
            matched.append(str(abs_path))

    return sorted(set(matched))


def _is_within(path: Path, root: Path) -> bool:
    """Return True if *path* is equal to or nested under *root*."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
