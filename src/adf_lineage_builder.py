"""Build end-to-end data lineage diagrams from parsed Azure Data Factory artifacts."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from .parsers.adf_parser import ADFDatasetInfo, ADFLinkedServiceInfo, ADFPipelineInfo

# Linked service types that are infrastructure-only — excluded from lineage nodes
_SKIP_SERVICE_TYPES = {"azurekeyvault", "keyvault"}

# Maps ADF linked service type (lowercase) → display layer key
_SERVICE_TO_LAYER: dict[str, str] = {
    "salesforce":               "source",
    "salesforcev2":             "source",
    "restservice":              "source",
    "sqlserver":                "source",
    "marketo":                  "source",
    "oracledatabase":           "source",
    "saphana":                  "source",
    "sapbw":                    "source",
    "azureblobfs":              "adls",
    "azureblobstorage":         "adls",
    "azuredatalakestoragegen2": "adls",
    "snowflake":                "snowflake",
    "snowflakev2":              "snowflake",
    "azuresynapseanalytics":    "synapse",
    "azuredatawarehouseconn":   "synapse",
    "azuresqldatabase":         "azuresql",
    "azuresqlmi":               "azuresql",
}

_LAYER_ORDER = ["source", "adls", "azuresql", "synapse", "snowflake", "other"]

_LAYER_LABEL = {
    "source":   "External Sources",
    "adls":     "ADLS Raw Layer",
    "azuresql": "Azure SQL",
    "synapse":  "Synapse DW",
    "snowflake":"Snowflake DW",
    "other":    "Other Storage",
}

_LAYER_STYLE = {
    "source":   "fill:#ffe699,stroke:#c6a000,color:#000",
    "adls":     "fill:#b3c6e7,stroke:#4472c4,color:#000",
    "azuresql": "fill:#d9d9d9,stroke:#7f7f7f,color:#000",
    "synapse":  "fill:#d9d9d9,stroke:#7f7f7f,color:#000",
    "snowflake":"fill:#d9ead3,stroke:#38761d,color:#000",
    "other":    "fill:#f2f2f2,stroke:#999999,color:#000",
    "pipeline": "fill:#fce5cd,stroke:#e69138,color:#000",
}


@dataclass
class ADFLineageResult:
    """Output from :func:`detect_and_build`."""
    can_build: bool
    mermaid_code: str
    pipeline_count: int = 0
    source_count: int = 0
    reason: str = ""


def detect_and_build(
    pipelines: list[ADFPipelineInfo],
    datasets: list[ADFDatasetInfo],
    linked_services: list[ADFLinkedServiceInfo],
) -> ADFLineageResult:
    """Detect whether an end-to-end lineage diagram can be built and return it.

    Requirements for a buildable lineage:
    - At least one pipeline with Copy activities that reference input *and* output datasets
    - At least two distinct storage layers reachable through those datasets
      (e.g. a source system + ADLS, or ADLS + Snowflake)

    Args:
        pipelines:        Parsed ADF pipeline definitions.
        datasets:         Parsed ADF dataset definitions.
        linked_services:  Parsed ADF linked service definitions.

    Returns:
        :class:`ADFLineageResult` — ``can_build=True`` and Mermaid code if successful,
        or ``can_build=False`` with a ``reason`` string explaining why not.
    """
    if not pipelines or not datasets:
        return ADFLineageResult(
            can_build=False, mermaid_code="",
            reason="No pipelines or datasets found",
        )

    ds_to_ls: dict[str, str] = {
        d.name: d.linked_service for d in datasets if d.linked_service
    }
    ls_to_type: dict[str, str] = {
        ls.name: ls.service_type.lower() for ls in linked_services if ls.service_type
    }

    # Collect Copy-activity edges: (src_dataset, pipeline_name, sink_dataset)
    copy_edges: list[tuple[str, str, str]] = []
    for pipeline in pipelines:
        for activity in pipeline.activities:
            if activity.activity_type.lower() in ("copy", "copydata"):
                if activity.inputs and activity.outputs:
                    for src_ds in activity.inputs:
                        for sink_ds in activity.outputs:
                            if src_ds and sink_ds:
                                copy_edges.append((src_ds, pipeline.name, sink_ds))

    if not copy_edges:
        return ADFLineageResult(
            can_build=False, mermaid_code="",
            reason="No Copy activities with dataset input/output references found",
        )

    # Check that at least two distinct storage layers are covered
    all_ds = {e[0] for e in copy_edges} | {e[2] for e in copy_edges}
    layers_found: set[str] = set()
    for ds_name in all_ds:
        svc_type = ls_to_type.get(ds_to_ls.get(ds_name, ""), "")
        layer = _get_layer(svc_type)
        if layer:
            layers_found.add(layer)

    if len(layers_found) < 2:
        return ADFLineageResult(
            can_build=False, mermaid_code="",
            reason=(
                f"Only one storage layer identified ({layers_found or 'none'}) — "
                "lineage requires at least a source and a destination layer"
            ),
        )

    mermaid = _build_mermaid(copy_edges, ds_to_ls, ls_to_type)
    pipelines_shown = len({e[1] for e in copy_edges})
    sources_shown = len({
        ls_to_type.get(ds_to_ls.get(e[0], ""), "")
        for e in copy_edges
        if _get_layer(ls_to_type.get(ds_to_ls.get(e[0], ""), "")) == "source"
    })

    return ADFLineageResult(
        can_build=True,
        mermaid_code=mermaid,
        pipeline_count=pipelines_shown,
        source_count=sources_shown,
    )


# ── Diagram builder ───────────────────────────────────────────────────────────

def _build_mermaid(
    copy_edges: list[tuple[str, str, str]],
    ds_to_ls: dict[str, str],
    ls_to_type: dict[str, str],
) -> str:
    """Render ADF data flow as a Mermaid TD flowchart.

    Node types:
    - Linked service nodes grouped into layer subgraphs (sources, ADLS, Snowflake…)
    - Pipeline nodes in a dedicated "ADF Pipelines" subgraph (stadium shape)

    Edges: source_linked_service → pipeline → sink_linked_service
    """
    # Aggregate Copy edges to linked-service level to reduce clutter
    ls_edges: list[tuple[str, str, str]] = []  # (src_ls, pipeline_name, sink_ls)
    for src_ds, pipeline_name, sink_ds in copy_edges:
        src_ls = ds_to_ls.get(src_ds, "")
        sink_ls = ds_to_ls.get(sink_ds, "")
        if src_ls and sink_ls:
            triple = (src_ls, pipeline_name, sink_ls)
            if triple not in ls_edges:
                ls_edges.append(triple)

    if not ls_edges:
        return "```mermaid\nflowchart TD\n    note[No lineage data available]\n```"

    # Determine layer for each linked service node
    all_ls: set[str] = {e[0] for e in ls_edges} | {e[2] for e in ls_edges}
    ls_layer: dict[str, str] = {}
    for ls_name in all_ls:
        svc_type = ls_to_type.get(ls_name, "")
        ls_layer[ls_name] = _get_layer(svc_type) or "other"

    pipeline_names = sorted({e[1] for e in ls_edges})

    # Assign stable node IDs
    node_ids: dict[str, str] = {}
    counter = 0

    def nid(name: str) -> str:
        nonlocal counter
        if name not in node_ids:
            node_ids[name] = f"n{counter}"
            counter += 1
        return node_ids[name]

    # Pre-allocate in layer order for stable, readable IDs
    for layer in _LAYER_ORDER:
        for ls_name in sorted(all_ls):
            if ls_layer.get(ls_name) == layer:
                nid(ls_name)
    for pname in pipeline_names:
        nid(pname)

    lines = ["```mermaid", "%% ADF End-to-End Data Lineage", "flowchart TD"]

    # Linked service subgraphs, one per layer
    layer_nodes: dict[str, list[str]] = defaultdict(list)
    for ls_name in all_ls:
        layer_nodes[ls_layer[ls_name]].append(ls_name)

    for layer in _LAYER_ORDER:
        nodes = sorted(layer_nodes.get(layer, []))
        if not nodes:
            continue
        sg_label = _LAYER_LABEL.get(layer, layer.title())
        lines.append(f'    subgraph SG_{layer.upper()}["{sg_label}"]')
        for ls_name in nodes:
            svc_type = ls_to_type.get(ls_name, "")
            display = _ls_display(ls_name, svc_type)
            lines.append(f'        {nid(ls_name)}["{display}"]')
        lines.append("    end")

    # Pipeline subgraph (stadium shape = rounded rectangle)
    if pipeline_names:
        lines.append('    subgraph SG_PIPELINES["ADF Pipelines"]')
        for pname in pipeline_names:
            display = _pipeline_display(pname)
            lines.append(f'        {nid(pname)}(["{display}"])')
        lines.append("    end")

    # Edges: src_ls --> pipeline --> sink_ls (deduplicated)
    seen: set[tuple[str, str]] = set()
    for src_ls, pname, sink_ls in ls_edges:
        e1 = (nid(src_ls), nid(pname))
        e2 = (nid(pname), nid(sink_ls))
        if e1 not in seen:
            lines.append(f"    {e1[0]} --> {e1[1]}")
            seen.add(e1)
        if e2 not in seen:
            lines.append(f"    {e2[0]} --> {e2[1]}")
            seen.add(e2)

    # Class definitions and assignments
    for style_key, style_val in _LAYER_STYLE.items():
        lines.append(f"    classDef {style_key} {style_val}")
    for ls_name in all_ls:
        lines.append(f"    class {nid(ls_name)} {ls_layer[ls_name]}")
    for pname in pipeline_names:
        lines.append(f"    class {nid(pname)} pipeline")

    lines.append("```")
    return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_layer(service_type: str) -> Optional[str]:
    """Map an ADF service type to a display layer key, or None to skip the node."""
    lower = service_type.lower().strip()
    if not lower or lower in _SKIP_SERVICE_TYPES:
        return None
    return _SERVICE_TO_LAYER.get(lower, "other")


def _ls_display(ls_name: str, service_type: str) -> str:
    """Human-readable two-line label for a linked service node."""
    label = ls_name.removeprefix("ls_").replace("_", " ").title()
    if service_type:
        type_label = (
            service_type
            .replace("v2", "")
            .replace("gen2", " Gen2")
            .title()
            .strip()
        )
        return f"{label}\\n({type_label})"
    return label


def _pipeline_display(pipeline_name: str) -> str:
    """Human-readable label for a pipeline node."""
    return pipeline_name.removeprefix("pl_").replace("_", " ").title()
