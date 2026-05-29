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
import os
import subprocess
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


class CommitState(TypedDict, total=False):
    """Persistent commit-state payload per project."""

    last_processed_commit: str
    repo_commits: dict[str, str]


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


def get_repo_root(path: str = ".") -> str | None:
    """Return git root directory for *path*, or None when not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    root = result.stdout.strip()
    return root or None


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


def save_commit_state(
    hash_store_dir: str,
    project_name: str,
    commit_sha: str,
    repo_root: str | None = None,
) -> None:
    """Persist latest processed commit SHA for a project/repository."""
    store_dir = Path(hash_store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    state_path = store_dir / f"{project_name}_commit_state.json"

    payload = load_commit_state(hash_store_dir, project_name)
    payload["last_processed_commit"] = commit_sha
    repo_commits = dict(payload.get("repo_commits", {}))
    if repo_root:
        repo_commits[str(Path(repo_root).resolve())] = commit_sha
    payload["repo_commits"] = repo_commits

    with state_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def get_last_processed_commit_for_repo(state: CommitState, repo_root: str) -> str | None:
    """Return last processed commit SHA for a specific repo root."""
    repo_commits = state.get("repo_commits", {})
    normalized_root = str(Path(repo_root).resolve())
    if normalized_root in repo_commits:
        return repo_commits[normalized_root]
    return state.get("last_processed_commit")


def get_ci_trigger_mode() -> str:
    """Return CI trigger mode: pr, commit, or manual."""
    reason = os.getenv("BUILD_REASON", "").strip().lower()
    if reason == "pullrequest":
        return "pr"
    if reason in {"individualci", "batchedci", "schedule", "resourcetrigger"}:
        return "commit"
    return "manual"


def get_pr_target_branch() -> str | None:
    """Return PR target branch name from Azure DevOps env if available."""
    raw = os.getenv("SYSTEM_PULLREQUEST_TARGETBRANCH", "").strip()
    if not raw:
        return None
    prefix = "refs/heads/"
    return raw[len(prefix):] if raw.startswith(prefix) else raw


def list_changed_files_in_pr(
    target_branch: str,
    source_paths: list[str],
    file_types: list[str],
    repo_dir: str = ".",
) -> list[str]:
    """Return tracked files changed in a PR against *target_branch*."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", target_branch],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["git", "diff", "--name-only", f"origin/{target_branch}...HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        logger.warning("Unable to resolve PR changed files for target branch '%s'", target_branch)
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
