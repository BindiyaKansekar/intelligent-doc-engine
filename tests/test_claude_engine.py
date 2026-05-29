"""
tests/test_claude_engine.py
============================
Unit tests for src/claude_engine.py.

Live API calls are skipped unless ANTHROPIC_API_KEY is set in the environment.
The parsing logic is tested fully without any network calls.
"""

import os
import pytest

from src.claude_engine import (
    REQUIRED_SECTIONS,
    _parse_sections,
    _read_file_content,
)


# ---------------------------------------------------------------------------
# _read_file_content
# ---------------------------------------------------------------------------

class TestReadFileContent:
    def test_reads_file(self, tmp_path) -> None:
        f = tmp_path / "mod.py"
        f.write_text("x = 42", encoding="utf-8")
        assert _read_file_content(str(f)) == "x = 42"

    def test_truncates_large_file(self, tmp_path) -> None:
        f = tmp_path / "big.py"
        f.write_text("a" * 20_000, encoding="utf-8")
        result = _read_file_content(str(f), max_chars=100)
        assert len(result) < 200
        assert "truncated" in result

    def test_missing_file_returns_error_string(self, tmp_path) -> None:
        result = _read_file_content(str(tmp_path / "ghost.py"))
        assert "Could not read file" in result


# ---------------------------------------------------------------------------
# _parse_sections
# ---------------------------------------------------------------------------

class TestParseSections:
    def _make_response(self, sections: dict[str, str]) -> str:
        lines = []
        for key, content in sections.items():
            lines.append(f"[SECTION:{key}]")
            lines.append(content)
            lines.append(f"[/SECTION:{key}]")
        return "\n".join(lines)

    def test_parses_all_required_sections(self) -> None:
        raw = self._make_response({k: f"Content for {k}" for k in REQUIRED_SECTIONS})
        result = _parse_sections(raw)
        for key in REQUIRED_SECTIONS:
            assert key in result
            assert result[key] == f"Content for {key}"

    def test_content_is_trimmed(self) -> None:
        raw = "[SECTION:OVERVIEW]\n\n  some text  \n\n[/SECTION:OVERVIEW]"
        result = _parse_sections(raw)
        assert result["OVERVIEW"] == "some text"

    def test_no_tags_falls_back_to_overview(self) -> None:
        result = _parse_sections("Claude returned something unstructured.")
        assert "OVERVIEW" in result
        assert "unstructured" in result["OVERVIEW"]

    def test_partial_sections_parsed(self) -> None:
        raw = "[SECTION:DEPENDENCIES]\nrequests, boto3\n[/SECTION:DEPENDENCIES]"
        result = _parse_sections(raw)
        assert "DEPENDENCIES" in result
        assert "OVERVIEW" not in result  # only what's present


# ---------------------------------------------------------------------------
# Live API smoke test (skipped without key)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live API test",
)
def test_generate_documentation_sections_live(tmp_path) -> None:
    """Smoke test: call the real API with a tiny synthetic file."""
    from src.claude_engine import generate_documentation_sections

    f = tmp_path / "hello.py"
    f.write_text('def greet(name: str) -> str:\n    return f"Hello, {name}"\n', encoding="utf-8")

    sections = generate_documentation_sections(
        project_name="smoke_test",
        changed_files=[str(f)],
    )
    for key in REQUIRED_SECTIONS:
        assert key in sections, f"Missing section: {key}"
        assert isinstance(sections[key], str)
