"""
Master Guardrails Runner — Executes all guardrail checks.

Runs all guard scripts in sequence and produces a combined report.
Adapted from the devops toolkit template for the Intelligent Documentation Engine.

Usage:
    python guardrails/run_guardrails.py
"""

import sys
from datetime import datetime
from pathlib import Path

GUARDRAILS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(GUARDRAILS_DIR))

from logger import setup_guard_logger, get_project_root  # noqa: E402
import input_guard   # noqa: E402
import folder_guard  # noqa: E402
import phase_guard   # noqa: E402
import content_guard # noqa: E402


def main() -> bool:
    """Run all guardrail checks and report results."""
    logger = setup_guard_logger("run_guardrails")
    logger.info("=" * 60)
    logger.info("Guardrails Validation — Intelligent Documentation Engine")
    logger.info("Timestamp: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    guards = [
        ("Input Guard",   input_guard.main),
        ("Folder Guard",  folder_guard.main),
        ("Phase Guard",   phase_guard.main),
        ("Content Guard", content_guard.main),
    ]

    results: dict = {}
    for name, guard_fn in guards:
        logger.info("\n--- %s ---", name)
        try:
            passed = guard_fn()
            results[name] = "PASSED" if passed else "FAILED"
        except Exception as exc:
            logger.error("%s raised exception: %s", name, exc)
            results[name] = "ERROR"

    logger.info("\n" + "=" * 60)
    logger.info("Guardrails Summary")
    logger.info("=" * 60)

    all_passed = True
    for name, status in results.items():
        icon = "PASS" if status == "PASSED" else "FAIL"
        logger.info("  [%s] %s: %s", icon, name, status)
        if status != "PASSED":
            all_passed = False

    if all_passed:
        logger.info("\nAll guardrails PASSED")
    else:
        logger.warning("\nSome guardrails FAILED — review issues above")

    return all_passed


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
