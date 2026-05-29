"""
Input Guard — Validates config.json has the required structure.

Checks that config.json is present, valid JSON, and contains
required projects and settings fields before the pipeline runs.
"""

import json
import sys
from pathlib import Path

from logger import get_project_root, setup_guard_logger

REQUIRED_TOP_LEVEL = ["projects", "settings"]
REQUIRED_PROJECT_FIELDS = ["name", "source_paths", "file_types", "output_dir"]
REQUIRED_SETTINGS_FIELDS = ["hash_store_dir", "template_path"]


def validate_config(config_path: Path) -> list:
    """Validate config.json structure and required fields.

    Args:
        config_path: Path to config.json.

    Returns:
        List of issue dicts with severity, file, message, and suggestion.
    """
    issues = []

    if not config_path.exists():
        issues.append({
            "severity": "ERROR",
            "file": str(config_path),
            "message": "config.json not found",
            "suggestion": "Create config.json from the template",
        })
        return issues

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            config = json.load(fh)
    except json.JSONDecodeError as exc:
        issues.append({
            "severity": "ERROR",
            "file": str(config_path),
            "message": f"Invalid JSON: {exc}",
            "suggestion": "Fix JSON syntax errors in config.json",
        })
        return issues

    for field in REQUIRED_TOP_LEVEL:
        if field not in config:
            issues.append({
                "severity": "ERROR",
                "file": str(config_path),
                "message": f"Missing required top-level field: {field}",
                "suggestion": f'Add "{field}" section to config.json',
            })

    projects = config.get("projects", [])
    if not projects:
        issues.append({
            "severity": "WARNING",
            "file": str(config_path),
            "message": "No projects defined in config.json",
            "suggestion": "Add at least one project to the projects array",
        })

    for i, project in enumerate(projects):
        for field in REQUIRED_PROJECT_FIELDS:
            if field not in project:
                issues.append({
                    "severity": "ERROR",
                    "file": str(config_path),
                    "message": f"Project[{i}] missing required field: {field}",
                    "suggestion": f'Add "{field}" to project entry {i}',
                })

    settings = config.get("settings", {})
    for field in REQUIRED_SETTINGS_FIELDS:
        if field not in settings:
            issues.append({
                "severity": "ERROR",
                "file": str(config_path),
                "message": f"settings missing required field: {field}",
                "suggestion": f'Add "{field}" to the settings section',
            })

    return issues


def main() -> bool:
    """Run input guard validation."""
    logger = setup_guard_logger("input_guard")
    root = get_project_root()
    config_path = root / "config.json"

    logger.info("Validating config.json...")
    issues = validate_config(config_path)

    errors = [i for i in issues if i["severity"] == "ERROR"]
    warnings = [i for i in issues if i["severity"] == "WARNING"]

    for issue in errors:
        logger.error("[%s] %s", issue["file"], issue["message"])
    for issue in warnings:
        logger.warning("[%s] %s", issue["file"], issue["message"])

    if errors:
        logger.error("Input guard FAILED: %d error(s)", len(errors))
        return False
    elif warnings:
        logger.warning("Input guard PASSED with %d warning(s)", len(warnings))
        return True
    else:
        logger.info("Input guard PASSED")
        return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
