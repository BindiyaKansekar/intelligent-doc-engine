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

from ..claude_engine import EXTENDED_SECTIONS, INTEGRATION_SECTIONS, REQUIRED_SECTIONS
from .scanner_agent import ScanResult

logger = logging.getLogger(__name__)

_FUNCTIONAL_EXTENSIONS = {".py", ".json"}
_DOC_ONLY_EXTENSIONS = {".md", ".yaml", ".yml", ".txt", ".rst"}

# Maps each doc section to the file extensions whose changes should trigger its regeneration.
# None means "always regenerate regardless of file type".
_SECTION_FILE_TRIGGERS: dict[str, set[str] | None] = {
    "OVERVIEW":                None,
    "FILE_INVENTORY":          None,
    "DATA_FLOWS":              {".py"},
    "DEPENDENCIES":            {".py", ".txt", ".toml", ".cfg", ".cfg"},
    "CONFIGURATION":           {".yaml", ".yml", ".json", ".env", ".toml", ".ini", ".cfg"},
    "KNOWN_ISSUES":            {".py"},
    "CHANGE_SUMMARY":          None,
    "ARCHITECTURE_DIAGRAM":    {".py"},
    "REQUIREMENT_TRACEABILITY": None,
    # Extended sections — regenerate when .py files change (models/API may have changed)
    "IMPLEMENTATION_DETAILS":  {".py"},
    "DATA_MODEL":              {".py"},
    "API_REFERENCE":           {".py"},
    # Integration sections — always regenerate when new-integration flag is set
    "INTEGRATION_OVERVIEW":    None,
    "SEQUENCE_DIAGRAM":        None,
    "API_FIELD_MAPPING":       None,
    "ERROR_RETRY_BEHAVIOUR":   None,
}


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
    is_first_run: bool = False
    commit_messages: list[dict] | None = None
    commit_diff_summary: str = ""
    work_items: list[dict] | None = None
    is_new_integration: bool = False
    extra_repos: list[dict] | None = None


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

    changed_exts = {Path(f).suffix.lower() for f in changed_files}

    if scan_result.is_first_run:
        sections = list(REQUIRED_SECTIONS) + list(EXTENDED_SECTIONS)
        section_rationale = (
            "First run — no previous documentation exists. "
            "All standard and extended sections generated fresh."
        )
        logger.info("[Analyzer] First run detected for '%s' — generating all sections", name)
    else:
        sections = _map_files_to_sections(changed_files)
        # Add extended sections that are triggered by the changed file types
        triggered_extended = [
            s for s in EXTENDED_SECTIONS
            if not _SECTION_FILE_TRIGGERS.get(s) or changed_exts & _SECTION_FILE_TRIGGERS[s]
        ]
        sections = list(dict.fromkeys(sections + triggered_extended))
        skipped_required = sorted(set(REQUIRED_SECTIONS) - set(sections))
        skipped_extended = sorted(set(EXTENDED_SECTIONS) - set(triggered_extended))
        section_rationale = (
            f"Subsequent run — {len(sections)} section(s) regenerated. "
            + (f"Preserved from cache: {', '.join(skipped_required)}. " if skipped_required else "")
            + (f"Extended sections skipped (no .py changes): {', '.join(skipped_extended)}." if skipped_extended else "")
        )
        logger.info(
            "[Analyzer] Subsequent run for '%s' — regenerating: %s | preserving: %s",
            name, sections, skipped_required,
        )

    if scan_result.is_new_integration:
        # Append integration-specific sections, preserving order and avoiding duplicates
        existing = set(sections)
        for s in INTEGRATION_SECTIONS:
            if s not in existing:
                sections.append(s)
        section_rationale += (
            f" New-integration flag set — appending integration sections: "
            f"{', '.join(INTEGRATION_SECTIONS)}."
        )
        logger.info(
            "[Analyzer] New-integration flag — added sections: %s", INTEGRATION_SECTIONS
        )

    plan_path = _write_doc_plan(
        name,
        changed_files,
        sections,
        bump_type,
        bump_rationale,
        section_rationale,
        scan_result.commit_sha,
        scan_result.associated_document_path,
        scan_result.is_first_run,
        scan_result.is_new_integration,
        scan_result.extra_repos,
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
        is_first_run=scan_result.is_first_run,
        commit_messages=scan_result.commit_messages,
        commit_diff_summary=scan_result.commit_diff_summary,
        work_items=scan_result.work_items,
        is_new_integration=scan_result.is_new_integration,
        extra_repos=scan_result.extra_repos,
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


def _map_files_to_sections(changed_files: list[str]) -> list[str]:
    """Return the minimal set of doc sections that must be regenerated.

    Sections whose trigger extensions overlap with the extensions present in
    *changed_files* are included.  Sections with ``None`` triggers (OVERVIEW,
    FILE_INVENTORY) are always included because any code change affects them.

    Args:
        changed_files: List of new + modified file paths from the scan.

    Returns:
        Ordered list of section keys (subset of :data:`REQUIRED_SECTIONS`).
    """
    changed_exts = {Path(f).suffix.lower() for f in changed_files}
    selected: list[str] = []
    for section in REQUIRED_SECTIONS:   # preserve canonical ordering
        triggers = _SECTION_FILE_TRIGGERS.get(section)
        if triggers is None or changed_exts & triggers:
            selected.append(section)
    # Guarantee at least one section is always selected
    return selected if selected else list(REQUIRED_SECTIONS)


def _write_doc_plan(
    name: str,
    changed_files: list[str],
    sections: list[str],
    bump_type: str,
    bump_rationale: str,
    section_rationale: str,
    commit_sha: str | None,
    associated_document_path: str | None,
    is_first_run: bool = False,
    is_new_integration: bool = False,
    extra_repos: list[dict] | None = None,
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
            "is_first_run": is_first_run,
            "is_new_integration": is_new_integration,
            "extra_repo_count": len(extra_repos) if extra_repos else 0,
        },
        "sections_to_generate": sections,
        "sections_preserved_from_cache": sorted(set(REQUIRED_SECTIONS) - set(sections)),
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
