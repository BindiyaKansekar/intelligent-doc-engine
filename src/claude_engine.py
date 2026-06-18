"""
claude_engine.py
================
Anthropic Agent SDK integration for the Intelligent Documentation Engine.

Responsibilities:
  - Define file-exploration tools (read_file, list_directory, search_in_files).
  - Run an agentic loop: Claude calls tools to selectively read changed files,
    then produces documentation sections in tagged format.
  - Parse the response into a dict of documentation sections that map directly
    to the {{PLACEHOLDER}} fields used by template_writer.py.

Agent vs. single-turn:
  Live mode always uses the agent loop (tool use + multi-turn).  Claude decides
  which files to read rather than receiving all content in one prompt — this
  scales better for large change sets and produces more accurate output.

Mock mode:
  Set ``mock_mode=True`` in ``config.json`` (settings.mock_mode) or pass
  ``mock_mode=True`` directly to :func:`generate_documentation_sections`.
  The function then returns realistic placeholder sections built from the
  actual file list — no API key required.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REQUIRED_SECTIONS = [
    "OVERVIEW",
    "FILE_INVENTORY",
    "DATA_FLOWS",
    "DEPENDENCIES",
    "CONFIGURATION",
    "KNOWN_ISSUES",
    "CHANGE_SUMMARY",
    "ARCHITECTURE_DIAGRAM",
    "REQUIREMENT_TRACEABILITY",
]

# Standard extended sections — generated for every project, every run.
# Appended after the template content; not in the Word template placeholders.
EXTENDED_SECTIONS = [
    "IMPLEMENTATION_DETAILS",
    "DATA_MODEL",
    "API_REFERENCE",
]

# Extra sections generated only when --new-integration flag is set.
INTEGRATION_SECTIONS = [
    "INTEGRATION_OVERVIEW",
    "SEQUENCE_DIAGRAM",
    "API_FIELD_MAPPING",
    "ERROR_RETRY_BEHAVIOUR",
]

# ---------------------------------------------------------------------------
# Agent tool definitions
# ---------------------------------------------------------------------------

AGENT_TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Read the contents of a source file. Use this to examine code "
            "before documenting it. Prefer reading only the files you need."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path to the file.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 8000).",
                    "default": 8000,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List files in a directory. Use this to discover what exists "
            "alongside the changed files (e.g. config files, neighbours)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list.",
                },
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Filter to these extensions e.g. [\".py\", \".yaml\"]. "
                        "Omit for all files."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_in_files",
        "description": (
            "Search for a regex pattern across a list of files. "
            "Useful for finding imports, env vars, config keys, TODOs, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for.",
                },
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files to search. Defaults to the changed files "
                        "provided in the task context."
                    ),
                },
                "max_matches_per_file": {
                    "type": "integer",
                    "description": "Cap matches per file (default 20).",
                    "default": 20,
                },
            },
            "required": ["pattern"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _execute_tool(
    tool_name: str,
    tool_input: dict,
    changed_files: list[str],
) -> str:
    """Dispatch a tool call from the agent loop and return the result string."""
    if tool_name == "read_file":
        path = tool_input.get("path", "")
        max_chars = int(tool_input.get("max_chars", 8000))
        return _read_file_content(path, max_chars)

    if tool_name == "list_directory":
        dir_path = Path(tool_input.get("path", "."))
        extensions: list[str] | None = tool_input.get("extensions")
        if not dir_path.exists():
            return f"[Directory not found: {dir_path}]"
        try:
            entries = sorted(dir_path.iterdir())
        except PermissionError as exc:
            return f"[Permission denied: {exc}]"
        if extensions:
            ext_set = {e if e.startswith(".") else f".{e}" for e in extensions}
            entries = [e for e in entries if e.suffix in ext_set]
        lines = [
            f"{'[DIR] ' if e.is_dir() else '      '}{e.name}"
            for e in entries
        ]
        return "\n".join(lines) if lines else "[Empty directory]"

    if tool_name == "search_in_files":
        pattern = tool_input.get("pattern", "")
        file_paths: list[str] = tool_input.get("file_paths") or changed_files
        max_per_file = int(tool_input.get("max_matches_per_file", 20))
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"[Invalid regex: {exc}]"
        results: list[str] = []
        for fp in file_paths:
            p = Path(fp)
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            matches = [
                (i + 1, line)
                for i, line in enumerate(text.splitlines())
                if compiled.search(line)
            ]
            if matches:
                results.append(f"=== {fp} ===")
                results.extend(
                    f"  {lineno}: {line}"
                    for lineno, line in matches[:max_per_file]
                )
        return "\n".join(results) if results else "[No matches found]"

    return f"[Unknown tool: {tool_name}]"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _run_agent_loop(
    client: "anthropic.Anthropic",
    system_prompt: str,
    user_prompt: str,
    changed_files: list[str],
    model: str,
    max_tokens: int,
    max_turns: int = 12,
) -> str:
    """Run an agentic tool-use loop and return the final text response.

    Claude can call read_file / list_directory / search_in_files to explore
    the codebase before producing the documentation sections.
    """
    import anthropic  # noqa: F401 — validated by caller

    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    last_response = None

    for turn in range(max_turns):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=AGENT_TOOLS,
            messages=messages,
        )
        last_response = response
        logger.debug(
            "Agent turn %d/%d — stop_reason=%s, blocks=%d",
            turn + 1,
            max_turns,
            response.stop_reason,
            len(response.content),
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return ""

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict] = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info(
                        "Agent tool call: %s  args=%s",
                        block.name,
                        list(block.input.keys()),
                    )
                    result_text = _execute_tool(block.name, block.input, changed_files)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
            continue

        logger.warning("Unexpected stop_reason=%s at turn %d", response.stop_reason, turn + 1)
        break

    logger.warning("Agent loop exhausted max_turns=%d — returning best available text", max_turns)
    if last_response:
        for block in last_response.content:
            if hasattr(block, "text"):
                return block.text
    return ""


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _read_file_content(file_path: str, max_chars: int = 8_000) -> str:
    path = Path(file_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read %s: %s", file_path, exc)
        return f"[Could not read file: {exc}]"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"
    return text


def _read_document_context(document_path: str | None, max_chars: int = 10_000) -> str:
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
        logger.warning("python-docx is not available — skipping associated document context.")
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
# Mock mode
# ---------------------------------------------------------------------------

def _generate_mock_sections(
    project_name: str,
    changed_files: list[str],
    associated_document_path: str | None = None,
    commit_sha: str | None = None,
) -> dict[str, str]:
    logger.info("[MOCK MODE] Generating placeholder sections for %d file(s)", len(changed_files))

    inventory_lines: list[str] = []
    dep_hints: set[str] = set()
    config_hints: list[str] = []

    for fp in changed_files:
        p = Path(fp)
        size = p.stat().st_size if p.exists() else 0
        ext = p.suffix.lstrip(".").upper() or "FILE"
        inventory_lines.append(
            f"{p.name}  |  {ext}  |  {size:,} bytes  |  [purpose to be filled]"
        )
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
        else "No configuration files detected.\nSet ANTHROPIC_API_KEY in .env to switch to live mode."
    )
    prior_doc_note = (
        f"Prior document context source: {associated_document_path}."
        if associated_document_path
        else "No prior generated document was available."
    )
    commit_note = f"Triggered by commit: {commit_sha}." if commit_sha else "Commit SHA not available."

    file_names = [Path(fp).name for fp in changed_files[:6]]
    mermaid_nodes = " --> ".join(f"{n}({n.split('.')[0]})" for n in file_names[:4]) if file_names else "A(Source) --> B(Process) --> C(Output)"

    integration_mock_participants = " ->> ".join(
        f"{n.split('.')[0]}" for n in file_names[:3]
    ) if file_names else "Client ->> Service ->> ExternalSystem"

    return {
        "OVERVIEW": (
            f"{project_name} — documentation generated in MOCK MODE (no API key).\n"
            f"{len(changed_files)} file(s) were detected as new or modified.\n"
            f"{commit_note}\n"
            f"{prior_doc_note}\n"
            "Replace mock_mode with false in config.json once ANTHROPIC_API_KEY is set."
        ),
        "FILE_INVENTORY": "File | Type | Size | Purpose\n--- | --- | --- | ---\n" + "\n".join(inventory_lines) if inventory_lines else "(no files scanned)",
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
        "CHANGE_SUMMARY": (
            "Component | Change | Reason | Commit\n--- | --- | --- | ---\n"
            + "\n".join(
                f"{Path(fp).name} | New/Modified | See commit history | {commit_sha or 'N/A'}"
                for fp in changed_files[:10]
            )
        ),
        "ARCHITECTURE_DIAGRAM": f"```mermaid\nflowchart LR\n    {mermaid_nodes}\n```",
        "REQUIREMENT_TRACEABILITY": "No linked ADO work items available in mock mode.",
        # Extended sections — always generated (mock placeholders)
        "IMPLEMENTATION_DETAILS": (
            "[MOCK] Implementation details require a live Claude API call.\n"
            "Set mock_mode: false and provide ANTHROPIC_API_KEY to enable."
        ),
        "DATA_MODEL": (
            "Model | Field | Type | Required | Description\n"
            "--- | --- | --- | --- | ---\n"
            "[MOCK] Data model analysis requires a live Claude API call. | - | - | - | -"
        ),
        "API_REFERENCE": (
            "Class | Method | Parameters | Returns | Description\n"
            "--- | --- | --- | --- | ---\n"
            "[MOCK] API reference requires a live Claude API call. | - | - | - | -"
        ),
        # Integration sections — populated only when is_new_integration=True
        "INTEGRATION_OVERVIEW": (
            f"[MOCK] {project_name} — new integration detected.\n"
            f"{len(changed_files)} file(s) changed across the integration.\n"
            "Set mock_mode: false and provide ANTHROPIC_API_KEY for live integration analysis."
        ),
        "SEQUENCE_DIAGRAM": (
            "```mermaid\nsequenceDiagram\n"
            "    participant Client\n"
            "    participant Service\n"
            "    participant ExternalSystem\n"
            "    Client->>+Service: Request\n"
            "    Service->>+ExternalSystem: API Call\n"
            "    ExternalSystem-->>-Service: Response\n"
            "    Service-->>-Client: Result\n"
            "```"
        ),
        "API_FIELD_MAPPING": (
            "Endpoint / Field | Direction | Type | Required | Description\n"
            "--- | --- | --- | --- | ---\n"
            "[MOCK] Populate with live API call — set mock_mode: false to enable. | - | - | - | -"
        ),
        "ERROR_RETRY_BEHAVIOUR": (
            "- [MOCK] Error handling analysis requires a live Claude API call.\n"
            "- Set mock_mode: false and provide ANTHROPIC_API_KEY for live error/retry analysis."
        ),
    }


# ---------------------------------------------------------------------------
# CLI mode (claude -p) — uses Claude.ai subscription, no ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------

# Inline file budget for CLI mode (CLI has no tool-use loop; we stuff context).
_CLI_MAX_CHARS_PER_FILE = 6_000
_CLI_TOTAL_BUDGET = 80_000
_CLI_TIMEOUT_SECONDS = 240


def _build_developer_source_context(source_docs: list[str]) -> str:
    """Read developer-provided spec/design documents and format them as authoritative context.

    These are plain-text or markdown files (e.g. wiki exports, design docs) shared
    by the developer to describe the integration.  They are treated as primary source
    material — Claude should extract structured documentation from them rather than
    trying to infer from code.
    """
    if not source_docs:
        return ""
    parts: list[str] = []
    for fp in source_docs:
        p = Path(fp)
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read developer source doc %s: %s", fp, exc)
            continue
        if len(text) > 20_000:
            text = text[:20_000] + "\n[... truncated at 20000 chars ...]"
        parts.append(f"=== DEVELOPER SOURCE DOCUMENT: {p.name} ===\n{text}\n")
    if not parts:
        return ""
    header = (
        "DEVELOPER-PROVIDED SPECIFICATION DOCUMENTS\n"
        "==========================================\n"
        "The developer has shared the following integration design document(s) as "
        "primary source material. Treat this content as authoritative — extract "
        "structured information directly from it to generate all documentation sections. "
        "Do not invent details not present in this material.\n\n"
    )
    return header + "\n".join(parts)


def _build_inline_file_context(changed_files: list[str]) -> str:
    """Concatenate changed file contents with per-file caps and a total budget."""
    parts: list[str] = []
    used = 0
    for fp in changed_files:
        p = Path(fp)
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > _CLI_MAX_CHARS_PER_FILE:
            text = text[:_CLI_MAX_CHARS_PER_FILE] + f"\n[... truncated at {_CLI_MAX_CHARS_PER_FILE} chars ...]"
        block = f"=== FILE: {fp} ===\n{text}\n"
        if used + len(block) > _CLI_TOTAL_BUDGET:
            parts.append(f"[... remaining files omitted to stay within {_CLI_TOTAL_BUDGET}-char budget ...]")
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts) if parts else "[No file contents available]"


def _run_via_cli(system_prompt: str, user_prompt: str, model: str) -> str:
    """Run a one-shot prompt through the `claude` CLI (-p print mode).

    Uses the user's Claude.ai subscription — no ANTHROPIC_API_KEY required.
    The combined prompt is sent via stdin to avoid command-line length limits.
    """
    cli = shutil.which("claude")
    if not cli:
        raise RuntimeError(
            "claude CLI not found on PATH. Install Claude Code or disable cli_mode."
        )
    combined = f"<system>\n{system_prompt}\n</system>\n\n{user_prompt}"
    logger.info("[CLI MODE] Invoking claude -p --model %s (%d chars)", model, len(combined))
    try:
        result = subprocess.run(
            [cli, "-p", "--model", model],
            input=combined,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude CLI timed out after {_CLI_TIMEOUT_SECONDS}s") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def _format_commit_context(
    commit_messages: list[dict] | None,
    commit_diff_summary: str | None,
) -> str:
    """Format commit messages and diff summary for prompt injection."""
    parts: list[str] = []
    if commit_messages:
        parts.append("Recent commit history (explains WHY changes were made):")
        parts.append("  SHA | Author | Date | Message")
        parts.append("  --- | ------ | ---- | -------")
        for msg in commit_messages[:30]:
            sha = msg.get("sha", "")[:10]
            parts.append(f"  {sha} | {msg.get('author', '')} | {msg.get('date', '')[:10]} | {msg.get('subject', '')}")
    if commit_diff_summary:
        parts.append("\nDiff summary:\n" + commit_diff_summary)
    return "\n".join(parts) if parts else ""


def _format_work_items_context(work_items: list[dict] | None) -> str:
    """Format ADO work items for prompt injection."""
    if not work_items:
        return ""
    parts = ["Linked Azure DevOps work items (use for requirements traceability):"]
    for wi in work_items[:20]:
        parts.append(f"  AB#{wi.get('id', 0)}: \"{wi.get('title', '')}\" [{wi.get('type', '')}] — {wi.get('state', '')}")
        if wi.get("acceptance_criteria"):
            parts.append(f"    Acceptance criteria: {wi['acceptance_criteria'][:500]}")
        elif wi.get("description"):
            parts.append(f"    Description: {wi['description'][:500]}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SECTION_DESCRIPTIONS = {
    "OVERVIEW": (
        "2-4 sentences describing the project's purpose and architecture. "
        "If a developer specification document is present, extract the integration name, "
        "the source and target systems, the business purpose, and the high-level data flow "
        "(e.g. SAP → BizTalk → Azure Blob → ADF → Snowflake) directly from it."
    ),
    "FILE_INVENTORY": (
        "Pipe-delimited table: File | Type | Size | Purpose. One row per file. "
        "For spec documents (.txt, .md), set Type to 'SPEC' and Purpose to a one-line summary "
        "of what the document describes."
    ),
    "DATA_FLOWS": (
        "Describe how data moves through the system (inputs → processing → outputs). "
        "If a developer spec document is present, trace the full pipeline chain described in it — "
        "include source system, intermediary components (e.g. Open Hub, BizTalk, SHIR, ADF), "
        "file format and naming convention, load frequency, and target (e.g. Blob Storage, Snowflake). "
        "Distinguish delta vs full load behaviour where documented."
    ),
    "DEPENDENCIES": (
        "Pipe-delimited table: Component | Type | Purpose. One row per dependency. "
        "For integrations, include all platform components named in the spec: "
        "SAP BW, BizTalk Server, Self-Hosted Integration Runtime, Azure Data Factory, "
        "Azure Blob Storage, Snowflake, etc. — with their role in the pipeline."
    ),
    "CONFIGURATION": (
        "Document all configuration details found in the source. "
        "For integrations, include: Azure subscription names, storage account names, "
        "container and base directory paths for each environment (DEV and PRD), "
        "file naming patterns, ADF linked service names, authentication methods, "
        "and Snowflake pipe and table references. Present as labelled sections per environment."
    ),
    "KNOWN_ISSUES": (
        "Note any TODOs, open items, deprecated patterns, or potential gaps found. "
        "If a developer spec document is present, highlight any sections marked as incomplete "
        "(e.g. blank fields for server name, network location, ADF pipeline name) "
        "and note any explicit change items or action points mentioned."
    ),
    "CHANGE_SUMMARY": (
        "Summarize what changed and why using commit messages and work item context. "
        "Format as pipe-delimited table: Component | Change | Reason | Commit SHA."
    ),
    "ARCHITECTURE_DIAGRAM": (
        "Produce a Mermaid flowchart diagram showing major components and their relationships. "
        "If a developer spec document is present, model the full pipeline described in it — "
        "include every named system (e.g. SAP HANA, BW/4HANA, Open Hub, BizTalk, SHIR, "
        "ADF, Azure Blob, Snowflake) as distinct nodes with labelled edges showing data direction. "
        "Wrap the diagram in ```mermaid and ``` fenced code blocks."
    ),
    "REQUIREMENT_TRACEABILITY": (
        "Map linked ADO work item acceptance criteria to documented features. "
        "Format as pipe-delimited table: Work Item ID | Requirement | Feature | Status. "
        "If no work items are linked, state that no traceability data is available."
    ),
    # Extended sections — generated for every project, appended after main template
    "IMPLEMENTATION_DETAILS": (
        "Describe the implementation structure: key classes and their responsibilities, "
        "core design patterns (async, DI, observer, etc.), and how components interact at runtime. "
        "For integrations, describe each pipeline stage (extract, transfer, load) and the "
        "technology handling it. Use bullet points — one per key component or design decision."
    ),
    "DATA_MODEL": (
        "Document every data model or table in the project. "
        "For code projects: dataclasses, Pydantic models, TypedDicts, SQLAlchemy models, enums. "
        "For integrations: list every replicated table with its full pipeline mapping. "
        "Format as pipe-delimited table. For integration table mappings use columns: "
        "Source Table | Delta Table | Calculation View | Composite Provider | Open Hub Destination. "
        "For code models use columns: Model | Field | Type | Required | Description. "
        "Include a header separator row (--- | --- | ...) after the header."
    ),
    "API_REFERENCE": (
        "Document the complete API or interface surface. "
        "For code projects: public methods as pipe-delimited table: "
        "Class | Method | Parameters | Returns | Description. "
        "For integrations: document the pipeline endpoints and Snowflake objects — "
        "format as: Object | Type | Source | Target | Description. "
        "Cover ADF pipelines, Snowpipes, Snowflake stages, and any external service endpoints. "
        "If multiple environments exist (DEV/PRD), note both."
    ),
    # Integration-specific sections (--new-integration flag)
    "INTEGRATION_OVERVIEW": (
        "Describe the new integration in clear paragraphs covering: "
        "(1) what external system is the data source and what is the target analytics platform, "
        "(2) the business purpose and what the integration enables, "
        "(3) the full technical pipeline chain — source → intermediary hops → destination, "
        "(4) file format, naming convention, delivery frequency and schedule, "
        "(5) delta vs full load strategy, "
        "(6) licensing or compliance rationale if mentioned. "
        "Extract these details directly from any developer spec document present. "
        "Include the owning team and support contacts if stated."
    ),
    "SEQUENCE_DIAGRAM": (
        "Produce a Mermaid sequence diagram showing the end-to-end runtime flow. "
        "If a developer spec document is present, model every hop in the pipeline as a named participant: "
        "e.g. SAP_HANA, BW_OpenHub, BizTalk, SHIR, ADF_Pipeline, AzureBlob, Snowpipe, Snowflake. "
        "Show: data extraction trigger, file push/pull between each hop, "
        "ADF pipeline trigger, Snowpipe auto-ingest, and any cleanup jobs. "
        "Wrap the diagram in ```mermaid and ``` fenced code blocks."
    ),
    "API_FIELD_MAPPING": (
        "Pipe-delimited table documenting every significant data object and field in the integration. "
        "For integrations with source→target table mappings, use columns: "
        "Source Table | Delta Table | Staging Object | Target Table | Notes. "
        "One row per table or field. If the spec lists 40+ tables, include all of them — "
        "do not truncate. Group by domain or schema if logical groupings are present. "
        "If multiple repos are involved, prefix rows with the repo/service name."
    ),
    "ERROR_RETRY_BEHAVIOUR": (
        "Document how the integration handles failures and what operational procedures exist. "
        "Cover: cleanup jobs (e.g. old file removal on source server), "
        "ADF retry and error handling configuration, Snowpipe failure handling, "
        "monitoring approach (ADF monitoring, error notifications), "
        "procedure for failed loads, and any manual intervention steps documented. "
        "Include support escalation path and contacts if present. "
        "Use bullet points, one per distinct scenario or procedure."
    ),
}


def generate_documentation_sections(
    project_name: str,
    changed_files: list[str],
    associated_document_path: str | None = None,
    commit_sha: str | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    mock_mode: bool = False,
    cli_mode: bool = False,
    sections_to_generate: list[str] | None = None,
    max_turns: int = 12,
    commit_messages: list[dict] | None = None,
    commit_diff_summary: str | None = None,
    work_items: list[dict] | None = None,
) -> dict[str, str]:
    """Return populated documentation sections for *changed_files*.

    In **live mode** an agentic loop runs: Claude uses read_file /
    list_directory / search_in_files tools to selectively explore the changed
    files and their context, then emits tagged documentation sections.

    In **mock mode** (``mock_mode=True`` or no ``ANTHROPIC_API_KEY``) the
    function returns placeholder content without any network call.

    Args:
        project_name:         Human-readable project identifier.
        changed_files:        List of file paths added or modified.
        associated_document_path: Path to the previous .docx for baseline context.
        commit_sha:           Git commit SHA for traceability.
        model:                Claude model ID.
        max_tokens:           Max tokens per agent response turn.
        mock_mode:            Skip API call when ``True``.
        sections_to_generate: Subset of REQUIRED_SECTIONS to regenerate.
                              ``None`` generates all (first-run behaviour).
        max_turns:            Maximum agent loop iterations before forcing output.

    Returns:
        Dict mapping section keys to generated text.

    Raises:
        anthropic.APIError: If any API call fails (live mode only).
    """
    requested = sections_to_generate if sections_to_generate is not None else list(REQUIRED_SECTIONS)

    if mock_mode or (not cli_mode and not os.getenv("ANTHROPIC_API_KEY")):
        if not mock_mode:
            logger.warning(
                "ANTHROPIC_API_KEY not set — falling back to mock mode. "
                "Add key to .env and set mock_mode: false in config.json for live output."
            )
        all_sections = _generate_mock_sections(
            project_name=project_name,
            changed_files=changed_files,
            associated_document_path=associated_document_path,
            commit_sha=commit_sha,
        )
        return {k: all_sections.get(k, "") for k in requested}

    # ------------------------------------------------------------------
    # CLI mode — uses `claude -p` (Claude.ai subscription), no API key
    # ------------------------------------------------------------------
    if cli_mode:
        prior_document_text = _read_document_context(associated_document_path)
        section_lines = "\n".join(
            f"{i + 1}. {s:<16} — {_SECTION_DESCRIPTIONS[s]}"
            for i, s in enumerate(requested)
            if s in _SECTION_DESCRIPTIONS
        )
        run_type = (
            "FIRST RUN — generate all sections fresh."
            if sections_to_generate is None
            else f"SUBSEQUENT RUN — regenerate ONLY: {', '.join(requested)}."
        )
        file_context = _build_inline_file_context(changed_files)
        commit_context = _format_commit_context(commit_messages, commit_diff_summary)
        wi_context = _format_work_items_context(work_items)
        system_prompt = (
            "You are a senior software documentation engineer producing concise, accurate "
            "Application Understanding Documents (AUDs). "
            "FORMATTING RULES:\n"
            "- For FILE_INVENTORY, DEPENDENCIES, CHANGE_SUMMARY, and REQUIREMENT_TRACEABILITY: "
            "use pipe-delimited tables with a header row and separator row (e.g. Col1 | Col2\\n--- | ---\\nval | val).\n"
            "- For ARCHITECTURE_DIAGRAM and SEQUENCE_DIAGRAM: produce Mermaid diagrams inside "
            "```mermaid fenced code blocks.\n"
            "- For other sections: use concise plain text, short paragraphs, or bullet lists.\n"
            "- Each section MUST begin with [SECTION:<NAME>] on its own line and end with "
            "[/SECTION:<NAME>] on its own line.\n"
            "- Use commit messages and work item context to explain WHY changes were made, not just what.\n"
            "SOURCE DOCUMENT GUIDANCE:\n"
            "If any of the inlined files are developer-provided specification documents "
            "(e.g. wiki exports or design docs in .txt or .md format), treat them as "
            "authoritative primary source material — extract structured information directly "
            "from them. Do not paraphrase or generalise; use the exact system names, "
            "table names, paths, schedules, and contacts stated in the document. "
            "Blank or placeholder fields in the spec (e.g. 'TBD', empty rows) should be "
            "noted as 'Not yet documented' rather than omitted."
        )
        user_prompt = (
            f"Project: {project_name}\n"
            f"Triggering commit: {commit_sha or 'unknown'}\n"
            f"Run type: {run_type}\n\n"
            f"Changed files inlined below.\n\n"
            f"{file_context}\n\n"
            + (f"{commit_context}\n\n" if commit_context else "")
            + (f"{wi_context}\n\n" if wi_context else "")
            + f"Previously generated AUD content (baseline; update only what changed):\n"
            f"{prior_document_text or '[No previous document available]'}\n\n"
            f"---\nProduce exactly these sections in [SECTION:<NAME>] ... [/SECTION:<NAME>] format:\n\n"
            f"{section_lines}\n"
        )
        raw_text = _run_via_cli(system_prompt, user_prompt, model)
        sections = _parse_sections(raw_text)
        return {k: sections.get(k, "") for k in requested}

    import anthropic

    client = anthropic.Anthropic()
    prior_document_text = _read_document_context(associated_document_path)

    section_lines = "\n".join(
        f"{i + 1}. {s:<16} — {_SECTION_DESCRIPTIONS[s]}"
        for i, s in enumerate(requested)
        if s in _SECTION_DESCRIPTIONS
    )

    run_type = (
        "FIRST RUN — generate all sections fresh."
        if sections_to_generate is None
        else (
            f"SUBSEQUENT RUN — regenerate ONLY these sections: {', '.join(requested)}. "
            "Do NOT produce any other sections; unchanged sections are preserved separately."
        )
    )

    file_list = "\n".join(f"  - {fp}" for fp in changed_files)

    system_prompt = (
        "You are a senior software documentation engineer using the Claude Agent SDK. "
        "You produce concise, accurate Application Understanding Documents (AUDs) "
        "from source code and developer-provided specification documents. "
        "Use your tools (read_file, list_directory, search_in_files) to explore the "
        "changed files and any relevant neighbours before writing. "
        "Read only what you need — do not read every file if unchanged sections are excluded. "
        "Your output must be plain text — no markdown headings, no bullet symbols — "
        "unless they appear naturally in the source. "
        "Each section begins with the exact tag [SECTION:<NAME>] on its own line "
        "and ends with [/SECTION:<NAME>] on its own line. "
        "SOURCE DOCUMENT GUIDANCE: If any changed file is a developer-provided specification "
        "document (.txt or .md), treat it as authoritative primary source material. "
        "Extract system names, table names, paths, schedules, and contacts verbatim — "
        "do not paraphrase. Mark blank or placeholder fields as 'Not yet documented'. "
        "When you have gathered enough information, produce the required sections and stop."
    )

    user_prompt = f"""Project: {project_name}
Triggering commit: {commit_sha or 'unknown'}
Run type: {run_type}

The following files have been added or modified since the last documentation run.
Use your tools to read the files you need, then produce the sections listed below.

Changed files:
{file_list}

Previously generated AUD content (use as baseline; update only what changed):
{prior_document_text or '[No previous document available]'}

---
Produce exactly these sections using the [SECTION:<NAME>] ... [/SECTION:<NAME>] format:

{section_lines}

Only call tools for the files relevant to the sections you are generating.
When you are ready, output all required sections and stop.
"""

    logger.info(
        "Starting agent loop for %d changed file(s) — project=%s, commit=%s, sections=%s",
        len(changed_files),
        project_name,
        commit_sha or "unknown",
        requested,
    )

    raw_text = _run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        changed_files=changed_files,
        model=model,
        max_tokens=max_tokens,
        max_turns=max_turns,
    )

    sections = _parse_sections(raw_text)
    return {k: sections.get(k, "") for k in requested}


def generate_additional_points(
    project_name: str,
    changed_files: list[str],
    associated_document_path: str | None = None,
    commit_sha: str | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
    mock_mode: bool = False,
    cli_mode: bool = False,
    max_turns: int = 6,
) -> list[str]:
    """Return concise additional bullet points to append to existing documentation.

    Uses the agent loop so Claude can read files before drafting points.
    """
    if not changed_files:
        return []

    if mock_mode or (not cli_mode and not os.getenv("ANTHROPIC_API_KEY")):
        return [
            f"Review updates in {Path(fp).name} and align related documentation sections."
            for fp in changed_files[:8]
        ]

    if cli_mode:
        prior_document_text = _read_document_context(associated_document_path)
        file_context = _build_inline_file_context(changed_files)
        system_prompt = (
            "You are a software documentation maintainer. "
            "Return concise incremental documentation additions as plain bullet lines. "
            "Each bullet MUST start with '- '. Focus on NEW points only."
        )
        user_prompt = (
            f"Project: {project_name}\n"
            f"Triggering commit: {commit_sha or 'unknown'}\n\n"
            f"Changed files (inlined):\n\n{file_context}\n\n"
            f"Existing documentation context:\n"
            f"{prior_document_text or '[No prior documentation available]'}\n\n"
            f"Return only bullet lines, one per line, each starting with '- '. Max 10 points."
        )
        raw_text = _run_via_cli(system_prompt, user_prompt, model)
        points: list[str] = []
        for line in raw_text.splitlines():
            cleaned = line.strip()
            if cleaned.startswith(("- ", "* ")):
                points.append(cleaned[2:].strip())
        if not points:
            fallback = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
            return fallback[:10]
        return points[:10]

    import anthropic

    client = anthropic.Anthropic()
    prior_document_text = _read_document_context(associated_document_path)
    file_list = "\n".join(f"  - {fp}" for fp in changed_files)

    system_prompt = (
        "You are a software documentation maintainer using the Claude Agent SDK. "
        "Use your tools to read the changed files as needed, then provide concise "
        "incremental documentation additions as plain bullet lines. "
        "Each bullet must start with '- '. "
        "Focus on NEW points only — do not rewrite unchanged content."
    )

    user_prompt = f"""Project: {project_name}
Triggering commit: {commit_sha or 'unknown'}

Changed files (use read_file to inspect them):
{file_list}

Existing documentation context:
{prior_document_text or '[No prior documentation available]'}

Return only bullet lines, one point per line, each starting with '- '.
Maximum 10 points. Focus on what is new or changed.
"""

    logger.info(
        "Starting agent loop for additional points — project=%s, %d file(s)",
        project_name,
        len(changed_files),
    )

    raw_text = _run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        changed_files=changed_files,
        model=model,
        max_tokens=max_tokens,
        max_turns=max_turns,
    )

    points: list[str] = []
    for line in raw_text.splitlines():
        cleaned = line.strip()
        if cleaned.startswith("- "):
            points.append(cleaned[2:].strip())
        elif cleaned.startswith("* "):
            points.append(cleaned[2:].strip())

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
