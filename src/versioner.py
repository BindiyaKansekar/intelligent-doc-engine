"""
versioner.py
============
Semantic versioning and archive management for the Intelligent Documentation Engine.

Responsibilities:
  - Read and write ``output/{project}/version_history.json``.
  - Bump major / minor / patch version numbers based on the nature of the change.
  - Provide the next versioned output path for a document.
  - Archive (move) previous documents rather than deleting them.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

logger = logging.getLogger(__name__)

BumpType = Literal["major", "minor", "patch"]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class VersionEntry(TypedDict):
    """One record in version_history.json."""
    version: str
    generated_at: str          # ISO-8601 UTC timestamp
    changed_files: list[str]   # Files that triggered this version
    document_path: str         # Relative path to the generated .docx
    commit_sha: str | None     # Commit that triggered this run (if available)


# ---------------------------------------------------------------------------
# Version arithmetic
# ---------------------------------------------------------------------------

def parse_version(version_string: str) -> tuple[int, int, int]:
    """Parse a ``MAJOR.MINOR.PATCH`` string into a tuple of ints.

    Args:
        version_string: Version string, e.g. ``"1.3.7"``.

    Returns:
        Tuple ``(major, minor, patch)``.

    Raises:
        ValueError: If the string is not a valid three-part version.
    """
    parts = version_string.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid version string: {version_string!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def bump_version(current: str, bump_type: BumpType) -> str:
    """Return the next version string after applying *bump_type*.

    Rules:
      - ``major``: increment major, reset minor and patch to 0.
      - ``minor``: increment minor, reset patch to 0.
      - ``patch``: increment patch only.

    Args:
        current:   Current version string, e.g. ``"1.2.3"``.
        bump_type: One of ``"major"``, ``"minor"``, or ``"patch"``.

    Returns:
        New version string, e.g. ``"1.3.0"``.
    """
    major, minor, patch = parse_version(current)
    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# ---------------------------------------------------------------------------
# Version history persistence
# ---------------------------------------------------------------------------

def load_version_history(output_dir: str) -> list[VersionEntry]:
    """Load the version history for a project from its output directory.

    Args:
        output_dir: Path to the project's output directory (contains
                    ``version_history.json``).

    Returns:
        List of :class:`VersionEntry` dicts, oldest first.  Returns an empty
        list if no history file exists yet.
    """
    history_path = Path(output_dir) / "version_history.json"
    if not history_path.exists():
        return []
    with history_path.open("r", encoding="utf-8") as fh:
        data: list[VersionEntry] = json.load(fh)
    logger.debug("Loaded %d version entries from %s", len(data), history_path)
    return data


def save_version_history(output_dir: str, history: list[VersionEntry]) -> None:
    """Persist *history* to ``{output_dir}/version_history.json``.

    Args:
        output_dir: Project output directory.
        history:    Complete list of version entries to write.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    history_path = out / "version_history.json"
    with history_path.open("w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2)
    logger.info("Version history saved to %s (%d entries)", history_path, len(history))


def get_current_version(history: list[VersionEntry]) -> str:
    """Return the most recent version string from *history*, or ``"0.0.0"``
    if the list is empty.

    Args:
        history: List of version entries as returned by
                 :func:`load_version_history`.

    Returns:
        Latest version string.
    """
    if not history:
        return "0.0.0"
    return history[-1]["version"]


# ---------------------------------------------------------------------------
# Document path / archiving
# ---------------------------------------------------------------------------

def next_document_path(output_dir: str, project_name: str, new_version: str) -> str:
    """Build the output path for the next document version.

    Args:
        output_dir:   Project output directory.
        project_name: Project identifier used in the filename.
        new_version:  Version string for the new document.

    Returns:
        String path of the form ``{output_dir}/AUD_v{new_version}.docx``.
    """
    filename = f"AUD_v{new_version}.docx"
    return str(Path(output_dir) / filename)


def record_new_version(
    output_dir: str,
    project_name: str,
    bump_type: BumpType,
    changed_files: list[str],
    commit_sha: str | None = None,
) -> tuple[str, str]:
    """Compute the next version, append it to history, persist, and return the
    new version string and output document path.

    Args:
        output_dir:    Project output directory.
        project_name:  Project identifier.
        bump_type:     Semver bump level.
        changed_files: Files included in this version.

    Returns:
        Tuple ``(new_version_string, document_output_path)``.
    """
    history = load_version_history(output_dir)
    current = get_current_version(history)
    new_version = bump_version(current, bump_type)
    doc_path = next_document_path(output_dir, project_name, new_version)

    entry: VersionEntry = {
        "version": new_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "changed_files": changed_files,
        "document_path": doc_path,
        "commit_sha": commit_sha,
    }
    history.append(entry)
    save_version_history(output_dir, history)

    logger.info("Recorded version %s for project '%s'", new_version, project_name)
    return new_version, doc_path
