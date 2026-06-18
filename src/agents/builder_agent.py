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

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..claude_engine import EXTENDED_SECTIONS, INTEGRATION_SECTIONS, REQUIRED_SECTIONS, generate_additional_points, generate_documentation_sections
from ..mermaid_renderer import extract_mermaid_code, render_mermaid_to_png
from ..template_writer import append_additional_points, append_extended_sections, append_integration_sections, write_document
from ..versioner import record_new_version
from .analyzer_agent import DocPlan

logger = logging.getLogger(__name__)

# Filename for the per-project sections cache stored alongside the output docs
_SECTIONS_CACHE_FILENAME = "sections_cache.json"


# ---------------------------------------------------------------------------
# Sections cache helpers
# ---------------------------------------------------------------------------

def _cache_path(output_dir: str) -> Path:
    """Return the path to the sections cache file for a project.

    Args:
        output_dir: Project output directory (e.g. ``output/energy_grid_monitor``).

    Returns:
        Path to ``sections_cache.json`` inside *output_dir*.
    """
    return Path(output_dir) / _SECTIONS_CACHE_FILENAME


def _load_sections_cache(output_dir: str) -> dict[str, str]:
    """Load the previously saved sections cache, or return an empty dict.

    Args:
        output_dir: Project output directory.

    Returns:
        Mapping of section key → previously generated text, or ``{}`` if absent.
    """
    cache_file = _cache_path(output_dir)
    if not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[Builder] Could not load sections cache %s: %s", cache_file, exc)
    return {}


def _save_sections_cache(output_dir: str, sections: dict[str, str]) -> None:
    """Persist all generated sections to the cache file.

    Args:
        output_dir: Project output directory.
        sections:   Complete mapping of section key → generated text.
    """
    cache_file = _cache_path(output_dir)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache_file.write_text(
            json.dumps(sections, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug("[Builder] Sections cache saved to %s", cache_file)
    except OSError as exc:
        logger.warning("[Builder] Could not save sections cache %s: %s", cache_file, exc)


@dataclass
class BuildResult:
    """Output of the Builder Agent (Phase 3)."""

    project_name: str
    doc_output_path: Path
    new_version: str
    sections: dict[str, str]
    replacements: dict[str, str]
    additional_points: list[str]
    is_new_integration: bool = False


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

    # ------------------------------------------------------------------
    # Generate only the sections flagged by the Analyzer.
    # On first run: all sections. On subsequent runs: only changed ones.
    # ------------------------------------------------------------------
    newly_generated = generate_documentation_sections(
        project_name=name,
        changed_files=doc_plan.changed_files,
        associated_document_path=doc_plan.associated_document_path,
        commit_sha=doc_plan.commit_sha,
        model=settings.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=settings.get("max_tokens", 4096),
        mock_mode=settings.get("mock_mode", False),
        cli_mode=settings.get("cli_mode", False),
        sections_to_generate=doc_plan.sections_to_generate,
        commit_messages=doc_plan.commit_messages,
        commit_diff_summary=doc_plan.commit_diff_summary,
        work_items=doc_plan.work_items,
    )

    # On subsequent runs, fill gaps with cached content from the previous build
    cached = {} if doc_plan.is_first_run else _load_sections_cache(project["output_dir"])
    missing_from_cache = [s for s in REQUIRED_SECTIONS
                          if s not in newly_generated and s not in cached]
    if missing_from_cache:
        logger.warning(
            "[Builder] Sections missing from both generation and cache — will use empty placeholders: %s",
            missing_from_cache,
        )

    # Merge: newly generated takes priority; cache fills in unchanged sections
    sections: dict[str, str] = {**cached, **newly_generated}

    if doc_plan.is_first_run:
        logger.info("[Builder] First run — all %d sections generated fresh", len(sections))
    else:
        preserved = sorted(set(cached) - set(newly_generated))
        logger.info(
            "[Builder] Subsequent run — %d section(s) regenerated, %d preserved from cache: %s",
            len(newly_generated), len(preserved), preserved,
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

    # ------------------------------------------------------------------
    # Render Mermaid diagram to image if available
    # ------------------------------------------------------------------
    image_sections: dict[str, str] = {}
    output_fmt = settings.get("output_format", {})
    if output_fmt.get("render_mermaid", True) and "ARCHITECTURE_DIAGRAM" in sections:
        mermaid_code = extract_mermaid_code(sections["ARCHITECTURE_DIAGRAM"])
        if mermaid_code:
            img_path = Path(project["output_dir"]) / f"architecture_v{new_version}.png"
            rendered = render_mermaid_to_png(
                mermaid_code, str(img_path),
                renderer=output_fmt.get("mermaid_renderer", "auto"),
            )
            if rendered:
                image_sections["ARCHITECTURE_DIAGRAM"] = str(rendered)
                logger.info("[Builder] Architecture diagram rendered → %s", rendered)

    table_sections = output_fmt.get(
        "table_sections",
        ["FILE_INVENTORY", "CHANGE_SUMMARY", "DEPENDENCIES", "REQUIREMENT_TRACEABILITY"],
    )

    write_document(
        template_path=settings["template_path"],
        output_path=doc_output_path_str,
        replacements=replacements,
        table_sections=table_sections,
        image_sections=image_sections,
    )

    # ------------------------------------------------------------------
    # Extended sections: Implementation Details, Data Model, API Reference
    # Always appended for every project.
    # ------------------------------------------------------------------
    extended_content = {k: sections[k] for k in EXTENDED_SECTIONS if sections.get(k)}
    if extended_content or image_sections:
        append_extended_sections(doc_output_path_str, extended_content, image_sections)
        logger.info(
            "[Builder] Extended sections appended (%d section(s)%s)",
            len(extended_content),
            ", architecture diagram embedded" if image_sections.get("ARCHITECTURE_DIAGRAM") else "",
        )

    # ------------------------------------------------------------------
    # New-integration: render sequence diagram + append integration block
    # ------------------------------------------------------------------
    if doc_plan.is_new_integration:
        integration_keys = [k for k in INTEGRATION_SECTIONS if k in sections]
        integration_sections_content = {k: sections[k] for k in integration_keys}
        integration_images: dict[str, str] = {}

        if "SEQUENCE_DIAGRAM" in integration_sections_content:
            seq_code = extract_mermaid_code(integration_sections_content["SEQUENCE_DIAGRAM"])
            if seq_code:
                seq_img_path = Path(project["output_dir"]) / f"sequence_v{new_version}.png"
                rendered_seq = render_mermaid_to_png(
                    seq_code, str(seq_img_path),
                    renderer=output_fmt.get("mermaid_renderer", "auto"),
                )
                if rendered_seq:
                    integration_images["SEQUENCE_DIAGRAM"] = str(rendered_seq)
                    logger.info("[Builder] Sequence diagram rendered → %s", rendered_seq)

        append_integration_sections(
            doc_path=doc_output_path_str,
            sections=integration_sections_content,
            image_sections=integration_images,
        )
        logger.info(
            "[Builder] Integration block appended (%d section(s))", len(integration_sections_content)
        )

    # Persist all sections (merged) so subsequent runs can reuse unchanged ones
    _save_sections_cache(project["output_dir"], sections)

    additional_points = generate_additional_points(
        project_name=name,
        changed_files=doc_plan.changed_files,
        associated_document_path=doc_plan.associated_document_path,
        commit_sha=doc_plan.commit_sha,
        model=settings.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=settings.get("max_tokens", 4096),
        mock_mode=settings.get("mock_mode", False),
        cli_mode=settings.get("cli_mode", False),
    )

    if additional_points:
        append_additional_points(doc_output_path_str, additional_points)

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
        is_new_integration=doc_plan.is_new_integration,
    )
