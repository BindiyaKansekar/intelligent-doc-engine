"""Detect the technology type of a repository from its file tree."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

RepoType = Literal["snowflake", "adf", "azure_function_python",
                   "azure_function_typescript", "mixed", "unknown"]


@dataclass
class RepoProfile:
    primary_type: RepoType
    types_found: list[RepoType] = field(default_factory=list)
    sql_file_count: int = 0
    adf_pipeline_count: int = 0
    function_count: int = 0


def detect(paths: list[str]) -> RepoProfile:
    """Detect repo type from a list of changed or scanned file paths."""
    sql_files = 0
    adf_pipeline_files = 0
    func_python_files = 0
    func_ts_files = 0

    for p in paths:
        lower = p.lower().replace("\\", "/")
        if lower.endswith(".sql"):
            sql_files += 1
        if "pipeline/" in lower and lower.endswith(".json"):
            adf_pipeline_files += 1
        if "linkedservice/" in lower and lower.endswith(".json"):
            adf_pipeline_files += 1
        if "dataset/" in lower and lower.endswith(".json"):
            adf_pipeline_files += 1
        if lower.endswith("function.json") or lower.endswith("host.json"):
            if _has_python_sibling(p):
                func_python_files += 1
            else:
                func_ts_files += 1
        if lower.endswith("__init__.py") and "function" in lower:
            func_python_files += 1
        if lower.endswith("index.ts") and _is_under_function_dir(p):
            func_ts_files += 1
        if lower.endswith("function_app.py"):
            func_python_files += 1

    types: list[RepoType] = []
    if sql_files:
        types.append("snowflake")
    if adf_pipeline_files:
        types.append("adf")
    if func_python_files:
        types.append("azure_function_python")
    if func_ts_files:
        types.append("azure_function_typescript")

    if not types:
        primary = "unknown"
    elif len(types) == 1:
        primary = types[0]
    else:
        scores = {
            "snowflake": sql_files,
            "adf": adf_pipeline_files,
            "azure_function_python": func_python_files,
            "azure_function_typescript": func_ts_files,
        }
        dominant = max(types, key=lambda t: scores.get(t, 0))
        primary = "mixed" if len(types) > 1 else dominant

    return RepoProfile(
        primary_type=primary,
        types_found=types,
        sql_file_count=sql_files,
        adf_pipeline_count=adf_pipeline_files,
        function_count=func_python_files + func_ts_files,
    )


def detect_from_directory(root: str) -> RepoProfile:
    paths = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            paths.append(os.path.join(dirpath, f))
    return detect(paths)


def _has_python_sibling(path: str) -> bool:
    parent = Path(path).parent
    return any(parent.glob("*.py")) or any(parent.glob("**/*.py"))


def _is_under_function_dir(path: str) -> bool:
    parts = Path(path).parts
    return any("function" in p.lower() for p in parts)
