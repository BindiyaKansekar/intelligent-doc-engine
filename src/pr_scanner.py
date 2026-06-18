"""Fetch PR metadata and changed-file content from GitHub or Azure DevOps."""
from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ChangedFile:
    path: str
    status: str          # added / modified / deleted / renamed
    diff: str
    content: str         # full file content (empty if deleted)


@dataclass
class PRInfo:
    pr_number: int
    title: str
    description: str
    author: str
    base_branch: str
    head_branch: str
    changed_files: list[ChangedFile] = field(default_factory=list)
    repo_slug: str = ""


class GitHubScanner:
    def __init__(self, repo: str):
        """repo: 'owner/repo'"""
        self.repo = repo

    def scan(self, pr_number: int) -> PRInfo:
        meta = self._run_json(
            ["gh", "pr", "view", str(pr_number), "--repo", self.repo, "--json",
             "title,body,author,baseRefName,headRefName"]
        )
        changed = self._run_json(
            ["gh", "pr", "view", str(pr_number), "--repo", self.repo, "--json", "files"]
        ).get("files", [])

        diff_text = self._run_text(
            ["gh", "pr", "diff", str(pr_number), "--repo", self.repo]
        )
        file_diffs = _split_diff_by_file(diff_text)

        files = []
        for f in changed:
            fpath = f.get("path", "")
            status = f.get("status", "modified")
            content = ""
            if status != "deleted":
                content = self._fetch_file_content(fpath, meta.get("headRefName", ""))
            files.append(ChangedFile(
                path=fpath,
                status=status,
                diff=file_diffs.get(fpath, ""),
                content=content,
            ))

        return PRInfo(
            pr_number=pr_number,
            title=meta.get("title", ""),
            description=meta.get("body", ""),
            author=meta.get("author", {}).get("login", ""),
            base_branch=meta.get("baseRefName", "main"),
            head_branch=meta.get("headRefName", ""),
            changed_files=files,
            repo_slug=self.repo,
        )

    def _fetch_file_content(self, path: str, ref: str) -> str:
        try:
            return self._run_text(
                ["gh", "api", f"repos/{self.repo}/contents/{path}",
                 "-f", f"ref={ref}", "--jq", ".content"],
                decode_base64=True,
            )
        except Exception:
            return ""

    def _run_json(self, cmd: list[str]) -> dict:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def _run_text(self, cmd: list[str], decode_base64: bool = False) -> str:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if decode_base64:
            import base64
            return base64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
        return result.stdout


class AzDoScanner:
    def __init__(self, org: str, project: str, repo: str):
        self.org = org
        self.project = project
        self.repo = repo

    def scan(self, pr_number: int) -> PRInfo:
        meta = self._run_json(
            ["az", "repos", "pr", "show",
             "--id", str(pr_number),
             "--org", self.org,
             "--output", "json"]
        )
        changed = self._run_json(
            ["az", "repos", "pr", "list-file-changes",
             "--id", str(pr_number),
             "--org", self.org,
             "--output", "json"]
        )

        files = []
        for item in (changed if isinstance(changed, list) else []):
            fpath = item.get("item", {}).get("path", "").lstrip("/")
            change_type = item.get("changeType", "edit").lower()
            status = _azdo_status(change_type)
            content = ""
            if status != "deleted":
                content = self._fetch_file_content(
                    fpath, meta.get("lastMergeSourceCommit", {}).get("commitId", "")
                )
            files.append(ChangedFile(path=fpath, status=status, diff="", content=content))

        source_ref = meta.get("sourceRefName", "").replace("refs/heads/", "")
        target_ref = meta.get("targetRefName", "").replace("refs/heads/", "")

        return PRInfo(
            pr_number=pr_number,
            title=meta.get("title", ""),
            description=meta.get("description", ""),
            author=meta.get("createdBy", {}).get("displayName", ""),
            base_branch=target_ref,
            head_branch=source_ref,
            changed_files=files,
            repo_slug=f"{self.org}/{self.project}/{self.repo}",
        )

    def _fetch_file_content(self, path: str, version: str) -> str:
        try:
            result = self._run_json(
                ["az", "repos", "show-ref",
                 "--name", path,
                 "--version", version,
                 "--org", self.org,
                 "--project", self.project,
                 "--repository", self.repo,
                 "--output", "json"]
            )
            return result.get("content", "")
        except Exception:
            return ""

    def _run_json(self, cmd: list[str]) -> dict | list:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)


def scan_directory(directory: str, extensions: Optional[list[str]] = None) -> list[ChangedFile]:
    exts = {e.lower() for e in (extensions or [".sql", ".json", ".py", ".ts"])}
    files = []
    for p in Path(directory).rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
            files.append(ChangedFile(
                path=str(p),
                status="existing",
                diff="",
                content=content,
            ))
    return files


def _split_diff_by_file(diff: str) -> dict[str, str]:
    result: dict[str, str] = {}
    current_file = None
    current_lines: list[str] = []

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git"):
            if current_file:
                result[current_file] = "".join(current_lines)
            parts = line.strip().split(" b/")
            current_file = parts[-1] if len(parts) > 1 else None
            current_lines = [line]
        elif current_file:
            current_lines.append(line)

    if current_file:
        result[current_file] = "".join(current_lines)
    return result


def _azdo_status(change_type: str) -> str:
    mapping = {"add": "added", "edit": "modified", "delete": "deleted", "rename": "renamed"}
    return mapping.get(change_type, "modified")
