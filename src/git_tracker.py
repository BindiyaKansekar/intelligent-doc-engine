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


class CommitRecord(TypedDict):
    """Structured representation of a single git commit."""

    sha: str
    author: str
    date: str
    subject: str


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


def get_commit_history(
    repo_dir: str = ".",
    since_commit: str | None = None,
    max_commits: int = 50,
) -> list[CommitRecord]:
    """Return structured commit history since *since_commit* (or last N commits)."""
    cmd = ["git", "log", "--pretty=format:%H|%an|%ai|%s", f"--max-count={max_commits}"]
    if since_commit:
        cmd.append(f"{since_commit}..HEAD")
    try:
        result = subprocess.run(
            cmd, cwd=repo_dir, check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        logger.warning("Unable to retrieve commit history from %s", repo_dir)
        return []

    records: list[CommitRecord] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        records.append(CommitRecord(sha=parts[0], author=parts[1], date=parts[2], subject=parts[3]))
    return records


def get_commit_diff_summary(
    repo_dir: str = ".",
    since_commit: str | None = None,
) -> str:
    """Return a concise diffstat since *since_commit* (or for the latest commit)."""
    if since_commit:
        cmd = ["git", "diff", "--stat", f"{since_commit}..HEAD"]
    else:
        cmd = ["git", "diff", "--stat", "HEAD~1..HEAD"]
    try:
        result = subprocess.run(
            cmd, cwd=repo_dir, check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        logger.warning("Unable to retrieve diff summary from %s", repo_dir)
        return ""
    return result.stdout.strip()


def list_changed_files_in_extra_repo(
    repo_path: str,
    target_branch: str | None,
    file_types: list[str],
) -> list[str]:
    """Return changed files in *repo_path* for multi-repo integration documentation.

    Diffs against *target_branch* (PR mode) when provided, otherwise diffs
    the latest commit (HEAD~1..HEAD). Filters by *file_types* extensions.

    Args:
        repo_path:     Absolute path to the additional repository root.
        target_branch: Branch to diff against (e.g. 'main'). ``None`` → latest commit.
        file_types:    Allowed file extensions (e.g. ['.py', '.yaml']).

    Returns:
        Sorted list of absolute file paths that changed in that repo.
    """
    repo_root = Path(repo_path).resolve()
    if not repo_root.exists():
        logger.warning("[GitTracker] Extra repo path does not exist: %s", repo_root)
        return []

    if target_branch:
        try:
            subprocess.run(
                ["git", "fetch", "origin", target_branch],
                cwd=str(repo_root),
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            pass
        diff_range = f"origin/{target_branch}...HEAD"
    else:
        diff_range = "HEAD~1..HEAD"

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", diff_range],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        logger.warning("[GitTracker] Unable to list changed files in extra repo: %s", repo_root)
        return []

    allowed_suffixes = set(file_types)
    matched: list[str] = []
    for rel_path in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
        abs_path = (repo_root / rel_path).resolve()
        if allowed_suffixes and abs_path.suffix not in allowed_suffixes:
            continue
        if abs_path.exists():
            matched.append(str(abs_path))

    logger.info(
        "[GitTracker] Extra repo '%s' — %d changed file(s) against '%s'",
        repo_root.name, len(matched), target_branch or "HEAD~1",
    )
    return sorted(set(matched))
