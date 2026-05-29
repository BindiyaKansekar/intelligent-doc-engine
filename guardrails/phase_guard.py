"""
Phase Guard — Enforces phase dependency rules.

Checks that prerequisites for each phase are met before allowing that phase
to proceed. Uses completion markers defined in GUARDRAILS.yaml.
Adapted from the devops toolkit template for the Intelligent Documentation Engine.
"""

import sys
from pathlib import Path

from logger import get_project_root, load_guardrails_config, setup_guard_logger


def check_phase_prerequisites(project_root: Path) -> list:
    """Check that all phase prerequisites are satisfied.

    Args:
        project_root: Root directory of the project.

    Returns:
        List of issue dicts (INFO-level items are pending prerequisites, not failures).
    """
    issues = []
    config = load_guardrails_config()
    phase_deps = config.get("phase_dependencies", {})

    if not phase_deps:
        issues.append({
            "severity": "WARNING",
            "file": "GUARDRAILS.yaml",
            "message": "No phase_dependencies defined",
            "suggestion": "Define phase_dependencies in GUARDRAILS.yaml",
        })
        return issues

    completed_phases: set = set()
    for phase_name, phase_config in phase_deps.items():
        marker = phase_config.get("completion_marker", "")
        marker_path = project_root / marker
        if marker_path.exists():
            if marker_path.is_dir():
                real_files = [f for f in marker_path.iterdir() if f.name != ".gitkeep"]
                if real_files:
                    completed_phases.add(phase_name)
            else:
                completed_phases.add(phase_name)

    for phase_name, phase_config in phase_deps.items():
        prerequisites = phase_config.get("prerequisites", [])
        for prereq in prerequisites:
            if prereq not in completed_phases:
                issues.append({
                    "severity": "INFO",
                    "file": phase_config.get("completion_marker", ""),
                    "message": (
                        f'Phase "{phase_name}" prerequisite not yet complete: "{prereq}"'
                    ),
                    "suggestion": f"Complete {prereq} before starting {phase_name}",
                })

    return issues


def get_current_phase(project_root: Path) -> str:
    """Determine the current phase based on completion markers.

    Args:
        project_root: Root directory of the project.

    Returns:
        Name of the most recently completed phase, or ``"not_started"``.
    """
    config = load_guardrails_config()
    phase_deps = config.get("phase_dependencies", {})

    current_phase = "not_started"
    for phase_name, phase_config in phase_deps.items():
        marker = phase_config.get("completion_marker", "")
        marker_path = project_root / marker
        if marker_path.exists():
            if marker_path.is_dir():
                real_files = [f for f in marker_path.iterdir() if f.name != ".gitkeep"]
                if real_files:
                    current_phase = phase_name
            else:
                current_phase = phase_name

    return current_phase


def main() -> bool:
    """Run phase guard validation."""
    logger = setup_guard_logger("phase_guard")
    root = get_project_root()

    logger.info("Validating phase dependencies...")
    current = get_current_phase(root)
    logger.info("Current phase: %s", current)

    issues = check_phase_prerequisites(root)

    errors = [i for i in issues if i["severity"] == "ERROR"]
    infos = [i for i in issues if i["severity"] == "INFO"]

    for issue in errors:
        logger.error(issue["message"])
    for issue in infos:
        logger.info(issue["message"])

    if errors:
        logger.error("Phase guard FAILED: %d error(s)", len(errors))
        return False
    else:
        logger.info("Phase guard PASSED (%d pending prerequisite(s))", len(infos))
        return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
