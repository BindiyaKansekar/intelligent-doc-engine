"""
analytics_template_generator.py
================================
Pure-Python documentation generator — no Claude / LLM dependency.

Generates structured Markdown documentation directly from parsed SQL, ADF,
and Azure Function metadata. All output is 100% derived from parsed data:
no AI calls, no API keys, no credentials required.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .parsers.sql_parser import SQLFileInfo
from .parsers.adf_parser import ADFPipelineInfo, ADFDatasetInfo, ADFLinkedServiceInfo
from .parsers.function_parser import FunctionInfo
from .lineage import LineageGraph

_LAYER_ORDER = ["raw", "stage", "silver", "mart", "gold", "unknown"]
_LAYER_LABEL = {
    "raw":     "RAW",
    "stage":   "STAGE",
    "silver":  "SILVER",
    "mart":    "MART",
    "gold":    "GOLD",
    "unknown": "Source",
}


# ── SQL / Snowflake ──────────────────────────────────────────────────────────

def document_sql_pr(
    changed_files: list[SQLFileInfo],
    pr_title: str,
    pr_description: str,
    graph: Optional[LineageGraph] = None,
) -> str:
    sections: list[str] = [f"# PR Documentation: {pr_title}\n"]

    # 1. Overview
    layers_hit = sorted(
        {_LAYER_LABEL.get(f.layer, f.layer.upper()) for f in changed_files},
        key=lambda l: _LAYER_ORDER.index(l.lower()) if l.lower() in _LAYER_ORDER else 99,
    )
    obj_types = sorted({f.object_type for f in changed_files if f.object_type != "UNKNOWN"})
    sections.append("## Overview\n")
    sections.append(
        f"{len(changed_files)} SQL file(s) changed across "
        f"{', '.join(layers_hit) or 'unknown'} layer(s). "
        f"Object types: {', '.join(obj_types) or 'N/A'}."
    )
    if pr_description:
        sections.append(f"\n{pr_description}")

    # 2. Changes by Layer
    sections.append("\n\n## Changes by Layer\n")
    by_layer: dict[str, list[SQLFileInfo]] = defaultdict(list)
    for f in changed_files:
        by_layer[f.layer].append(f)

    for layer_key in _LAYER_ORDER:
        files = by_layer.get(layer_key, [])
        if not files:
            continue
        label = _LAYER_LABEL.get(layer_key, layer_key.upper())
        sections.append(f"\n### {label}\n")
        rows = [
            "| File | Object | Type | Columns | Load Strategy |",
            "|------|--------|------|---------|---------------|",
        ]
        for f in files:
            fname = f.path.replace("\\", "/").split("/")[-1]
            obj = f.primary_target or (f.targets[0] if f.targets else None)
            obj_name = str(obj) if obj else f.object_name or "—"
            ncols = len(f.columns)
            strategy = f.load_strategy or "—"
            rows.append(f"| `{fname}` | `{obj_name}` | {f.object_type} | {ncols} | {strategy} |")
        sections.append("\n".join(rows))

        # Sources per file
        for f in files:
            if f.sources:
                src_list = ", ".join(f"`{s}`" for s in f.sources)
                fname = f.path.replace("\\", "/").split("/")[-1]
                sections.append(f"\n**`{fname}` reads from:** {src_list}")

    # 3. Column-Level Lineage
    if graph and graph.edge_columns:
        sections.append("\n\n## Column-Level Lineage\n")
        total_mappings = sum(len(v) for v in graph.edge_columns.values())
        sections.append(
            f"{total_mappings} column mapping(s) across {len(graph.edge_columns)} table connection(s).\n"
        )
        for (src, tgt), pairs in sorted(graph.edge_columns.items()):
            if not pairs:
                continue
            src_label = src.split(".")[-1]
            tgt_label = tgt.split(".")[-1]
            sections.append(f"\n**`{src_label}` → `{tgt_label}`** ({len(pairs)} columns)\n")
            rows = [
                "| Source Column | | Target Column |",
                "|---------------|---|---------------|",
            ]
            for sc, tc in pairs:
                src_cell = f"`{sc}`" if sc else "_expression_"
                rows.append(f"| {src_cell} | → | `{tc}` |")
            sections.append("\n".join(rows))

    # 4. Mermaid lineage diagram
    if graph and graph.edges:
        diagrams = graph.to_layer_diagrams(title_prefix=f"Lineage — {pr_title}")
        diagram_blocks = "\n\n".join(mermaid for _, mermaid in diagrams)
        sections.append(f"\n\n## Data Lineage\n\n{diagram_blocks}\n")

    # 5. Column inventory
    files_with_cols = [f for f in changed_files if f.columns]
    if files_with_cols:
        sections.append("\n\n## Column Inventory\n")
        for f in files_with_cols:
            fname = f.path.replace("\\", "/").split("/")[-1]
            obj = f.primary_target or (f.targets[0] if f.targets else None)
            obj_name = str(obj) if obj else f.object_name or fname
            pk_set = {c.upper() for c in f.pk_columns}
            col_list = ", ".join(
                f"**`{c}`**" if c.upper() in pk_set else f"`{c}`" for c in f.columns
            )
            sections.append(f"\n**`{obj_name}`** ({len(f.columns)} columns): {col_list}\n")

    sections.append("\n---\n_Generated by Intelligent Doc Engine | Template mode (no AI)_\n")
    return "\n".join(sections)


# ── ADF ──────────────────────────────────────────────────────────────────────

def document_adf_pr(
    pipelines: list[ADFPipelineInfo],
    datasets: list[ADFDatasetInfo],
    linked_services: list[ADFLinkedServiceInfo],
    pr_title: str,
    pr_description: str,
) -> str:
    sections: list[str] = [f"# PR Documentation: {pr_title}\n"]

    sections.append("## Overview\n")
    parts = []
    if pipelines:
        parts.append(f"{len(pipelines)} pipeline(s) changed")
    if datasets:
        parts.append(f"{len(datasets)} dataset(s) changed")
    if linked_services:
        parts.append(f"{len(linked_services)} linked service(s) changed")
    sections.append(", ".join(parts) + "." if parts else "No ADF artefacts detected.")
    if pr_description:
        sections.append(f"\n{pr_description}")

    if pipelines:
        sections.append("\n\n## Pipelines\n")
        for p in pipelines:
            sections.append(f"\n### `{p.name}`\n")
            if p.description:
                sections.append(f"{p.description}\n")
            if p.activities:
                sections.append(f"\n**Activities ({len(p.activities)}):**\n")
                rows = ["| Activity | Type | Depends On |", "|----------|------|------------|"]
                for a in p.activities:
                    deps = ", ".join(f"`{d}`" for d in (a.depends_on or [])) or "—"
                    rows.append(f"| `{a.name}` | {a.activity_type} | {deps} |")
                sections.append("\n".join(rows))
            if p.activity_flow:
                sections.append(f"\n**Activity Flow:**\n\n{p.activity_flow}")

    if datasets:
        sections.append("\n\n## Datasets\n")
        rows = ["| Name | Type | Linked Service |", "|------|------|----------------|"]
        for d in datasets:
            rows.append(f"| `{d.name}` | {d.dataset_type} | `{d.linked_service}` |")
        sections.append("\n".join(rows))

    if linked_services:
        sections.append("\n\n## Linked Services\n")
        rows = ["| Name | Type |", "|------|------|"]
        for ls in linked_services:
            rows.append(f"| `{ls.name}` | {ls.service_type} |")
        sections.append("\n".join(rows))

    sections.append("\n---\n_Generated by Intelligent Doc Engine | Template mode (no AI)_\n")
    return "\n".join(sections)


# ── Azure Functions ──────────────────────────────────────────────────────────

def document_function_pr(
    functions: list[FunctionInfo],
    pr_title: str,
    pr_description: str,
) -> str:
    sections: list[str] = [f"# PR Documentation: {pr_title}\n"]

    sections.append("## Overview\n")
    sections.append(f"{len(functions)} Azure Function(s) changed.")
    if pr_description:
        sections.append(f"\n{pr_description}")

    if functions:
        sections.append("\n\n## Functions\n")
        for f in functions:
            sections.append(f"\n### `{f.function_name}`\n")
            rows = ["| Property | Value |", "|----------|-------|"]
            rows.append(f"| Trigger | {f.trigger_type or '—'} |")
            if f.trigger_schedule:
                rows.append(f"| Schedule | `{f.trigger_schedule}` |")
            if f.route:
                rows.append(f"| Route | `{f.route}` |")
            if f.http_methods:
                rows.append(f"| HTTP Methods | {', '.join(f.http_methods)} |")
            if f.auth_level:
                rows.append(f"| Auth Level | {f.auth_level} |")
            ins  = [b.name for b in f.bindings if b.direction in ("in", "inout")]
            outs = [b.name for b in f.bindings if b.direction in ("out", "inout")]
            if ins:
                rows.append(f"| Input Bindings | {', '.join(ins)} |")
            if outs:
                rows.append(f"| Output Bindings | {', '.join(outs)} |")
            sections.append("\n".join(rows))

    sections.append("\n---\n_Generated by Intelligent Doc Engine | Template mode (no AI)_\n")
    return "\n".join(sections)


# ── Generic fallback ─────────────────────────────────────────────────────────

def document_generic_pr(
    diff_text: str,
    pr_title: str,
    pr_description: str,
) -> str:
    sections = [
        f"# PR Documentation: {pr_title}\n",
        "## Overview\n",
        pr_description or "No description provided.",
        "\n\n## Diff Summary\n",
        f"```\n{diff_text[:3000]}\n```",
        "\n---\n_Generated by Intelligent Doc Engine | Template mode (no AI)_\n",
    ]
    return "\n".join(sections)
