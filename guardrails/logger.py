"""
Centralized logging for guardrail checks.

Writes log entries to thoughts/logs/ directory.
Adapted from the devops toolkit template for the Intelligent Documentation Engine.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml


def get_project_root() -> Path:
    """Return the project root (parent of guardrails/)."""
    return Path(__file__).resolve().parent.parent


def load_guardrails_config() -> dict:
    """Load GUARDRAILS.yaml from the project root."""
    config_path = get_project_root() / "GUARDRAILS.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def setup_guard_logger(
    guard_name: str,
    log_dir: Optional[Path] = None,
) -> logging.Logger:
    """Set up a logger for a specific guardrail check.

    Args:
        guard_name: Short name for this guard (used as logger name suffix).
        log_dir:    Directory for log files; defaults to thoughts/logs/.

    Returns:
        Configured :class:`logging.Logger` instance.
    """
    config = load_guardrails_config()
    log_config = config.get("logging", {})

    if log_dir is None:
        log_dir = get_project_root() / log_config.get("log_directory", "thoughts/logs/")

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    level_str = log_config.get("log_level", "INFO")
    level = getattr(logging, level_str.upper(), logging.INFO)

    logger = logging.getLogger(f"guardrail.{guard_name}")
    logger.setLevel(level)

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_file = log_dir / f"guardrails_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
