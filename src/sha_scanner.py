"""
sha_scanner.py
==============
SHA-256 change detection for the Intelligent Documentation Engine.

Responsibilities:
  - Walk source directories and compute SHA-256 digests for each tracked file.
  - Load / save the persistent hash store at hash_store/{project_name}.json.
  - Return the diff (new, modified, deleted) between the current scan and the
    last stored snapshot.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class FileDiff(TypedDict):
    """Result of comparing the current scan against the stored hash snapshot."""
    new: list[str]        # Paths that did not exist in the previous snapshot
    modified: list[str]   # Paths whose digest has changed
    deleted: list[str]    # Paths present in the snapshot but no longer on disk
    unchanged: list[str]  # Paths whose digest is identical to the snapshot


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def compute_sha256(file_path: Path) -> str:
    """Compute and return the SHA-256 hex digest of *file_path*.

    Args:
        file_path: Absolute or relative path to the file to hash.

    Returns:
        A 64-character lowercase hex string.

    Raises:
        OSError: If the file cannot be read.
    """
    sha = hashlib.sha256()
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def scan_directory(
    source_paths: list[str],
    file_types: list[str],
) -> dict[str, str]:
    """Walk *source_paths* and return a mapping of ``path → sha256`` for every
    file whose suffix matches one of *file_types*.

    Args:
        source_paths: List of directory paths to scan recursively.
        file_types:   List of file extensions to include, e.g. ``['.py', '.sql']``.

    Returns:
        Dict mapping the string representation of each matched file path to its
        SHA-256 digest.
    """
    results: dict[str, str] = {}
    for raw_path in source_paths:
        root = Path(raw_path)
        if not root.exists():
            logger.warning("Source path does not exist — skipping: %s", root)
            continue
        for file in root.rglob("*"):
            if file.is_file() and file.suffix in file_types:
                try:
                    results[str(file)] = compute_sha256(file)
                except OSError as exc:
                    logger.error("Could not hash %s: %s", file, exc)
    logger.info("Scanned %d files across %d source paths", len(results), len(source_paths))
    return results


def load_hash_store(hash_store_dir: str, project_name: str) -> dict[str, str]:
    """Load the persisted hash snapshot for *project_name* from disk.

    Args:
        hash_store_dir: Directory that contains ``{project_name}.json`` files.
        project_name:   Logical name of the project (used as the filename stem).

    Returns:
        A dict mapping file paths to their last-known SHA-256 digests, or an
        empty dict if no snapshot exists yet.
    """
    store_path = Path(hash_store_dir) / f"{project_name}.json"
    if not store_path.exists():
        logger.debug("No existing hash store found for project '%s'", project_name)
        return {}
    with store_path.open("r", encoding="utf-8") as fh:
        data: dict[str, str] = json.load(fh)
    logger.debug("Loaded %d hashes from %s", len(data), store_path)
    return data


def save_hash_store(
    hash_store_dir: str,
    project_name: str,
    hashes: dict[str, str],
) -> None:
    """Persist *hashes* to ``{hash_store_dir}/{project_name}.json``.

    Args:
        hash_store_dir: Directory that will hold the snapshot file.
        project_name:   Logical name of the project.
        hashes:         Mapping of file path → SHA-256 digest to persist.
    """
    store_dir = Path(hash_store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    store_path = store_dir / f"{project_name}.json"
    with store_path.open("w", encoding="utf-8") as fh:
        json.dump(hashes, fh, indent=2, sort_keys=True)
    logger.info("Saved %d hashes to %s", len(hashes), store_path)


def diff_hashes(
    current: dict[str, str],
    previous: dict[str, str],
) -> FileDiff:
    """Compare *current* scan results against the *previous* snapshot.

    Args:
        current:  Mapping of ``path → sha256`` from the latest scan.
        previous: Mapping of ``path → sha256`` from the stored snapshot.

    Returns:
        A :class:`FileDiff` with keys ``new``, ``modified``, ``deleted``, and
        ``unchanged``.
    """
    current_keys = set(current)
    previous_keys = set(previous)

    new = sorted(current_keys - previous_keys)
    deleted = sorted(previous_keys - current_keys)
    modified: list[str] = []
    unchanged: list[str] = []

    for path in sorted(current_keys & previous_keys):
        if current[path] != previous[path]:
            modified.append(path)
        else:
            unchanged.append(path)

    logger.info(
        "Diff complete — new: %d, modified: %d, deleted: %d, unchanged: %d",
        len(new), len(modified), len(deleted), len(unchanged),
    )
    return FileDiff(new=new, modified=modified, deleted=deleted, unchanged=unchanged)
