"""
tests/test_versioner.py
=======================
Unit tests for src/versioner.py.
"""

import json
from pathlib import Path

import pytest

from src.versioner import (
    bump_version,
    get_current_version,
    load_version_history,
    next_document_path,
    parse_version,
    record_new_version,
    save_version_history,
)


# ---------------------------------------------------------------------------
# parse_version
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_valid_version(self) -> None:
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_zeros(self) -> None:
        assert parse_version("0.0.0") == (0, 0, 0)

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_version("1.2")

        with pytest.raises(ValueError):
            parse_version("not.a.version")


# ---------------------------------------------------------------------------
# bump_version
# ---------------------------------------------------------------------------

class TestBumpVersion:
    def test_patch_bump(self) -> None:
        assert bump_version("1.2.3", "patch") == "1.2.4"

    def test_minor_bump_resets_patch(self) -> None:
        assert bump_version("1.2.3", "minor") == "1.3.0"

    def test_major_bump_resets_minor_and_patch(self) -> None:
        assert bump_version("1.2.3", "major") == "2.0.0"

    def test_bump_from_zero(self) -> None:
        assert bump_version("0.0.0", "minor") == "0.1.0"


# ---------------------------------------------------------------------------
# get_current_version
# ---------------------------------------------------------------------------

class TestGetCurrentVersion:
    def test_empty_history_returns_zero(self) -> None:
        assert get_current_version([]) == "0.0.0"

    def test_returns_last_entry(self) -> None:
        history = [
            {"version": "0.1.0", "generated_at": "", "changed_files": [], "document_path": ""},
            {"version": "0.2.0", "generated_at": "", "changed_files": [], "document_path": ""},
        ]
        assert get_current_version(history) == "0.2.0"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# load / save version_history
# ---------------------------------------------------------------------------

class TestVersionHistoryPersistence:
    def test_roundtrip(self, tmp_path: Path) -> None:
        history = [
            {
                "version": "0.1.0",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "changed_files": ["a.py"],
                "document_path": "output/proj/AUD_v0.1.0.docx",
            }
        ]
        save_version_history(str(tmp_path), history)  # type: ignore[arg-type]
        loaded = load_version_history(str(tmp_path))
        assert loaded == history

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_version_history(str(tmp_path / "nonexistent")) == []


# ---------------------------------------------------------------------------
# next_document_path
# ---------------------------------------------------------------------------

class TestNextDocumentPath:
    def test_path_format(self) -> None:
        from pathlib import Path as _Path
        path = next_document_path("output/my_project", "my_project", "1.3.0")
        p = _Path(path)
        assert p.name == "AUD_v1.3.0.docx"
        assert "my_project" in path


# ---------------------------------------------------------------------------
# record_new_version (integration)
# ---------------------------------------------------------------------------

class TestRecordNewVersion:
    def test_creates_first_version(self, tmp_path: Path) -> None:
        version, doc_path = record_new_version(
            str(tmp_path), "proj", "minor", ["file.py"]
        )
        assert version == "0.1.0"
        assert "AUD_v0.1.0.docx" in doc_path

    def test_increments_from_existing(self, tmp_path: Path) -> None:
        record_new_version(str(tmp_path), "proj", "minor", ["a.py"])
        version, _ = record_new_version(str(tmp_path), "proj", "patch", ["b.py"])
        assert version == "0.1.1"

    def test_history_file_is_written(self, tmp_path: Path) -> None:
        record_new_version(str(tmp_path), "proj", "minor", ["x.py"])
        history_file = tmp_path / "version_history.json"
        assert history_file.exists()
        data = json.loads(history_file.read_text())
        assert len(data) == 1
        assert data[0]["version"] == "0.1.0"
