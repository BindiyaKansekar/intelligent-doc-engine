"""
tests/test_sha_scanner.py
=========================
Unit tests for src/sha_scanner.py.
"""

import json
import tempfile
from pathlib import Path

import pytest

from src.sha_scanner import (
    compute_sha256,
    diff_hashes,
    load_hash_store,
    save_hash_store,
    scan_directory,
)


# ---------------------------------------------------------------------------
# compute_sha256
# ---------------------------------------------------------------------------

class TestComputeSha256:
    def test_consistent_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.py"
        f.write_text("print('hello')", encoding="utf-8")
        assert compute_sha256(f) == compute_sha256(f)

    def test_different_content_gives_different_hash(self, tmp_path: Path) -> None:
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("x = 1", encoding="utf-8")
        b.write_text("x = 2", encoding="utf-8")
        assert compute_sha256(a) != compute_sha256(b)

    def test_returns_64_char_hex_string(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        f.write_bytes(b"data")
        digest = compute_sha256(f)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            compute_sha256(tmp_path / "nonexistent.py")


# ---------------------------------------------------------------------------
# scan_directory
# ---------------------------------------------------------------------------

class TestScanDirectory:
    def test_finds_matching_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("pass", encoding="utf-8")
        (tmp_path / "b.sql").write_text("SELECT 1", encoding="utf-8")
        (tmp_path / "c.txt").write_text("ignore me", encoding="utf-8")

        result = scan_directory([str(tmp_path)], [".py", ".sql"])
        assert len(result) == 2
        assert any("a.py" in k for k in result)
        assert any("b.sql" in k for k in result)
        assert not any("c.txt" in k for k in result)

    def test_recurses_into_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.py").write_text("x = 1", encoding="utf-8")
        result = scan_directory([str(tmp_path)], [".py"])
        assert any("nested.py" in k for k in result)

    def test_missing_source_path_is_skipped(self, tmp_path: Path) -> None:
        result = scan_directory([str(tmp_path / "does_not_exist")], [".py"])
        assert result == {}


# ---------------------------------------------------------------------------
# load_hash_store / save_hash_store
# ---------------------------------------------------------------------------

class TestHashStorePersistence:
    def test_roundtrip(self, tmp_path: Path) -> None:
        data = {"file_a.py": "abc123", "file_b.sql": "def456"}
        save_hash_store(str(tmp_path), "my_project", data)
        loaded = load_hash_store(str(tmp_path), "my_project")
        assert loaded == data

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        result = load_hash_store(str(tmp_path), "ghost_project")
        assert result == {}

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        save_hash_store(str(nested), "proj", {"k": "v"})
        assert (nested / "proj.json").exists()


# ---------------------------------------------------------------------------
# diff_hashes
# ---------------------------------------------------------------------------

class TestDiffHashes:
    def test_new_files_detected(self) -> None:
        current = {"a.py": "hash1"}
        previous: dict = {}
        diff = diff_hashes(current, previous)
        assert diff["new"] == ["a.py"]
        assert diff["modified"] == []
        assert diff["deleted"] == []

    def test_deleted_files_detected(self) -> None:
        current: dict = {}
        previous = {"a.py": "hash1"}
        diff = diff_hashes(current, previous)
        assert diff["deleted"] == ["a.py"]
        assert diff["new"] == []

    def test_modified_files_detected(self) -> None:
        current = {"a.py": "new_hash"}
        previous = {"a.py": "old_hash"}
        diff = diff_hashes(current, previous)
        assert diff["modified"] == ["a.py"]
        assert diff["unchanged"] == []

    def test_unchanged_files(self) -> None:
        same = {"a.py": "hash1"}
        diff = diff_hashes(same, same.copy())
        assert diff["unchanged"] == ["a.py"]
        assert diff["new"] == []
        assert diff["modified"] == []
        assert diff["deleted"] == []

    def test_mixed_diff(self) -> None:
        current = {"new.py": "h1", "modified.py": "h_new", "same.py": "h_same"}
        previous = {"modified.py": "h_old", "same.py": "h_same", "deleted.py": "h2"}
        diff = diff_hashes(current, previous)
        assert "new.py" in diff["new"]
        assert "modified.py" in diff["modified"]
        assert "deleted.py" in diff["deleted"]
        assert "same.py" in diff["unchanged"]
