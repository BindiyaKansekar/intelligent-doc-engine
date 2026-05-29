"""
Folder Guard — Enforces agent-to-folder boundary rules.

Checks that all agent-designated folders exist as defined in GUARDRAILS.yaml.
Adapted from the devops toolkit template for the Intelligent Documentation Engine.
"""

import sys
from pathlib import Path
from typing import Dict, List

from logger import get_project_root, load_guardrails_config, setup_guard_logger


def get_folder_boundaries(config: dict) -> Dict[str, List[str]]:
    """Extract allowed_write folder lists from GUARDRAILS.yaml folder_boundaries.

    Args:
        config: Parsed GUARDRAILS.yaml dict.

    Returns:
        Dict mapping agent name to list of allowed write folders.
    """
    boundaries: Dict[str, List[str]] = {}
    fb = config.get("folder_boundaries", {})
    for agent_name, agent_config in fb.items():
        if isinstance(agent_config, dict):
            boundaries[agent_name] = agent_config.get("allowed_write", [])
        elif isinstance(agent_config, list):
            boundaries[agent_name] = agent_config
    return boundaries


def validate_folder_boundaries(project_root: Path) -> list:
    """Validate that all agent-designated folders exist.

    Args:
        project_root: Root directory of the project.

    Returns:
        List of issue dicts.
    """
    issues = []
    config = load_guardrails_config()
    boundaries = get_folder_boundaries(config)

    if not boundaries:
        issues.append({
            "severity": "WARNING",
            "file": "GUARDRAILS.yaml",
            "message": "No folder_boundaries defined",
            "suggestion": "Define folder_boundaries in GUARDRAILS.yaml",
        })
        return issues

    all_folders: set = set()
    for folders in boundaries.values():
        all_folders.update(folders)

    for folder in sorted(all_folders):
        folder_path = project_root / folder
        if not folder_path.exists():
            issues.append({
                "severity": "WARNING",
                "file": folder,
                "message": f"Agent-designated folder does not exist: {folder}",
                "suggestion": f"Create the {folder} directory",
            })

    return issues


def main() -> bool:
    """Run folder guard validation."""
    logger = setup_guard_logger("folder_guard")
    root = get_project_root()

    logger.info("Validating folder boundaries...")
    issues = validate_folder_boundaries(root)

    errors = [i for i in issues if i["severity"] == "ERROR"]
    warnings = [i for i in issues if i["severity"] == "WARNING"]

    for issue in errors:
        logger.error(issue["message"])
    for issue in warnings:
        logger.warning(issue["message"])

    if errors:
        logger.error("Folder guard FAILED: %d error(s)", len(errors))
        return False
    elif warnings:
        logger.warning("Folder guard PASSED with %d warning(s)", len(warnings))
        return True
    else:
        logger.info("Folder guard PASSED")
        return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
