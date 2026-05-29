"""
builder_agent.py
================
Phase 3 (Build) agent for the Intelligent Documentation Engine.

Responsibilities:
  - Read the doc plan from Phase 2.
  - Call the Claude API (or mock) to generate documentation sections.
  - Assign a semantic version and output path via the versioner.
  - Inject generated content into the Word template.
  - Return a BuildResult for Phase 4 review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..claude_engine import generate_additional_points, generate_documentation_sections
from ..template_writer import write_document
from ..versioner import record_new_version
from .analyzer_agent import DocPlan

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    """Output of the Builder Agent (Phase 3)."""

    project_name: str
    doc_output_path: Path
    new_version: str
    sections: dict[str, str]
    replacements: dict[str, str]
    additional_points: list[str]
    additional_points_path: Path


def run(
    project: dict[str, Any],
    settings: dict[str, Any],
    doc_plan: DocPlan,
) -> BuildResult:
    """Execute Phase 3: generate documentation and write the versioned Word doc.

    Args:
        project:  Project entry from ``config.json``.
        settings: Global settings dict from ``config.json``.
        doc_plan: Output from :func:`analyzer_agent.run` (Phase 2).

    Returns:
        :class:`BuildResult` with the output path, version, and generated sections.

    Raises:
        anthropic.APIError: If the live API call fails.
        FileNotFoundError:  If the Word template is missing.
    """
    name: str = project["name"]
    logger.info("[Builder] Phase 3 starting for project: %s", name)

    sections = generate_documentation_sections(
        project_name=name,
        changed_files=doc_plan.changed_files,
        associated_document_path=doc_plan.associated_document_path,
        commit_sha=doc_plan.commit_sha,
        model=settings.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=settings.get("max_tokens", 4096),
        mock_mode=settings.get("mock_mode", False),
    )

    new_version, doc_output_path_str = record_new_version(
        output_dir=project["output_dir"],
        project_name=name,
        bump_type=doc_plan.version_bump_type,  # type: ignore[arg-type]
        changed_files=doc_plan.changed_files,
        commit_sha=doc_plan.commit_sha,
    )

    replacements: dict[str, str] = {
        "PROJECT_NAME": name,
        "VERSION": new_version,
        "GENERATED_DATE": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        **sections,
    }

    write_document(
        template_path=settings["template_path"],
        output_path=doc_output_path_str,
        replacements=replacements,
    )

    additional_points = generate_additional_points(
        project_name=name,
        changed_files=doc_plan.changed_files,
        associated_document_path=doc_plan.associated_document_path,
        commit_sha=doc_plan.commit_sha,
        model=settings.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=settings.get("max_tokens", 4096),
        mock_mode=settings.get("mock_mode", False),
    )

    additional_points_path = Path(project["output_dir"]) / f"AUD_v{new_version}_additional_points.md"
    additional_points_path.parent.mkdir(parents=True, exist_ok=True)
    with additional_points_path.open("w", encoding="utf-8") as fh:
        fh.write("# Additional Documentation Points\n\n")
        for point in additional_points:
            fh.write(f"- {point}\n")

    doc_output_path = Path(doc_output_path_str)
    logger.info(
        "[Builder] Phase 3 complete for '%s' — version: %s, document: %s",
        name, new_version, doc_output_path,
    )
    return BuildResult(
        project_name=name,
        doc_output_path=doc_output_path,
        new_version=new_version,
        sections=sections,
        replacements=replacements,
        additional_points=additional_points,
        additional_points_path=additional_points_path,
    )
