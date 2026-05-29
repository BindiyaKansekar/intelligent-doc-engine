"""
tests/test_git_tracker.py
=========================
Unit tests for src/git_tracker.py.
"""

from __future__ import annotations

from pathlib import Path

from src.git_tracker import (
    get_ci_trigger_mode,
    get_last_processed_commit_for_repo,
    get_pr_target_branch,
    list_changed_files_in_pr,
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

    def test_repo_specific_roundtrip(self, tmp_path: Path) -> None:
        repo_a = str((tmp_path / "repo-a").resolve())
        repo_b = str((tmp_path / "repo-b").resolve())

        save_commit_state(str(tmp_path), "proj", "sha-a", repo_root=repo_a)
        save_commit_state(str(tmp_path), "proj", "sha-b", repo_root=repo_b)
        state = load_commit_state(str(tmp_path), "proj")

        assert get_last_processed_commit_for_repo(state, repo_a) == "sha-a"
        assert get_last_processed_commit_for_repo(state, repo_b) == "sha-b"


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


class TestPullRequestHelpers:
    def test_ci_trigger_mode_pr(self, monkeypatch) -> None:
        monkeypatch.setenv("BUILD_REASON", "PullRequest")
        assert get_ci_trigger_mode() == "pr"

    def test_ci_trigger_mode_commit(self, monkeypatch) -> None:
        monkeypatch.setenv("BUILD_REASON", "IndividualCI")
        assert get_ci_trigger_mode() == "commit"

    def test_pr_target_branch_normalization(self, monkeypatch) -> None:
        monkeypatch.setenv("SYSTEM_PULLREQUEST_TARGETBRANCH", "refs/heads/main")
        assert get_pr_target_branch() == "main"

    def test_list_changed_files_in_pr_filters_scope(self, monkeypatch, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        src = repo / "src"
        src.mkdir(parents=True)

        wanted = src / "a.py"
        ignored = repo / "docs" / "b.md"
        wanted.write_text("print('x')", encoding="utf-8")
        ignored.parent.mkdir(parents=True, exist_ok=True)
        ignored.write_text("doc", encoding="utf-8")

        calls: list[list[str]] = []

        def _fake_run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["git", "diff", "--name-only"]:
                return _DummyResult("src/a.py\ndocs/b.md\n")
            return _DummyResult("")

        monkeypatch.setattr("src.git_tracker.subprocess.run", _fake_run)

        files = list_changed_files_in_pr(
            target_branch="main",
            source_paths=[str(src)],
            file_types=[".py"],
            repo_dir=str(repo),
        )

        assert files == [str(wanted.resolve())]
        assert any(cmd[:2] == ["git", "fetch"] for cmd in calls)
