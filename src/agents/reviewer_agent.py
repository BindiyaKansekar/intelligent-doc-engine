"""
reviewer_agent.py
=================
Phase 4 (Review) agent for the Intelligent Documentation Engine.

Responsibilities:
  - Validate the generated Word document for completeness.
  - Check that no {{PLACEHOLDER}} tokens remain unfilled.
  - Verify all REQUIRED_SECTIONS are present and non-empty.
  - Persist the SHA-256 hash store ONLY after a successful validation.
  - Write a structured validation report to testscripts/{project_name}_validation_report.yaml.

Idempotency guarantee:
  Hash persistence happens ONLY here, ONLY on PASS. If validation fails, the
  next pipeline run retries from scratch because the hash store is unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..claude_engine import REQUIRED_SECTIONS
from ..git_tracker import save_commit_state
from ..sha_scanner import save_hash_store
from .builder_agent import BuildResult
from .scanner_agent import ScanResult

logger = logging.getLogger(__name__)

_UNFILLED_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


@dataclass
class ReviewResult:
    """Output of the Reviewer Agent (Phase 4)."""

    project_name: str
    passed: bool
    issues: list[str] = field(default_factory=list)
    report_path: Path = field(default_factory=lambda: Path("testscripts"))


def run(
    project: dict[str, Any],
    settings: dict[str, Any],
    build_result: BuildResult,
    scan_result: ScanResult,
) -> ReviewResult:
    """Execute Phase 4: validate document and persist hashes on success.

    Writes ``testscripts/{project_name}_validation_report.yaml``.
    Persists hashes to ``hash_store/`` ONLY if validation passes.

    Args:
        project:      Project entry from ``config.json``.
        settings:     Global settings dict from ``config.json``.
        build_result: Output from :func:`builder_agent.run` (Phase 3).
        scan_result:  Output from :func:`scanner_agent.run` (Phase 1).

    Returns:
        :class:`ReviewResult` with verdict and report path.
    """
    name: str = project["name"]
    logger.info("[Reviewer] Phase 4 starting for project: %s", name)

    issues: list[str] = []
    checks: dict[str, Any] = {}
    doc_path = build_result.doc_output_path

    # Check 1: Document exists
    checks["document_exists"] = doc_path.exists()
    if not checks["document_exists"]:
        issues.append(f"Output document not found: {doc_path}")

    # Check 2: Non-zero size
    size = doc_path.stat().st_size if doc_path.exists() else 0
    checks["document_size_bytes"] = size
    if size == 0:
        issues.append(f"Output document is empty (0 bytes): {doc_path}")

    # Check 3: All REQUIRED_SECTIONS populated
    empty_sections = [k for k in REQUIRED_SECTIONS if not build_result.sections.get(k)]
    checks["all_sections_populated"] = len(empty_sections) == 0
    if empty_sections:
        issues.append(f"Empty or missing sections: {', '.join(empty_sections)}")

    # Check 4: No unfilled placeholders in replacements values
    unfilled = [
        k for k, v in build_result.replacements.items()
        if _UNFILLED_PLACEHOLDER_RE.search(v or "")
    ]
    checks["no_unfilled_placeholders"] = len(unfilled) == 0
    if unfilled:
        issues.append(f"Unfilled placeholder tokens in fields: {', '.join(unfilled)}")

    # Check 5: Additional points file exists and has content
    points_exists = build_result.additional_points_path.exists()
    checks["additional_points_file_exists"] = points_exists
    if not points_exists:
        issues.append(f"Additional points file not found: {build_result.additional_points_path}")
    else:
        points_size = build_result.additional_points_path.stat().st_size
        checks["additional_points_file_size_bytes"] = points_size
        if points_size == 0:
            issues.append(f"Additional points file is empty: {build_result.additional_points_path}")

    passed = len(issues) == 0
    hash_status = "SKIPPED"

    if passed:
        save_hash_store(settings["hash_store_dir"], name, scan_result.current_hashes)
        if scan_result.commit_state_updates:
            for repo_root, repo_commit_sha in scan_result.commit_state_updates.items():
                save_commit_state(
                    settings["hash_store_dir"],
                    name,
                    repo_commit_sha,
                    repo_root=repo_root,
                )
        elif scan_result.commit_sha:
            save_commit_state(settings["hash_store_dir"], name, scan_result.commit_sha)
        hash_status = "PERSISTED"
        logger.info(
            "[Reviewer] Phase 4 PASS for '%s' — hashes persisted to %s/",
            name, settings["hash_store_dir"],
        )
    else:
        logger.error(
            "[Reviewer] Phase 4 FAIL for '%s' — %d issue(s). Hashes NOT persisted.",
            name, len(issues),
        )
        for issue in issues:
            logger.error("[Reviewer]   - %s", issue)

    report_path = _write_validation_report(
        name, build_result, checks, passed, issues, hash_status, settings,
    )

    return ReviewResult(
        project_name=name,
        passed=passed,
        issues=issues,
        report_path=report_path,
    )


def _write_validation_report(
    name: str,
    build_result: BuildResult,
    checks: dict[str, Any],
    passed: bool,
    issues: list[str],
    hash_status: str,
    settings: dict[str, Any],
) -> Path:
    """Write a validation report to testscripts/{name}_validation_report.yaml.

    Args:
        name:         Project name.
        build_result: Builder output being reviewed.
        checks:       Dict of check name → bool/value result.
        passed:       Overall pass/fail verdict.
        issues:       List of issue strings (empty on PASS).
        hash_status:  ``"PERSISTED"`` or ``"SKIPPED"``.
        settings:     Global settings dict.

    Returns:
        Path to the written report file.
    """
    testscripts_dir = Path("testscripts")
    testscripts_dir.mkdir(parents=True, exist_ok=True)
    report_path = testscripts_dir / f"{name}_validation_report.yaml"

    report: dict[str, Any] = {
        "metadata": {
            "project_name": name,
            "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "agent": "reviewer",
            "document_path": str(build_result.doc_output_path),
            "additional_points_path": str(build_result.additional_points_path),
            "version": build_result.new_version,
        },
        "verdict": "PASS" if passed else "FAIL",
        "checks": checks,
        "issues": issues,
        "hash_persistence": {
            "status": hash_status,
            "hash_store_path": f"{settings['hash_store_dir']}/{name}.json",
            "note": (
                "Hashes persisted ONLY after successful validation — idempotency guarantee"
            ),
        },
    }

    with report_path.open("w", encoding="utf-8") as fh:
        yaml.dump(report, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)

    logger.info("[Reviewer] Validation report written to %s", report_path)
    return report_path
