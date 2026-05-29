"""
scanner_agent.py
================
Phase 1 (Research) agent for the Intelligent Documentation Engine.

Responsibilities:
  - Scan source directories and compute SHA-256 hashes.
  - Diff against the stored hash snapshot.
  - Write a structured scan report to research/{project_name}_scan_report.yaml.
  - Return a ScanResult for downstream pipeline phases.

Key rule: does NOT update hash_store/. Hash persistence is the Reviewer's
responsibility (Phase 4), ensuring idempotency — if any later phase fails,
the next run retries from scratch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..git_tracker import (
    get_head_commit,
    list_changed_files_in_commit,
    load_commit_state,
)
from ..sha_scanner import FileDiff, diff_hashes, load_hash_store, scan_directory
from ..versioner import load_version_history

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """Output of the Scanner Agent (Phase 1)."""

    project_name: str
    changed_files: list[str]
    diff: FileDiff
    current_hashes: dict[str, str]
    report_path: Path
    commit_triggered: bool
    commit_sha: str | None
    commit_changed_files: list[str]
    associated_document_path: str | None


def run(project: dict[str, Any], settings: dict[str, Any]) -> ScanResult:
    """Execute Phase 1: scan source files and detect changes.

    Writes a structured scan report to ``research/{project_name}_scan_report.yaml``.
    Does NOT update ``hash_store/`` — that is Phase 4's responsibility.

    Args:
        project:  Project entry from ``config.json``.
        settings: Global settings dict from ``config.json``.

    Returns:
        :class:`ScanResult` with the changed file list, full diff, and current hashes.
    """
    name: str = project["name"]
    logger.info("[Scanner] Phase 1 starting for project: %s", name)

    current_hashes = scan_directory(project["source_paths"], project["file_types"])
    previous_hashes = load_hash_store(settings["hash_store_dir"], name)
    diff = diff_hashes(current_hashes, previous_hashes)

    hash_changed_files = diff["new"] + diff["modified"]
    commit_changed_files: list[str] = []
    commit_sha: str | None = None
    commit_triggered = False

    if settings.get("commit_tracking", True):
        commit_sha = get_head_commit(repo_dir=".")
        if commit_sha:
            state = load_commit_state(settings["hash_store_dir"], name)
            last_processed = state.get("last_processed_commit")
            commit_triggered = commit_sha != last_processed
            if commit_triggered:
                commit_changed_files = list_changed_files_in_commit(
                    commit_sha=commit_sha,
                    source_paths=project["source_paths"],
                    file_types=project["file_types"],
                    repo_dir=".",
                )

    changed_files = sorted(set(hash_changed_files + commit_changed_files))

    history = load_version_history(project["output_dir"])
    associated_document_path = history[-1]["document_path"] if history else None

    report_path = _write_scan_report(
        name=name,
        project=project,
        settings=settings,
        changed_files=changed_files,
        diff=diff,
        commit_sha=commit_sha,
        commit_triggered=commit_triggered,
        commit_changed_files=commit_changed_files,
        associated_document_path=associated_document_path,
    )

    logger.info(
        "[Scanner] Phase 1 complete for '%s' — %d changed file(s). Report: %s",
        name, len(changed_files), report_path,
    )
    return ScanResult(
        project_name=name,
        changed_files=changed_files,
        diff=diff,
        current_hashes=current_hashes,
        report_path=report_path,
        commit_triggered=commit_triggered,
        commit_sha=commit_sha,
        commit_changed_files=commit_changed_files,
        associated_document_path=associated_document_path,
    )


def _write_scan_report(
    name: str,
    project: dict[str, Any],
    settings: dict[str, Any],
    changed_files: list[str],
    diff: FileDiff,
    commit_sha: str | None,
    commit_triggered: bool,
    commit_changed_files: list[str],
    associated_document_path: str | None,
) -> Path:
    """Serialize scan results to research/{name}_scan_report.yaml.

    Args:
        name:          Project name.
        project:       Project config dict.
        settings:      Global settings dict.
        changed_files: List of new + modified file paths.
        diff:          Full diff result from sha_scanner.

    Returns:
        Path to the written report file.
    """
    research_dir = Path("research")
    research_dir.mkdir(parents=True, exist_ok=True)
    report_path = research_dir / f"{name}_scan_report.yaml"

    report: dict[str, Any] = {
        "metadata": {
            "project_name": name,
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "agent": "scanner",
            "source_paths": project["source_paths"],
            "file_types": project["file_types"],
        },
        "changed_files": changed_files,
        "diff_stats": {
            "new": len(diff["new"]),
            "modified": len(diff["modified"]),
            "deleted": len(diff["deleted"]),
            "unchanged": len(diff["unchanged"]),
            "total_changed": len(changed_files),
        },
        "commit_tracking": {
            "enabled": settings.get("commit_tracking", True),
            "triggered": commit_triggered,
            "commit_sha": commit_sha,
            "commit_changed_files": commit_changed_files,
        },
        "associated_document": {
            "latest_document_path": associated_document_path,
        },
        "current_hashes_snapshot": (
            f"{settings['hash_store_dir']}/{name}.json "
            "(NOT YET UPDATED — Reviewer persists on Phase 4 success)"
        ),
    }

    with report_path.open("w", encoding="utf-8") as fh:
        yaml.dump(report, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)

    logger.debug("[Scanner] Scan report written to %s", report_path)
    return report_path
