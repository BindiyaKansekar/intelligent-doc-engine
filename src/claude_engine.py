"""
claude_engine.py
================
Anthropic API integration for the Intelligent Documentation Engine.

Responsibilities:
  - Accept changed-file content and project metadata.
  - Send structured prompts to the Claude API (with prompt caching where
    eligible).
  - Parse the response into a dict of documentation sections that map directly
    to the {{PLACEHOLDER}} fields used by template_writer.py.

Mock mode:
  Set ``mock_mode=True`` in ``config.json`` (settings.mock_mode) or pass
  ``mock_mode=True`` directly to :func:`generate_documentation_sections`.
  The function then returns realistic placeholder sections built from the
  actual file list — no API key required.  Swap to ``false`` the moment you
  have an ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Section keys that the engine is expected to return.  These must match the
# placeholder names (minus braces) used in the Word template.
REQUIRED_SECTIONS = [
    "OVERVIEW",
    "FILE_INVENTORY",
    "DATA_FLOWS",
    "DEPENDENCIES",
    "CONFIGURATION",
    "KNOWN_ISSUES",
]


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _read_file_content(file_path: str, max_chars: int = 8_000) -> str:
    """Return the text content of *file_path*, truncated to *max_chars*.

    Args:
        file_path: Path to the source file.
        max_chars: Maximum number of characters to read (avoids huge prompts).

    Returns:
        File content as a string, with a truncation notice if applicable.
    """
    path = Path(file_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read %s: %s", file_path, exc)
        return f"[Could not read file: {exc}]"

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"
    return text


def _build_file_block(changed_files: list[str]) -> str:
    """Produce a formatted block of all changed file contents for the prompt.

    Args:
        changed_files: List of file paths whose content should be included.

    Returns:
        A multi-section string with each file's path and content delimited.
    """
    blocks: list[str] = []
    for fp in changed_files:
        content = _read_file_content(fp)
        blocks.append(f"=== FILE: {fp} ===\n{content}\n")
    return "\n".join(blocks)


def _read_document_context(document_path: str | None, max_chars: int = 10_000) -> str:
    """Return plain-text context from an existing .docx document if present."""
    if not document_path:
        return ""

    path = Path(document_path)
    if not path.exists():
        logger.warning("Associated document not found: %s", path)
        return ""

    if path.suffix.lower() != ".docx":
        logger.warning("Associated document is not .docx: %s", path)
        return ""

    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx is not available. Skipping associated document context.")
        return ""

    try:
        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as exc:
        logger.warning("Failed to read associated document %s: %s", path, exc)
        return ""

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated associated doc at {max_chars} chars ...]"
    return text


# ---------------------------------------------------------------------------
# Mock mode — no API key needed
# ---------------------------------------------------------------------------

def _generate_mock_sections(
    project_name: str,
    changed_files: list[str],
    associated_document_path: str | None = None,
    commit_sha: str | None = None,
) -> dict[str, str]:
    """Return realistic placeholder sections built from the actual file list.

    Reads each file's name, suffix, and size so the output is grounded in
    reality even without an API call.  Replace with the live path once an
    ANTHROPIC_API_KEY is available.

    Args:
        project_name:  Logical project name.
        changed_files: List of file paths that changed.

    Returns:
        Dict mapping section keys to placeholder text.
    """
    logger.info("[MOCK MODE] Generating placeholder sections for %d file(s)", len(changed_files))

    # Build a simple file inventory from the real file list
    inventory_lines: list[str] = []
    dep_hints: set[str] = set()
    config_hints: list[str] = []

    for fp in changed_files:
        p = Path(fp)
        size = p.stat().st_size if p.exists() else 0
        ext = p.suffix.lstrip(".").upper() or "FILE"
        inventory_lines.append(f"{p.name}  |  {ext}  |  {size:,} bytes  |  [purpose to be filled]")

        # Cheap dependency sniffing — look for import lines
        if p.suffix == ".py" and p.exists():
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[:60]:
                    m = re.match(r"^(?:import|from)\s+([\w]+)", line.strip())
                    if m:
                        dep_hints.add(m.group(1))
            except OSError:
                pass

        if p.suffix in {".json", ".yaml", ".yml", ".env"} and p.exists():
            config_hints.append(str(p))

    inventory_text = "\n".join(inventory_lines) if inventory_lines else "(no files scanned)"

    std_libs = {
        "os", "sys", "re", "json", "pathlib", "logging", "typing", "datetime",
        "hashlib", "shutil", "asyncio", "io", "collections", "functools",
        "itertools", "contextlib", "dataclasses", "enum", "abc", "copy",
        "__future__",
    }
    third_party = sorted(dep_hints - std_libs)
    deps_text = (
        "\n".join(f"- {d}" for d in third_party)
        if third_party
        else "No third-party imports detected in changed files."
    )

    config_text = (
        "Config / env files detected:\n" + "\n".join(f"- {c}" for c in config_hints)
        if config_hints
        else "No configuration files detected in changed files.\nSet ANTHROPIC_API_KEY in .env to switch to live mode."
    )

    prior_doc_note = (
        f"Prior document context source: {associated_document_path}."
        if associated_document_path
        else "No prior generated document was available."
    )
    commit_note = f"Triggered by commit: {commit_sha}." if commit_sha else "Commit SHA not available."

    return {
        "OVERVIEW": (
            f"{project_name} — documentation generated in MOCK MODE (no API key).\n"
            f"{len(changed_files)} file(s) were detected as new or modified.\n"
            f"{commit_note}\n"
            f"{prior_doc_note}\n"
            "Replace mock_mode with false in config.json once ANTHROPIC_API_KEY is set "
            "to generate AI-authored content."
        ),
        "FILE_INVENTORY": inventory_text,
        "DATA_FLOWS": (
            "[MOCK] Data flow analysis requires a live Claude API call.\n"
            "Add your ANTHROPIC_API_KEY to .env and set mock_mode: false to enable."
        ),
        "DEPENDENCIES": deps_text,
        "CONFIGURATION": config_text,
        "KNOWN_ISSUES": (
            "[MOCK] Known-issue analysis requires a live Claude API call.\n"
            "Add your ANTHROPIC_API_KEY to .env and set mock_mode: false to enable."
        ),
    }


# ---------------------------------------------------------------------------
# Core API call
# ---------------------------------------------------------------------------

def generate_documentation_sections(
    project_name: str,
    changed_files: list[str],
    associated_document_path: str | None = None,
    commit_sha: str | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    mock_mode: bool = False,
) -> dict[str, str]:
    """Return populated documentation sections for *changed_files*.

    In **mock mode** (``mock_mode=True``, or no ``ANTHROPIC_API_KEY`` in env)
    the function builds placeholder content from the real file list without
    any network call.

    In **live mode** it calls the Claude API and parses the tagged response.

    Args:
        project_name:  Human-readable project identifier included in the prompt.
        changed_files: List of file paths that have been added or modified.
        model:         Claude model ID to use (live mode only).
        max_tokens:    Maximum tokens to request in the response (live mode only).
        mock_mode:     If ``True``, skip the API call entirely.

    Returns:
        A dict mapping section keys (e.g. ``"OVERVIEW"``) to their generated
        text.  All keys in :data:`REQUIRED_SECTIONS` are guaranteed present.

    Raises:
        anthropic.APIError: If the API call fails (live mode only).
    """
    # Auto-fallback: if no key is set, silently use mock mode
    if mock_mode or not os.getenv("ANTHROPIC_API_KEY"):
        if not mock_mode:
            logger.warning(
                "ANTHROPIC_API_KEY not set — falling back to mock mode automatically. "
                "Add key to .env and set mock_mode: false in config.json for live output."
            )
        sections = _generate_mock_sections(
            project_name=project_name,
            changed_files=changed_files,
            associated_document_path=associated_document_path,
            commit_sha=commit_sha,
        )
        for key in REQUIRED_SECTIONS:
            sections.setdefault(key, "")
        return sections

    # --- Live path --------------------------------------------------------
    import anthropic  # deferred so missing package doesn't break mock mode

    client = anthropic.Anthropic()

    file_block = _build_file_block(changed_files)
    prior_document_text = _read_document_context(associated_document_path)

    system_prompt = (
        "You are a senior software documentation engineer. "
        "You produce concise, accurate Application Understanding Documents (AUDs) "
        "from source code. Your output must be plain text — no markdown headings, "
        "no bullet symbols — unless they appear naturally in the source. "
        "Each section begins with the exact tag [SECTION:<NAME>] on its own line "
        "and ends with [/SECTION:<NAME>] on its own line."
    )

    user_prompt = f"""Project: {project_name}
Triggering commit: {commit_sha or 'unknown'}

The following files have been added or modified since the last documentation run.
Analyse them and produce the six AUD sections listed below.

{file_block}

Previously generated AUD content (use this as baseline; update only what changed):
{prior_document_text or '[No previous document available]'}

---
Produce exactly these sections (use the tag format described above):

1. OVERVIEW        — 2-4 sentences describing the project's purpose and architecture.
2. FILE_INVENTORY  — One line per file: <path> | <type> | <brief purpose>.
3. DATA_FLOWS      — Describe how data moves through the system (inputs → processing → outputs).
4. DEPENDENCIES    — List external libraries, services, or APIs referenced in the code.
5. CONFIGURATION   — Document config keys, env vars, and their expected values/types.
6. KNOWN_ISSUES    — Note any TODOs, deprecated patterns, or potential bugs found.

The output must reflect code changes from this commit while preserving unaffected context from the prior AUD.
"""

    logger.info(
        "Sending %d changed files to Claude (%s), commit=%s",
        len(changed_files),
        model,
        commit_sha or "unknown",
    )

    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text: str = message.content[0].text  # type: ignore[index]
    logger.debug("Claude response (%d chars): %s…", len(raw_text), raw_text[:200])

    sections = _parse_sections(raw_text)
    for key in REQUIRED_SECTIONS:
        sections.setdefault(key, "")
    return sections


def generate_additional_points(
    project_name: str,
    changed_files: list[str],
    associated_document_path: str | None = None,
    commit_sha: str | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
    mock_mode: bool = False,
) -> list[str]:
    """Return concise additional points to append to existing documentation."""
    if not changed_files:
        return []

    if mock_mode or not os.getenv("ANTHROPIC_API_KEY"):
        return [
            f"Review updates in {Path(fp).name} and align related documentation sections."
            for fp in changed_files[:8]
        ]

    import anthropic

    client = anthropic.Anthropic()
    file_block = _build_file_block(changed_files)
    prior_document_text = _read_document_context(associated_document_path)

    system_prompt = (
        "You are a software documentation maintainer. "
        "Given code changes and existing documentation context, provide concise incremental "
        "documentation additions as plain bullets."
    )

    user_prompt = f"""Project: {project_name}
Triggering commit: {commit_sha or 'unknown'}

Changed code:
{file_block}

Existing documentation context:
{prior_document_text or '[No prior documentation available]'}

Return only bullet lines, one point per line, each starting with '- '.
Focus on NEW points to add to documentation; do not rewrite unchanged content.
"""

    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system_prompt}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text: str = message.content[0].text  # type: ignore[index]
    points: list[str] = []
    for line in raw_text.splitlines():
        cleaned = line.strip()
        if cleaned.startswith("- "):
            points.append(cleaned[2:].strip())
        elif cleaned.startswith("*"):
            points.append(cleaned[1:].strip())

    if not points:
        fallback = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        return fallback[:10]
    return points[:10]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_sections(text: str) -> dict[str, str]:
    """Extract section content from Claude's tagged response.

    Parses blocks of the form::

        [SECTION:KEY]
        ... content ...
        [/SECTION:KEY]

    Args:
        text: Raw text returned by the Claude API.

    Returns:
        Dict mapping section key to its trimmed text content.
    """
    pattern = re.compile(
        r"\[SECTION:(?P<key>[A-Z_]+)\]\n(?P<content>.*?)\[/SECTION:(?P=key)\]",
        re.DOTALL,
    )
    results: dict[str, str] = {}
    for match in pattern.finditer(text):
        key = match.group("key")
        content = match.group("content").strip()
        results[key] = content
        logger.debug("Parsed section '%s' (%d chars)", key, len(content))

    if not results:
        logger.warning("No tagged sections found in Claude response — returning raw text as OVERVIEW")
        results["OVERVIEW"] = text.strip()

    return results