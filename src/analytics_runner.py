"""
analytics_runner.py
===================
CLI entry point for the analytics PR documentation capability.

Scans PRs (GitHub/AzDo) or local directories that contain data engineering
code — Snowflake/SQL, Azure Data Factory, or Azure Functions — and generates
AI-powered Markdown documentation using the Claude API.

For SQL/Snowflake repos, a Mermaid lineage diagram (RAW → SILVER → GOLD)
is generated alongside the text documentation.

Usage:
  python -m src.analytics_runner scan-pr --repo owner/repo --pr 42
  python -m src.analytics_runner scan-pr --provider azdo --org https://dev.azure.com/myorg --project Proj --repo MyRepo --pr 42
  python -m src.analytics_runner scan-dir --path ./snowflake_project
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

# Reconfigure stdout/stderr to UTF-8 on Windows where the default cp1252
# codec can't render Unicode spinner characters from Rich.
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from .pr_scanner import GitHubScanner, AzDoScanner, scan_directory, ChangedFile, PRInfo
from .parsers import repo_detector
from .parsers.sql_parser import parse_sql, SQLFileInfo
from .parsers.adf_parser import parse_file as parse_adf_file, ADFPipelineInfo, ADFDatasetInfo, ADFLinkedServiceInfo
from .parsers.function_parser import parse_file as parse_function_file, FunctionInfo
from . import analytics_template_generator as doc_generator
from .lineage import build_graph
from .analytics_docx_writer import write_docx

console = Console(legacy_windows=False)


@click.group()
@click.version_option("1.0.0", prog_name="intelligent-doc-engine analytics")
def cli() -> None:
    """Analytics PR documentation — Snowflake · ADF · Azure Functions."""


@cli.command("scan-pr")
@click.option("--provider", default="github", type=click.Choice(["github", "azdo"]),
              show_default=True, help="Source control provider")
@click.option("--repo",     default=None, help="GitHub: owner/repo")
@click.option("--org",      default=None, help="AzDO: https://dev.azure.com/<org>")
@click.option("--project",  default=None, help="AzDO: project name")
@click.option("--pr",       required=True, type=int, help="PR / Pull Request number")
@click.option("--type",     "repo_type", default="auto",
              type=click.Choice(["auto", "snowflake", "adf",
                                 "azure_function_python", "azure_function_typescript", "mixed"]),
              show_default=True, help="Force repo type (skip auto-detection)")
@click.option("--output",   default=None, help="Output file path (default: pr-<N>-docs.md)")
@click.option("--model",    default=None, help="Override Claude model (e.g. claude-opus-4-8)")
@click.option("--also-html", default=None, help="Also write an HTML report with interactive lineage to this path")
@click.option("--also-docx", default=None, help="Also write a Word (.docx) document to this path")
def scan_pr(provider, repo, org, project, pr, repo_type, output, model, also_html, also_docx):
    """Scan a PR and generate Markdown documentation."""
    output_path = output or f"pr-{pr}-docs.md"

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        t = prog.add_task("Fetching PR metadata...", total=None)

        if provider == "github":
            if not repo:
                raise click.UsageError("--repo is required for GitHub provider")
            pr_info = GitHubScanner(repo).scan(pr)
        else:
            if not (org and project and repo):
                raise click.UsageError("--org, --project, and --repo are required for AzDO")
            pr_info = AzDoScanner(org, project, repo).scan(pr)

        prog.update(t, description="Detecting repo type...")
        detected = repo_detector.detect([f.path for f in pr_info.changed_files])
        effective_type = repo_type if repo_type != "auto" else detected.primary_type

        prog.update(t, description=f"Parsing {len(pr_info.changed_files)} files ({effective_type})...")
        doc, graph = _generate_doc(pr_info.changed_files, effective_type,
                                   pr_info.title, pr_info.description, prog, t)

        prog.update(t, description="Writing output...")
        _write_output(doc, output_path, pr_info.title, graph=graph)

        if also_html:
            from .analytics_html_writer import write_html
            write_html(doc, also_html, title=pr_info.title, graph=graph)

        if also_docx:
            from .analytics_docx_writer import write_docx
            write_docx(doc, also_docx, title=pr_info.title)

    html_line = f"\nHTML report: [cyan]{also_html}[/cyan]" if also_html else ""
    docx_line = f"\nWord document: [cyan]{also_docx}[/cyan]" if also_docx else ""
    console.print(Panel(
        f"[green]Documentation generated[/green]\n\n"
        f"PR [cyan]#{pr_info.pr_number}[/cyan]: {pr_info.title}\n"
        f"Author: {pr_info.author} | {pr_info.base_branch} ← {pr_info.head_branch}\n"
        f"Type detected: [cyan]{effective_type}[/cyan]  "
        f"(sql={detected.sql_file_count}, adf={detected.adf_pipeline_count}, "
        f"func={detected.function_count})\n"
        f"Files changed: [cyan]{len(pr_info.changed_files)}[/cyan]\n"
        f"Output: [cyan]{output_path}[/cyan]{html_line}{docx_line}",
        title="Intelligent Doc Engine — Analytics",
    ))


@cli.command("scan-dir")
@click.option("--path",   "dir_path", required=True, help="Directory to scan")
@click.option("--type",   "repo_type", default="auto",
              type=click.Choice(["auto", "snowflake", "adf",
                                 "azure_function_python", "azure_function_typescript"]),
              show_default=True)
@click.option("--output", default=None, help="Output file (default: docs.md)")
@click.option("--model",    default=None, help="Override Claude model")
@click.option("--title",    default="Directory Scan", help="Document title")
@click.option("--also-html", default=None, help="Also write an HTML report with interactive lineage to this path")
@click.option("--also-docx", default=None, help="Also write a Word (.docx) document to this path")
def scan_dir(dir_path, repo_type, output, model, title, also_html, also_docx):
    """Scan a local directory and generate documentation."""
    output_path = output or "docs.md"

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        t = prog.add_task("Scanning directory...", total=None)

        changed_files = scan_directory(dir_path)
        detected = repo_detector.detect_from_directory(dir_path)
        effective_type = repo_type if repo_type != "auto" else detected.primary_type

        prog.update(t, description=f"Generating docs ({effective_type}, {len(changed_files)} files)...")
        doc, graph = _generate_doc(changed_files, effective_type, title, "", prog, t)

        prog.update(t, description="Writing output...")
        _write_output(doc, output_path, title, graph=graph)

        if also_html:
            from .analytics_html_writer import write_html
            write_html(doc, also_html, title=title, graph=graph)

        if also_docx:
            from .analytics_docx_writer import write_docx
            write_docx(doc, also_docx, title=title)

    html_line = f"\nHTML report: [cyan]{also_html}[/cyan]" if also_html else ""
    docx_line = f"\nWord document: [cyan]{also_docx}[/cyan]" if also_docx else ""
    console.print(Panel(
        f"[green]Done[/green]\n"
        f"Type detected: [cyan]{effective_type}[/cyan]\n"
        f"Files scanned: [cyan]{len(changed_files)}[/cyan]\n"
        f"Output: [cyan]{output_path}[/cyan]{html_line}{docx_line}",
        title="Intelligent Doc Engine — Analytics",
    ))


# ── Output writer ────────────────────────────────────────────────────────────

def _write_output(markdown: str, output_path: str, title: str, graph=None) -> None:
    """Write *markdown* to *output_path*. Dispatches on file extension."""
    if output_path.lower().endswith(".docx"):
        write_docx(markdown, output_path, title=title)
    elif output_path.lower().endswith(".html"):
        from .analytics_html_writer import write_html
        write_html(markdown, output_path, title=title, graph=graph)
    else:
        Path(output_path).write_text(markdown, encoding="utf-8")


# ── Document generation dispatcher ──────────────────────────────────────────

def _generate_doc(
    changed_files: list[ChangedFile],
    effective_type: str,
    title: str,
    description: str,
    prog=None,
    task=None,
) -> tuple[str, object]:
    """Return (markdown, graph_or_None). Graph is provided for snowflake/mixed-sql types."""
    def update(msg: str):
        if prog and task is not None:
            prog.update(task, description=msg)

    if effective_type == "snowflake":
        update("Parsing SQL files...")
        sql_infos = _parse_sql_files(changed_files)
        update("Building lineage graph...")
        graph = build_graph(sql_infos)
        update("Generating documentation with Claude...")
        return doc_generator.document_sql_pr(sql_infos, title, description, graph), graph

    if effective_type == "adf":
        update("Parsing ADF artefacts...")
        pipelines, datasets, linked_services = _parse_adf_files(changed_files)
        update("Generating documentation with Claude...")
        return doc_generator.document_adf_pr(pipelines, datasets, linked_services, title, description), None

    if effective_type in ("azure_function_python", "azure_function_typescript"):
        update("Parsing Azure Function files...")
        functions = _parse_function_files(changed_files)
        update("Generating documentation with Claude...")
        return doc_generator.document_function_pr(functions, title, description), None

    if effective_type == "mixed":
        parts = []
        mixed_graph = None
        sql_files = [f for f in changed_files if f.path.lower().endswith(".sql")]
        adf_files = [f for f in changed_files
                     if any(x in f.path.lower() for x in ["pipeline/", "dataset/", "linkedservice/"])]
        func_files = [f for f in changed_files
                      if "function.json" in f.path.lower() or f.path.endswith((".py", ".ts"))]

        if sql_files:
            sql_infos = _parse_sql_files(sql_files)
            mixed_graph = build_graph(sql_infos)
            parts.append(doc_generator.document_sql_pr(sql_infos, f"{title} (SQL)", description, mixed_graph))
        if adf_files:
            p, d, ls = _parse_adf_files(adf_files)
            parts.append(doc_generator.document_adf_pr(p, d, ls, f"{title} (ADF)", description))
        if func_files:
            funcs = _parse_function_files(func_files)
            if funcs:
                parts.append(doc_generator.document_function_pr(funcs, f"{title} (Functions)", description))

        return ("\n\n---\n\n".join(parts) if parts else "No supported files found."), mixed_graph

    all_diff = "\n".join(f.diff or f.content[:500] for f in changed_files[:10])
    return doc_generator.document_generic_pr(all_diff, title, description), None


# ── Per-type file parsers ────────────────────────────────────────────────────

def _parse_sql_files(files: list[ChangedFile]) -> list[SQLFileInfo]:
    results = []
    for f in files:
        if not f.path.lower().endswith(".sql"):
            continue
        content = f.content or ""
        if not content and Path(f.path).exists():
            content = Path(f.path).read_text(encoding="utf-8", errors="replace")
        if content:
            results.append(parse_sql(content, path=f.path))
    return results


def _parse_adf_files(files: list[ChangedFile]):
    pipelines: list[ADFPipelineInfo] = []
    datasets: list[ADFDatasetInfo] = []
    linked_services: list[ADFLinkedServiceInfo] = []

    for f in files:
        if not f.path.lower().endswith(".json"):
            continue
        content = f.content
        if content:
            import tempfile, json as _json
            try:
                data = _json.loads(content)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                                 delete=False, encoding="utf-8") as tmp:
                    _json.dump(data, tmp)
                    tmp_path = tmp.name
                result = parse_adf_file(tmp_path)
                os.unlink(tmp_path)
            except Exception:
                result = None
        else:
            result = parse_adf_file(f.path) if Path(f.path).exists() else None

        if isinstance(result, ADFPipelineInfo):
            pipelines.append(result)
        elif isinstance(result, ADFDatasetInfo):
            datasets.append(result)
        elif isinstance(result, ADFLinkedServiceInfo):
            linked_services.append(result)

    return pipelines, datasets, linked_services


def _parse_function_files(files: list[ChangedFile]) -> list[FunctionInfo]:
    functions = []
    for f in files:
        if "function.json" in f.path.lower() or f.path.endswith("function_app.py"):
            if Path(f.path).exists():
                result = parse_function_file(f.path)
                if result:
                    functions.append(result)
    return functions


if __name__ == "__main__":
    cli()
