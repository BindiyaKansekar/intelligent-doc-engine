"""
analyzer_agent.py
=================
Phase 2 (Planning) agent for the Intelligent Documentation Engine.

Responsibilities:
  - Read the scan report from Phase 1.
  - Determine which documentation sections to generate.
  - Infer the appropriate semver bump type from the nature of the changes.
  - Write a structured doc plan to plans/{project_name}_doc_plan.yaml.

Version bump heuristic:
  major  — >50% of all tracked files changed (significant refactor / restructure)
  minor  — any .py or .json files changed (functional / configuration change)
  patch  — only .md, .yaml, .yml, .txt files changed (docs / config-only change)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..claude_engine import REQUIRED_SECTIONS
from .scanner_agent import ScanResult

logger = logging.getLogger(__name__)

_FUNCTIONAL_EXTENSIONS = {".py", ".json"}
_DOC_ONLY_EXTENSIONS = {".md", ".yaml", ".yml", ".txt", ".rst"}


@dataclass
class DocPlan:
    """Output of the Analyzer Agent (Phase 2)."""

    project_name: str
    sections_to_generate: list[str]
    version_bump_type: str
    changed_files: list[str]
    plan_path: Path
    commit_sha: str | None
    associated_document_path: str | None


def run(
    project: dict[str, Any],
    settings: dict[str, Any],
    scan_result: ScanResult,
) -> DocPlan:
    """Execute Phase 2: plan documentation sections from scan results.

    Reads ``research/{project_name}_scan_report.yaml`` and writes
    ``plans/{project_name}_doc_plan.yaml``.

    Args:
        project:     Project entry from ``config.json``.
        settings:    Global settings dict from ``config.json``.
        scan_result: Output from :func:`scanner_agent.run` (Phase 1).

    Returns:
        :class:`DocPlan` with sections to generate, version bump type, and plan path.
    """
    name: str = project["name"]
    logger.info("[Analyzer] Phase 2 starting for project: %s", name)

    changed_files = scan_result.changed_files
    total_tracked = len(scan_result.current_hashes)

    bump_type, bump_rationale = _infer_bump_type(changed_files, total_tracked)
    sections = list(REQUIRED_SECTIONS)
    section_rationale = "All 6 sections regenerated (code or config files changed)"

    plan_path = _write_doc_plan(
        name,
        changed_files,
        sections,
        bump_type,
        bump_rationale,
        section_rationale,
        scan_result.commit_sha,
        scan_result.associated_document_path,
    )

    logger.info(
        "[Analyzer] Phase 2 complete for '%s' — bump: %s, sections: %d. Plan: %s",
        name, bump_type, len(sections), plan_path,
    )
    return DocPlan(
        project_name=name,
        sections_to_generate=sections,
        version_bump_type=bump_type,
        changed_files=changed_files,
        plan_path=plan_path,
        commit_sha=scan_result.commit_sha,
        associated_document_path=scan_result.associated_document_path,
    )


def _infer_bump_type(changed_files: list[str], total_tracked: int) -> tuple[str, str]:
    """Determine the semver bump type from the changed file set.

    Args:
        changed_files: List of new + modified file paths.
        total_tracked: Total number of tracked files (for major-bump threshold).

    Returns:
        Tuple of ``(bump_type, rationale_string)``.
    """
    if not changed_files:
        return "patch", "No changes — defaulting to patch"

    if total_tracked > 0 and len(changed_files) / total_tracked > 0.5:
        return "major", (
            f"{len(changed_files)}/{total_tracked} files changed "
            "(>50% of tracked files) — significant refactor → major bump"
        )

    functional = [f for f in changed_files if Path(f).suffix in _FUNCTIONAL_EXTENSIONS]
    if functional:
        sample_exts = ", ".join(sorted({Path(f).suffix for f in functional[:3]}))
        return "minor", (
            f"{len(functional)} functional file(s) changed "
            f"({sample_exts}) → minor bump"
        )

    return "patch", (
        f"{len(changed_files)} doc/config file(s) changed only → patch bump"
    )


def _write_doc_plan(
    name: str,
    changed_files: list[str],
    sections: list[str],
    bump_type: str,
    bump_rationale: str,
    section_rationale: str,
    commit_sha: str | None,
    associated_document_path: str | None,
) -> Path:
    """Serialize the documentation plan to plans/{name}_doc_plan.yaml.

    Args:
        name:              Project name.
        changed_files:     Changed file paths from the scan.
        sections:          Doc sections to generate.
        bump_type:         Semver bump type.
        bump_rationale:    Human-readable rationale for the bump type.
        section_rationale: Human-readable rationale for section selection.

    Returns:
        Path to the written plan file.
    """
    plans_dir = Path("plans")
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plans_dir / f"{name}_doc_plan.yaml"

    plan: dict[str, Any] = {
        "metadata": {
            "project_name": name,
            "planned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "agent": "analyzer",
            "source_scan": f"research/{name}_scan_report.yaml",
        },
        "sections_to_generate": sections,
        "version_bump_type": bump_type,
        "changed_files": changed_files,
        "commit_sha": commit_sha,
        "associated_document_path": associated_document_path,
        "rationale": {
            "version_bump": bump_rationale,
            "sections": section_rationale,
        },
    }

    with plan_path.open("w", encoding="utf-8") as fh:
        yaml.dump(plan, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)

    logger.debug("[Analyzer] Doc plan written to %s", plan_path)
    return plan_path
