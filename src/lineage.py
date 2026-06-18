"""Build SQL lineage graphs and render them as Mermaid flowcharts."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field

from .parsers.sql_parser import SQLFileInfo, TableRef

# Max unique nodes in a single Mermaid diagram — keeps it readable in a Word doc.
_MAX_NODES_PER_DIAGRAM = 12


@dataclass
class LineageGraph:
    """Directed graph: node = table, edge = data flow (source → target)."""
    edges: list[tuple[str, str]] = field(default_factory=list)
    node_layers: dict[str, str] = field(default_factory=dict)
    node_types: dict[str, str] = field(default_factory=dict)
    # edge_columns: per edge, list of [src_col_or_None, tgt_col] pairs
    edge_columns: dict[tuple[str, str], list[list]] = field(default_factory=dict)
    # node_columns: per node, list of {"col": tgt_col, "src": src_col_or_None}
    node_columns: dict[str, list[dict]] = field(default_factory=dict)
    # node_key_types: per node, {col_upper: "PK" | "FK"}
    node_key_types: dict[str, dict[str, str]] = field(default_factory=dict)

    def add_file(self, info: SQLFileInfo) -> None:
        target_str = str(info.primary_target) if info.primary_target else None
        if not target_str:
            return
        tgt_upper = target_str.upper()
        self.node_layers[tgt_upper] = info.layer
        self.node_types[tgt_upper] = info.object_type
        for src in info.sources:
            src_upper = str(src).upper()
            if src_upper not in self.node_layers:
                self.node_layers[src_upper] = _guess_layer(str(src))
            edge = (src_upper, tgt_upper)
            if edge not in self.edges:
                self.edges.append(edge)

        if info.column_lineage:
            src_to_pairs: dict[str, list[list]] = {}
            output_entries: list[dict] = []
            for m in info.column_lineage:
                col = m.target_col
                if not col or col == "*" or col == "(expression)":
                    continue
                src_col = m.source_col if m.source_col else None
                output_entries.append({"col": col, "src": src_col})
                if m.source_table:
                    src_to_pairs.setdefault(m.source_table.upper(), []).append([src_col, col])
            if output_entries:
                self.node_columns[tgt_upper] = output_entries
            for src_upper, pairs in src_to_pairs.items():
                self.edge_columns[(src_upper, tgt_upper)] = pairs

        if info.pk_columns:
            self.node_key_types[tgt_upper] = {
                col.upper(): "PK" for col in info.pk_columns
            }

    # ── Public diagram API ────────────────────────────────────────────────────

    def to_layer_diagrams(self, title_prefix: str = "Lineage") -> list[tuple[str, str]]:
        """Split the lineage into readable per-layer diagrams.

        Each diagram contains at most _MAX_NODES_PER_DIAGRAM nodes so that
        the rendered PNG fits on a Word page without being unreadably small.
        Returns a list of (subtitle, mermaid_code) tuples.
        """
        if not self.edges:
            return [("No Lineage",
                     "```mermaid\nflowchart TD\n    note[No lineage detected]\n```")]

        # Bucket edges by layer-transition type
        buckets: dict[str, list[tuple[str, str]]] = {
            "ingestion":      [],  # raw/unknown → raw
            "transformation": [],  # * → silver
            "aggregation":    [],  # * → gold
        }
        bucket_labels = {
            "ingestion":      f"{title_prefix}: Ingestion (Sources to RAW)",
            "transformation": f"{title_prefix}: Transformation (RAW to SILVER)",
            "aggregation":    f"{title_prefix}: Aggregation (SILVER to GOLD)",
        }
        for src, tgt in self.edges:
            tl = self.node_layers.get(tgt, "unknown")
            if tl == "gold":
                buckets["aggregation"].append((src, tgt))
            elif tl == "silver":
                buckets["transformation"].append((src, tgt))
            else:
                buckets["ingestion"].append((src, tgt))

        diagrams: list[tuple[str, str]] = []
        for bucket_key, edge_list in buckets.items():
            if not edge_list:
                continue
            label = bucket_labels[bucket_key]
            chunks = _split_by_node_count(edge_list, self.node_layers, _MAX_NODES_PER_DIAGRAM)
            total = len(chunks)
            for part_idx, chunk_edges in enumerate(chunks):
                chunk_label = label if total == 1 else f"{label} (part {part_idx+1} of {total})"
                mermaid = _build_mermaid(
                    chunk_edges, self.node_layers, self.node_types, chunk_label
                )
                diagrams.append((chunk_label, mermaid))

        return diagrams

    def to_mermaid(self, title: str = "Data Lineage") -> str:
        """Single compact diagram — capped at _MAX_NODES_PER_DIAGRAM nodes."""
        if not self.edges:
            return "```mermaid\nflowchart TD\n    note[No lineage detected]\n```"
        capped, _ = _greedy_select(self.edges, self.node_layers, _MAX_NODES_PER_DIAGRAM)
        return _build_mermaid(capped, self.node_layers, self.node_types, title)

    def to_summary_table(self) -> str:
        if not self.edges:
            return "_No lineage detected._"
        rows = ["| Source Table | Target Table | Source Layer | Target Layer |",
                "|---|---|---|---|"]
        for src, tgt in sorted(self.edges):
            sl = self.node_layers.get(src, "?")
            tl = self.node_layers.get(tgt, "?")
            rows.append(f"| `{src}` | `{tgt}` | {sl} | {tl} |")
        return "\n".join(rows)


def build_graph(sql_files: list[SQLFileInfo]) -> LineageGraph:
    graph = LineageGraph()
    for info in sql_files:
        graph.add_file(info)
    _infer_fk_types(graph)
    return graph


def _infer_fk_types(graph: LineageGraph) -> None:
    """Assign PK/FK annotations where CREATE TABLE constraints aren't available.

    Strategy (applied in order, earlier wins):
    1. Explicit PKs from CREATE TABLE are already in node_key_types (set in add_file).
    2. Gold DIM tables: _SK columns → PK  (Kimball surrogate-key convention).
    3. Gold FACT tables: _SK columns → FK (surrogate keys reference DIM tables).
    4. Edge propagation: column flowing out of a PK → mark as FK in target node.
    """
    # Step 2 & 3 — naming convention for gold layer
    for node in graph.node_layers:
        if graph.node_layers.get(node) != "gold":
            continue
        name = node.split(".")[-1].upper()
        is_dim  = name.startswith("DIM_") or name.startswith("DIM")
        is_fact = name.startswith("FACT_") or name.startswith("FACT")
        if not (is_dim or is_fact):
            continue
        kt = graph.node_key_types.setdefault(node, {})
        for entry in graph.node_columns.get(node, []):
            col = entry["col"].upper()
            if col in kt:
                continue
            if col.endswith("_SK"):
                kt[col] = "PK" if is_dim else "FK"

    # Step 4 — edge-based propagation from explicit/inferred PKs
    for (src, tgt), pairs in graph.edge_columns.items():
        src_keys = graph.node_key_types.get(src, {})
        if not src_keys:
            continue
        tgt_keys = graph.node_key_types.setdefault(tgt, {})
        for sc, tc in pairs:
            if not sc:
                continue
            if src_keys.get(sc.upper()) == "PK" and tc.upper() not in tgt_keys:
                tgt_keys[tc.upper()] = "FK"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_by_node_count(
    edges: list[tuple[str, str]],
    node_layers: dict[str, str],
    max_nodes: int,
) -> list[list[tuple[str, str]]]:
    """Greedily partition *edges* into chunks where each chunk uses <= *max_nodes* nodes.

    Priority order: cross-layer edges first (most informative), then same-layer.
    """
    # Prioritise cross-layer edges so each chunk shows meaningful data flow
    sorted_edges = sorted(
        edges,
        key=lambda e: (
            0 if node_layers.get(e[0], "x") != node_layers.get(e[1], "y") else 1,
            e[0], e[1],
        ),
    )

    chunks: list[list[tuple[str, str]]] = []
    remaining = list(sorted_edges)

    while remaining:
        chunk_edges, remaining = _greedy_select(remaining, node_layers, max_nodes)
        if chunk_edges:
            chunks.append(chunk_edges)
        else:
            # Safety valve: avoid infinite loop if a single edge needs >max_nodes
            chunks.append([remaining[0]])
            remaining = remaining[1:]

    return chunks


def _greedy_select(
    edges: list[tuple[str, str]],
    node_layers: dict[str, str],
    max_nodes: int,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Select as many edges as possible from *edges* while keeping unique nodes <= *max_nodes*.

    Returns (selected, leftover).
    """
    selected: list[tuple[str, str]] = []
    leftover: list[tuple[str, str]] = []
    nodes_in_chunk: set[str] = set()

    for src, tgt in edges:
        new_nodes = {src, tgt} - nodes_in_chunk
        if len(nodes_in_chunk) + len(new_nodes) <= max_nodes:
            selected.append((src, tgt))
            nodes_in_chunk.update(new_nodes)
        else:
            # If both nodes are already in the chunk, add the edge for free
            if src in nodes_in_chunk and tgt in nodes_in_chunk:
                selected.append((src, tgt))
            else:
                leftover.append((src, tgt))

    return selected, leftover


def _build_mermaid(
    edges: list[tuple[str, str]],
    node_layers: dict[str, str],
    node_types: dict[str, str],
    title: str,
) -> str:
    """Render a list of edges as a Mermaid TD flowchart with layer subgraphs."""
    all_nodes: set[str] = set()
    for src, tgt in edges:
        all_nodes.add(src)
        all_nodes.add(tgt)

    layer_order = ["raw", "unknown", "silver", "gold"]
    layer_labels = {
        "raw":     "RAW",
        "unknown": "Sources",
        "silver":  "SILVER",
        "gold":    "GOLD",
    }

    layer_nodes: dict[str, list[str]] = defaultdict(list)
    for node in sorted(all_nodes):
        layer = node_layers.get(node, "unknown")
        layer_nodes[layer].append(node)

    # Use title as a Mermaid comment — avoids Unicode issues in the diagram header
    safe_title = title.replace("→", "->").replace('"', "'")
    # Intra-layer diagrams (all nodes in one layer) flow better left-to-right
    unique_layers = {node_layers.get(n, "unknown") for n in all_nodes}
    direction = "LR" if len(unique_layers) <= 1 else "TD"
    lines = ["```mermaid", f"%% {safe_title}", f"flowchart {direction}"]

    node_ids: dict[str, str] = {}
    id_counter = 0
    for layer in layer_order:
        nodes = layer_nodes.get(layer, [])
        if not nodes:
            continue
        sg_id = f"SG_{layer.upper()}"
        sg_label = layer_labels.get(layer, layer.upper())
        lines.append(f"    subgraph {sg_id}[\"{sg_label} Layer\"]")
        for node in sorted(nodes):
            nid = f"n{id_counter}"
            id_counter += 1
            node_ids[node] = nid
            display = node.split(".")[-1].replace('"', "'")
            obj_type = node_types.get(node, "TABLE")
            shape_open, shape_close = _mermaid_shape(obj_type)
            lines.append(f'        {nid}{shape_open}"{display}"{shape_close}')
        lines.append("    end")

    for src, tgt in edges:
        sid = node_ids.get(src)
        tid = node_ids.get(tgt)
        if sid and tid:
            lines.append(f"    {sid} --> {tid}")

    lines.append('    classDef raw fill:#b3c6e7,stroke:#4472c4,color:#000')
    lines.append('    classDef silver fill:#d9d9d9,stroke:#7f7f7f,color:#000')
    lines.append('    classDef gold fill:#ffe699,stroke:#c6a000,color:#000')
    for node, nid in node_ids.items():
        layer = node_layers.get(node, "unknown")
        if layer in ("raw", "silver", "gold"):
            lines.append(f"    class {nid} {layer}")

    lines.append("```")
    return "\n".join(lines)


def _guess_layer(table_name: str) -> str:
    lower = table_name.lower()
    if "raw" in lower:
        return "raw"
    if "silver" in lower:
        return "silver"
    if "gold" in lower or "dim_" in lower or "fact_" in lower or "mart" in lower:
        return "gold"
    return "unknown"


def _mermaid_shape(obj_type: str) -> tuple[str, str]:
    shapes = {
        "VIEW":      ("([", "])"),
        "PIPE":      ("[[", "]]"),
        "PROCEDURE": ("{",  "}"),
    }
    return shapes.get(obj_type.upper(), ("[", "]"))
