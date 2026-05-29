"""
runner.py (Lead Orchestrator)
==============================
Lead Orchestrator for the Intelligent Documentation Engine.

Orchestrates the 4-phase documentation pipeline with quality gates:

  Phase 1: RESEARCH  — Scanner Agent scans source files, detects changes
  Gate 1:             Skip if no changes (efficiency — no API cost, no version bump)
  Phase 2: PLANNING  — Analyzer Agent plans sections and version bump type
  Gate 2:             Devil's Advocate validates the doc plan
  Phase 3: BUILD     — Builder Agent calls Claude API, assigns version, writes .docx
  Gate 3:             Peer Reviewer validates document completeness
  Phase 4: REVIEW    — Reviewer Agent validates document, persists hashes (idempotency)

Projects run concurrently via asyncio.gather.
Never silently swallow errors — all exceptions are logged and propagated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.config
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .agents import analyzer_agent, builder_agent, reviewer_agent, scanner_agent
from .claude_engine import REQUIRED_SECTIONS


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def configure_logging(log_dir: str, log_level: str = "INFO") -> None:
    """Configure root logger to write to both console and a rolling file.

    Args:
        log_dir:   Directory where log files are written.
        log_level: String log level, e.g. ``"DEBUG"`` or ``"INFO"``.
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.log"

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )
    logging.getLogger(__name__).info("Logging initialised — file: %s", log_file)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config.json") -> dict[str, Any]:
    """Load and return the contents of *config_path*.

    Args:
        config_path: Path to the JSON configuration file.

    Returns:
        Parsed configuration dict.

    Raises:
        FileNotFoundError: If the config file is absent.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path.resolve()}")
    with path.open("r", encoding="utf-8") as fh:
        config: dict[str, Any] = json.load(fh)
    return config


# ---------------------------------------------------------------------------
# Phase gates
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """Result of a phase gate check."""

    passed: bool
    issues: list[str] = field(default_factory=list)


def _gate2_devils_advocate(doc_plan: analyzer_agent.DocPlan) -> GateResult:
    """Gate 2: Devil's Advocate validates the doc plan.

    Checks completeness and consistency before any expensive API calls.

    Args:
        doc_plan: Output from Phase 2 (Analyzer Agent).

    Returns:
        :class:`GateResult` with pass/fail and any blocking issues.
    """
    issues: list[str] = []
    valid_bump_types = {"major", "minor", "patch"}

    if not doc_plan.sections_to_generate:
        issues.append("sections_to_generate is empty — nothing to generate")

    if doc_plan.version_bump_type not in valid_bump_types:
        issues.append(
            f"version_bump_type '{doc_plan.version_bump_type}' is invalid "
            f"(must be one of: {', '.join(sorted(valid_bump_types))})"
        )

    if not doc_plan.changed_files:
        issues.append("changed_files is empty — no files to document")

    if not doc_plan.plan_path.exists():
        issues.append(f"Doc plan file not found: {doc_plan.plan_path}")

    return GateResult(passed=len(issues) == 0, issues=issues)


def _gate3_peer_review(build_result: builder_agent.BuildResult) -> GateResult:
    """Gate 3: Peer Reviewer validates the generated document.

    Checks that the document exists, is non-zero, and all required sections
    are populated.

    Args:
        build_result: Output from Phase 3 (Builder Agent).

    Returns:
        :class:`GateResult` with pass/fail and any blocking issues.
    """
    issues: list[str] = []

    if not build_result.doc_output_path.exists():
        issues.append(f"Output document not found: {build_result.doc_output_path}")
    elif build_result.doc_output_path.stat().st_size == 0:
        issues.append(f"Output document is empty: {build_result.doc_output_path}")

    empty_sections = [k for k in REQUIRED_SECTIONS if not build_result.sections.get(k)]
    if empty_sections:
        issues.append(f"Empty required sections: {', '.join(empty_sections)}")

    return GateResult(passed=len(issues) == 0, issues=issues)


# ---------------------------------------------------------------------------
# Per-project pipeline (Lead Orchestrator)
# ---------------------------------------------------------------------------

async def process_project(project: dict[str, Any], settings: dict[str, Any]) -> None:
    """Orchestrate the 4-phase pipeline for a single *project*.

    Phase gates are enforced between phases. Gate 1 stops the pipeline
    immediately if no files changed (efficiency). Hashes are persisted only
    after Phase 4 validation passes (idempotency).

    Args:
        project:  Project entry from ``config.json``.
        settings: Global settings dict from ``config.json``.

    Raises:
        RuntimeError: If Gate 2 (Devil's Advocate) or Gate 3 (Peer Review) fails.
    """
    log = logging.getLogger(__name__)
    name: str = project["name"]
    log.info("=== [Orchestrator] Pipeline starting for project: %s ===", name)

    # ------------------------------------------------------------------
    # Phase 1: Research (Scanner Agent)
    # ------------------------------------------------------------------
    log.info("[Orchestrator] Phase 1: Scanning '%s'...", name)
    scan_result = await asyncio.to_thread(scanner_agent.run, project, settings)

    # Gate 1: Skip if no changes (efficiency philosophy)
    if not scan_result.changed_files:
        log.info(
            "[Orchestrator] Gate 1: No changes detected for '%s' — pipeline skipped. "
            "(No API cost, no version bump, hashes unchanged.)",
            name,
        )
        return

    log.info(
        "[Orchestrator] Gate 1: PASS — %d changed file(s) detected for '%s'",
        len(scan_result.changed_files), name,
    )

    # ------------------------------------------------------------------
    # Phase 2: Planning (Analyzer Agent)
    # ------------------------------------------------------------------
    log.info("[Orchestrator] Phase 2: Planning '%s'...", name)
    doc_plan = await asyncio.to_thread(analyzer_agent.run, project, settings, scan_result)

    # Gate 2: Devil's Advocate validates the doc plan
    gate2 = _gate2_devils_advocate(doc_plan)
    if not gate2.passed:
        raise RuntimeError(
            f"[Orchestrator] Gate 2 (Devil's Advocate) FAILED for '{name}': "
            + "; ".join(gate2.issues)
        )
    log.info("[Orchestrator] Gate 2: PASS — doc plan validated for '%s'", name)

    # ------------------------------------------------------------------
    # Phase 3: Build (Builder Agent)
    # ------------------------------------------------------------------
    log.info("[Orchestrator] Phase 3: Building doc for '%s'...", name)
    build_result = await asyncio.to_thread(builder_agent.run, project, settings, doc_plan)

    # Gate 3: Peer Reviewer validates generated document
    gate3 = _gate3_peer_review(build_result)
    if not gate3.passed:
        raise RuntimeError(
            f"[Orchestrator] Gate 3 (Peer Review) FAILED for '{name}': "
            + "; ".join(gate3.issues)
        )
    log.info("[Orchestrator] Gate 3: PASS — document validated for '%s'", name)

    # ------------------------------------------------------------------
    # Phase 4: Review (Reviewer Agent) — persists hashes ONLY here
    # ------------------------------------------------------------------
    log.info("[Orchestrator] Phase 4: Final review and hash persistence for '%s'...", name)
    review_result = await asyncio.to_thread(
        reviewer_agent.run, project, settings, build_result, scan_result
    )

    if not review_result.passed:
        raise RuntimeError(
            f"[Orchestrator] Phase 4 (Review) FAILED for '{name}': "
            + "; ".join(review_result.issues)
        )

    log.info(
        "=== [Orchestrator] Pipeline COMPLETE for '%s' — document: %s ===",
        name, build_result.doc_output_path,
    )


# ---------------------------------------------------------------------------
# Orchestration entry-point
# ---------------------------------------------------------------------------

async def run_all(config_path: str = "config.json") -> None:
    """Load config and concurrently process all configured projects.

    Args:
        config_path: Path to ``config.json``.
    """
    load_dotenv()
    config = load_config(config_path)
    settings: dict[str, Any] = config.get("settings", {})

    configure_logging(
        settings.get("log_dir", "logs"),
        settings.get("log_level", "INFO"),
    )

    projects: list[dict[str, Any]] = config.get("projects", [])
    if not projects:
        logging.getLogger(__name__).warning("No projects defined in %s", config_path)
        return

    # Run all projects concurrently — concurrency preserved from original design
    await asyncio.gather(
        *(process_project(p, settings) for p in projects),
        return_exceptions=False,
    )


def main() -> None:
    """CLI entry-point — runs :func:`run_all` via the asyncio event loop."""
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
