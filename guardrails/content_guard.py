"""
Content Guard — Validates content standards for pipeline outputs.

Checks research YAML files, doc plan YAML files, and generated doc sections
against the standards defined in GUARDRAILS.yaml.
Adapted from the devops toolkit template for the Intelligent Documentation Engine.
"""

import re
import sys
from pathlib import Path

import yaml

from logger import get_project_root, load_guardrails_config, setup_guard_logger


def check_yaml_files(directory: Path, required_fields: list) -> list:
    """Validate all YAML files in a directory have required fields.

    Args:
        directory:       Directory containing YAML files to check.
        required_fields: List of field names that must be present.

    Returns:
        List of issue dicts.
    """
    issues = []
    if not directory.exists():
        return issues

    for yaml_file in directory.glob("*.yaml"):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            issues.append({
                "severity": "ERROR",
                "file": str(yaml_file),
                "message": f"Invalid YAML: {exc}",
                "suggestion": "Fix YAML syntax errors",
            })
            continue

        if data is None:
            issues.append({
                "severity": "WARNING",
                "file": str(yaml_file),
                "message": "YAML file is empty",
                "suggestion": "Populate with content or remove",
            })
            continue

        flat_keys: set = set()
        if isinstance(data, dict):
            flat_keys.update(data.keys())
            for v in data.values():
                if isinstance(v, dict):
                    flat_keys.update(v.keys())

        for field in required_fields:
            if field not in flat_keys:
                issues.append({
                    "severity": "WARNING",
                    "file": str(yaml_file),
                    "message": f'Missing expected field: "{field}"',
                    "suggestion": f'Add "{field}" section to the file',
                })

    return issues


def check_doc_sections(output_dir: Path, required_sections: list) -> list:
    """Check that validation reports confirm all required doc sections were populated.

    Reads testscripts/*_validation_report.yaml files and checks each
    lists all required sections as populated.

    Args:
        output_dir:        testscripts/ directory.
        required_sections: List of section names (e.g. REQUIRED_SECTIONS).

    Returns:
        List of issue dicts.
    """
    issues = []
    if not output_dir.exists():
        return issues

    for report_file in output_dir.glob("*_validation_report.yaml"):
        try:
            with open(report_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            continue

        verdict = data.get("verdict", "UNKNOWN")
        if verdict == "FAIL":
            report_issues = data.get("issues", [])
            for issue in report_issues:
                issues.append({
                    "severity": "WARNING",
                    "file": str(report_file),
                    "message": f"Validation report FAIL: {issue}",
                    "suggestion": "Re-run the pipeline to regenerate the document",
                })

    return issues


def main() -> bool:
    """Run content guard validation."""
    logger = setup_guard_logger("content_guard")
    root = get_project_root()
    config = load_guardrails_config()
    standards = config.get("content_standards", {})

    logger.info("Validating content standards...")
    all_issues: list = []

    scan_fields = standards.get("required_yaml_fields", {}).get("scan_report", [])
    all_issues.extend(check_yaml_files(root / "research", scan_fields))

    plan_fields = standards.get("required_yaml_fields", {}).get("doc_plan", [])
    all_issues.extend(check_yaml_files(root / "plans", plan_fields))

    required_sections = standards.get("required_doc_sections", [])
    all_issues.extend(check_doc_sections(root / "testscripts", required_sections))

    errors = [i for i in all_issues if i["severity"] == "ERROR"]
    warnings = [i for i in all_issues if i["severity"] == "WARNING"]

    for issue in errors:
        logger.error("[%s] %s", issue["file"], issue["message"])
    for issue in warnings:
        logger.warning("[%s] %s", issue["file"], issue["message"])

    if errors:
        logger.error("Content guard FAILED: %d error(s), %d warning(s)", len(errors), len(warnings))
        return False
    elif warnings:
        logger.warning("Content guard PASSED with %d warning(s)", len(warnings))
        return True
    else:
        logger.info("Content guard PASSED")
        return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
