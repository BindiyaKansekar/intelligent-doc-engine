"""
tests/test_git_tracker.py
=========================
Unit tests for src/git_tracker.py.
"""

from __future__ import annotations

from pathlib import Path

from src.git_tracker import (
    list_changed_files_in_commit,
    load_commit_state,
    save_commit_state,
)


class _DummyResult:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


class TestCommitStatePersistence:
    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        state = load_commit_state(str(tmp_path), "proj")
        assert state == {}

    def test_roundtrip(self, tmp_path: Path) -> None:
        save_commit_state(str(tmp_path), "proj", "abc123")
        state = load_commit_state(str(tmp_path), "proj")
        assert state["last_processed_commit"] == "abc123"


class TestListChangedFilesInCommit:
    def test_filters_by_source_path_and_extension(self, monkeypatch, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        src = repo / "src"
        src.mkdir(parents=True)

        wanted = src / "a.py"
        ignored_ext = src / "readme.md"
        outside = repo / "docs" / "b.py"
        ignored_ext.write_text("# doc", encoding="utf-8")
        wanted.write_text("print('x')", encoding="utf-8")
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("print('y')", encoding="utf-8")

        def _fake_run(*args, **kwargs):
            return _DummyResult("src/a.py\nsrc/readme.md\ndocs/b.py\n")

        monkeypatch.setattr("src.git_tracker.subprocess.run", _fake_run)

        files = list_changed_files_in_commit(
            commit_sha="deadbeef",
            source_paths=[str(src)],
            file_types=[".py"],
            repo_dir=str(repo),
        )

        assert files == [str(wanted.resolve())]
