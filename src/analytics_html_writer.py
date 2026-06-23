"""
analytics_html_writer.py
========================
Converts Markdown documentation + optional LineageGraph into a
self-contained interactive HTML file.

The lineage section is rendered with vis.js:
  - Nodes coloured by layer (RAW / SILVER / GOLD)
  - Hierarchical left-to-right layout
  - Filter box: type a table name to highlight its ancestors and descendants

ADF / Functions Mermaid blocks (activity flows) are rendered in-browser
via mermaid.js so they appear as proper diagrams without any local tooling.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_MERMAID_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
_LINEAGE_SECTION_RE = re.compile(r"\n##\s+Data Lineage\b.*?(?=\n##\s|\n---|\Z)", re.DOTALL)

_LAYER_COLORS = {
    "raw":     {"background": "#b3c6e7", "border": "#4472c4"},
    "silver":  {"background": "#d9d9d9", "border": "#7f7f7f"},
    "gold":    {"background": "#ffe699", "border": "#c6a000"},
    "unknown": {"background": "#f0f0f0", "border": "#aaaaaa"},
}
_LAYER_LEVEL  = {"unknown": 0, "raw": 1, "silver": 2, "gold": 3}
_MATCH_COLOR  = "#e74c3c"   # matched node border
_ANC_COLOR    = "#2980b9"   # ancestor border
_DESC_COLOR   = "#27ae60"   # descendant border
_DIM_BG       = "#eeeeee"
_DIM_BORDER   = "#cccccc"


def write_html(
    markdown: str,
    output_path: str,
    title: str = "Analytics Documentation",
    graph=None,
) -> Path:
    """Convert *markdown* + optional *graph* to a self-contained HTML file."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    clean_md = markdown
    if graph and graph.edges:
        # Strip the Markdown lineage section — replaced by interactive network
        clean_md = _LINEAGE_SECTION_RE.sub("", clean_md)

    body = _md_to_html(clean_md)
    net_html, net_js = _build_network(graph) if (graph and graph.edges) else ("", "")

    page = _render_page(title, body, net_html, net_js)
    out.write_text(page, encoding="utf-8")
    logger.info("HTML written → %s", out)
    return out.resolve()


# ── vis.js network ────────────────────────────────────────────────────────────

def _build_network(graph) -> tuple[str, str]:
    """Return (html_section, inline_js) for the interactive lineage network."""
    all_nodes = sorted({n for edge in graph.edges for n in edge})

    nodes_data = []
    for node in all_nodes:
        layer = graph.node_layers.get(node, "unknown")
        color = _LAYER_COLORS.get(layer, _LAYER_COLORS["unknown"])
        col_entries = graph.node_columns.get(node, [])   # list[{col, src}]
        col_preview = ", ".join(e["col"] for e in col_entries[:5]) + (" …" if len(col_entries) > 5 else "")
        title = f"{node}\nLayer: {layer.upper()}"
        if col_preview:
            title += f"\nColumns: {col_preview}"
        title += "\n\nClick for details"
        nodes_data.append({
            "id":      node,
            "label":   node.split(".")[-1],
            "title":   title,
            "color":   {**color, "highlight": {"background": color["background"], "border": "#333"}},
            "level":   _LAYER_LEVEL.get(layer, 0),
            "font":    {"size": 11, "color": "#333333"},
            "layer":    layer,
            "objType":  graph.node_types.get(node, "TABLE"),
            "columns":  col_entries,
            "keyTypes": graph.node_key_types.get(node, {}),
        })

    edges_data = []
    for i, (src, tgt) in enumerate(graph.edges):
        pairs = graph.edge_columns.get((src, tgt), [])   # list of [src_col, tgt_col]
        tip = ""
        if pairs:
            rows = []
            for sc, tc in pairs[:15]:
                if sc:
                    rows.append(
                        f"<tr><td style='color:#2E74B5;padding:1px 6px 1px 0;white-space:nowrap'>{_html.escape(sc)}</td>"
                        f"<td style='color:#aaa;padding:0 5px'>&#8594;</td>"
                        f"<td style='color:#1a2a40;white-space:nowrap'>{_html.escape(tc)}</td></tr>"
                    )
                else:
                    rows.append(
                        f"<tr><td style='color:#aaa;font-style:italic;padding:1px 6px 1px 0'>expression</td>"
                        f"<td style='color:#aaa;padding:0 5px'>&#8594;</td>"
                        f"<td style='color:#1a2a40;white-space:nowrap'>{_html.escape(tc)}</td></tr>"
                    )
            more = (f"<tr><td colspan='3' style='color:#888;font-size:11px;padding-top:4px'>"
                    f"+{len(pairs)-15} more columns</td></tr>") if len(pairs) > 15 else ""
            tip = (f"<div style='font:12px/1.5 Consolas,monospace;padding:6px 8px'>"
                   f"<div style='font:600 11px/1.8 Segoe UI,sans-serif;color:#555;margin-bottom:3px'>"
                   f"{len(pairs)} column(s):</div>"
                   f"<table style='border-spacing:0'>{''.join(rows)}{more}</table></div>")
        if tip:
            tip += "<div style='font:11px/1.6 Segoe UI,sans-serif;color:#888;margin-top:6px;border-top:1px solid #eee;padding-top:5px'>Click arrow to pin column mappings</div>"
        edges_data.append({"id": i, "from": src, "to": tgt, "arrows": "to",
                            "title": tip, "columns": pairs})

    n_nodes = len(nodes_data)
    n_edges = len(edges_data)

    net_html = f"""\
<section id="lineage-section">
  <h2>Data Lineage</h2>
  <div class="net-toolbar">
    <div class="net-search">
      <input id="node-filter" type="text"
             placeholder="Search &amp; trace a table…"
             autocomplete="off" spellcheck="false">
      <button class="btn-ghost" id="btn-clear" onclick="resetFilter()" title="Clear filter (Esc)">&#x2715;</button>
    </div>
    <button class="btn-action" onclick="fitAll()" title="Fit all nodes into view">&#x26F6; Fit All</button>
    <button class="btn-action" id="btn-fs" onclick="toggleFullscreen()" title="Toggle fullscreen">&#x26F6; Fullscreen</button>
    <span class="net-stats">{n_nodes} nodes &middot; {n_edges} edges</span>
  </div>
  <div class="net-legend">
    <span class="leg-raw">RAW</span>
    <span class="leg-silver">SILVER</span>
    <span class="leg-gold">GOLD</span>
    <span class="leg-unknown">Source</span>
    <span class="leg-sep"></span>
    <span class="leg-hint"><span class="dot dot-match"></span>matched</span>
    <span class="leg-hint"><span class="dot dot-anc"></span>ancestor</span>
    <span class="leg-hint"><span class="dot dot-desc"></span>descendant</span>
  </div>
  <div id="net-wrap">
    <div id="lineage-network"></div>
    <div id="net-loading"><div class="spinner"></div><span>Laying out graph…</span></div>
    <div id="filter-toast"></div>
    <div id="node-panel" class="node-panel hidden">
      <div class="np-hdr">
        <div class="np-title-wrap">
          <span class="np-name" id="np-name"></span>
          <span class="np-full" id="np-full"></span>
        </div>
        <button class="np-close" onclick="closeNodePanel()" title="Close">&#x2715;</button>
      </div>
      <div class="np-badges">
        <span id="np-layer-badge" class="np-badge"></span>
        <span id="np-type-badge" class="np-badge np-badge-type"></span>
      </div>
      <div class="np-section">
        <div class="np-sec-title">&#8593; Incoming <span class="np-cnt" id="np-in-cnt">0</span></div>
        <div id="np-incoming" class="np-rel-wrap"></div>
      </div>
      <div class="np-section">
        <div class="np-sec-title">&#8595; Outgoing <span class="np-cnt" id="np-out-cnt">0</span></div>
        <div id="np-outgoing" class="np-rel-wrap"></div>
      </div>
      <div class="np-footer">
        <span id="np-anc-total">0</span> ancestors
        &nbsp;&middot;&nbsp;
        <span id="np-desc-total">0</span> descendants
      </div>
    </div>
    <div id="edge-panel" class="edge-panel hidden">
      <div class="np-hdr">
        <div class="np-title-wrap">
          <span class="np-name" id="ep-title">Column Mappings</span>
          <span class="np-full" id="ep-subtitle"></span>
        </div>
        <button class="np-close" onclick="closeEdgePanel()" title="Close">&#x2715;</button>
      </div>
      <div class="ep-stats" id="ep-stats"></div>
      <div id="ep-columns" class="ep-col-wrap"></div>
    </div>
  </div>
</section>"""

    net_js = f"""\
const _ND   = {json.dumps(nodes_data)};
const _ED   = {json.dumps(edges_data)};
const _ORIG = {{}};
const _COLS = {{}};
const _KEYS = {{}};
_ND.forEach(n => {{
    _ORIG[n.id] = n.color;
    if (n.columns  && n.columns.length)  _COLS[n.id] = n.columns;
    if (n.keyTypes && Object.keys(n.keyTypes).length) _KEYS[n.id] = n.keyTypes;
}});

let _nodes, _edges, _net, _isFullscreen = false;

// ── Init ──────────────────────────────────────────────────────────────────────
function _init() {{
    const el = document.getElementById("lineage-network");
    _nodes = new vis.DataSet(_ND.map(n => ({{...n}})));
    _edges = new vis.DataSet(_ED.map(e => ({{...e}})));

    _net = new vis.Network(el, {{ nodes: _nodes, edges: _edges }}, {{
        layout: {{
            hierarchical: {{
                enabled: true,
                direction: "LR",
                sortMethod: "directed",
                levelSeparation: 220,
                nodeSpacing: 95,
                treeSpacing: 160,
                blockShifting: true,
                edgeMinimization: true,
                parentCentralization: true,
            }},
        }},
        physics: {{
            enabled: true,
            solver: "hierarchicalRepulsion",
            hierarchicalRepulsion: {{
                nodeDistance: 120,
                avoidOverlap: 1,
                centralGravity: 0.0,
                springLength: 100,
                springConstant: 0.01,
                damping: 0.09,
            }},
            stabilization: {{
                enabled: true,
                iterations: 500,
                updateInterval: 25,
                fit: true,
            }},
        }},
        edges: {{
            smooth: {{ type: "cubicBezier", forceDirection: "horizontal", roundness: 0.4 }},
            color: {{ color: "#9aabbd", opacity: 0.9 }},
            width: 1.5,
            arrows: {{ to: {{ enabled: true, scaleFactor: 0.6 }} }},
            hoverWidth: 2.5,
            selectionWidth: 2.5,
        }},
        nodes: {{
            shape: "box",
            margin: {{ top: 7, bottom: 7, left: 10, right: 10 }},
            borderWidth: 1,
            borderWidthSelected: 2.5,
            shadow: {{ enabled: true, color: "rgba(0,0,0,0.10)", size: 5, x: 2, y: 2 }},
            chosen: false,
        }},
        interaction: {{
            hover: true,
            tooltipDelay: 200,
            navigationButtons: true,
            keyboard: {{ enabled: true, speed: {{ x: 12, y: 12, zoom: 0.025 }} }},
            multiselect: false,
            selectConnectedEdges: false,
        }},
        configure: {{ enabled: false }},
    }});

    // Lock layout after stabilisation then fit everything into view
    _net.once("stabilizationIterationsDone", () => {{
        _net.setOptions({{ physics: {{ enabled: false }} }});
        _net.fit({{ animation: {{ duration: 700, easingFunction: "easeInOutQuad" }} }});
        document.getElementById("net-loading").style.display = "none";
    }});

    // Click a node → trace lineage + show summary panel
    // Click an edge → show column mapping panel
    _net.on("click", p => {{
        if (p.nodes.length) {{
            closeEdgePanel();
            const nd = _ND.find(n => n.id === p.nodes[0]);
            if (nd) {{
                document.getElementById("node-filter").value = nd.label;
                traceNode(nd.label);
                showNodePanel(p.nodes[0]);
            }}
        }} else if (p.edges.length) {{
            closeNodePanel();
            showEdgePanel(p.edges[0]);
        }} else {{
            closeNodePanel();
            closeEdgePanel();
        }}
    }});

    // Double-click on empty space → reset everything
    _net.on("doubleClick", p => {{
        if (!p.nodes.length && !p.edges.length) resetFilter();
    }});

    // Show pointer cursor when hovering a node
    _net.on("hoverNode",  () => {{ document.getElementById("lineage-network").style.cursor = "pointer"; }});
    _net.on("blurNode",   () => {{ document.getElementById("lineage-network").style.cursor = "default"; }});
}}

// ── BFS helpers ───────────────────────────────────────────────────────────────
function _adj() {{
    const inn = {{}}, out = {{}};
    _ED.forEach(e => {{
        (inn[e.to]   = inn[e.to]   || []).push(e.from);
        (out[e.from] = out[e.from] || []).push(e.to);
    }});
    return {{ inn, out }};
}}

function _bfs(starts, adj) {{
    const seen = new Set(), q = [...starts];
    while (q.length) {{
        const n = q.shift();
        (adj[n] || []).forEach(nb => {{ if (!seen.has(nb)) {{ seen.add(nb); q.push(nb); }} }});
    }}
    return seen;
}}

// ── Trace ─────────────────────────────────────────────────────────────────────
function traceNode(query) {{
    const q = query.trim().toLowerCase();
    if (!q) {{ resetFilter(); return; }}

    const matched = new Set(
        _ND.filter(n => n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q))
           .map(n => n.id)
    );
    if (!matched.size) {{ showToast("No matching tables found"); return; }}

    const {{ inn, out }} = _adj();
    const ancestors   = _bfs([...matched], inn);
    const descendants = _bfs([...matched], out);
    matched.forEach(id => {{ ancestors.delete(id); descendants.delete(id); }});
    const rel = new Set([...matched, ...ancestors, ...descendants]);

    _nodes.update(_ND.map(n => {{
        const bg = _ORIG[n.id].background;
        if (matched.has(n.id))
            return {{ id: n.id, color: {{ background: bg, border: "{_MATCH_COLOR}", highlight: {{background: bg, border: "{_MATCH_COLOR}"}} }}, borderWidth: 3 }};
        if (ancestors.has(n.id))
            return {{ id: n.id, color: {{ background: bg, border: "{_ANC_COLOR}",   highlight: {{background: bg, border: "{_ANC_COLOR}"  }} }}, borderWidth: 2 }};
        if (descendants.has(n.id))
            return {{ id: n.id, color: {{ background: bg, border: "{_DESC_COLOR}",  highlight: {{background: bg, border: "{_DESC_COLOR}" }} }}, borderWidth: 2 }};
        return {{ id: n.id, color: {{ background: "{_DIM_BG}", border: "{_DIM_BORDER}", highlight: {{background: "{_DIM_BG}", border: "{_DIM_BORDER}"}} }}, borderWidth: 1 }};
    }}));

    _edges.update(_ED.map(e => ({{
        id: e.id,
        color: rel.has(e.from) && rel.has(e.to)
            ? {{ color: "#444", opacity: 1.0 }}
            : {{ color: "#ddd", opacity: 0.1 }},
        width: rel.has(e.from) && rel.has(e.to) ? 2.5 : 1,
    }})));

    // Zoom into the relevant subgraph
    _net.fit({{
        nodes: [...rel],
        animation: {{ duration: 550, easingFunction: "easeInOutQuad" }},
    }});

    showToast(`${{matched.size}} match · ${{ancestors.size}} ancestor(s) · ${{descendants.size}} descendant(s)`);
}}

// ── Edge column-mapping panel ─────────────────────────────────────────────────
function showEdgePanel(edgeId) {{
    const edge = _ED.find(e => e.id === edgeId);
    if (!edge) return;

    const srcNd = _ND.find(n => n.id === edge.from);
    const tgtNd = _ND.find(n => n.id === edge.to);
    const srcLabel = srcNd ? srcNd.label : edge.from.split(".").pop();
    const tgtLabel = tgtNd ? tgtNd.label : edge.to.split(".").pop();

    document.getElementById("ep-title").textContent = srcLabel + " → " + tgtLabel;
    const sub = document.getElementById("ep-subtitle");
    if (edge.from !== srcLabel || edge.to !== tgtLabel) {{
        sub.textContent = edge.from + " → " + edge.to;
        sub.style.display = "block";
    }} else {{
        sub.style.display = "none";
    }}

    const pairs = edge.columns || [];
    document.getElementById("ep-stats").textContent =
        pairs.length ? pairs.length + " column mapping" + (pairs.length !== 1 ? "s" : "") : "No column mappings";

    let html = "";
    if (pairs.length) {{
        html = '<table class="ep-col-table">';
        pairs.forEach(function(p) {{
            const sc = p[0], tc = p[1];
            const srcCell = sc
                ? '<td class="ep-src">' + sc + '</td>'
                : '<td class="ep-src ep-expr">expression</td>';
            html += "<tr>" + srcCell + '<td class="ep-arr">→</td><td class="ep-tgt">' + tc + "</td></tr>";
        }});
        html += "</table>";
    }} else {{
        html = '<div style="color:#aaa;font-style:italic;font-size:.8rem;padding:4px 0">No column-level mapping available.</div>';
    }}
    document.getElementById("ep-columns").innerHTML = html;

    // Highlight clicked edge, dim others
    _edges.update(_ED.map(e => ({{
        id: e.id,
        color: e.id === edgeId ? {{ color: "#2E74B5", opacity: 1.0 }} : {{ color: "#9aabbd", opacity: 0.25 }},
        width: e.id === edgeId ? 3 : 1.5,
    }})));

    document.getElementById("edge-panel").classList.remove("hidden");
}}

function closeEdgePanel() {{
    document.getElementById("edge-panel").classList.add("hidden");
    _edges.update(_ED.map(e => ({{ id: e.id, color: {{ color: "#9aabbd", opacity: 0.9 }}, width: 1.5 }})));
}}

// ── Reset ─────────────────────────────────────────────────────────────────────
function resetFilter() {{
    document.getElementById("node-filter").value = "";
    _nodes.update(_ND.map(n => ({{ id: n.id, color: _ORIG[n.id], borderWidth: 1 }})));
    _edges.update(_ED.map(e => ({{ id: e.id, color: {{ color: "#9aabbd", opacity: 0.9 }}, width: 1.5 }})));
    closeEdgePanel();
    fitAll();
    hideToast();
}}

function fitAll() {{
    _net.fit({{ animation: {{ duration: 500, easingFunction: "easeInOutQuad" }} }});
}}

// ── Fullscreen ────────────────────────────────────────────────────────────────
function toggleFullscreen() {{
    const sec = document.getElementById("lineage-section");
    _isFullscreen = !_isFullscreen;
    sec.classList.toggle("fullscreen", _isFullscreen);
    document.getElementById("btn-fs").textContent = _isFullscreen ? "✕ Exit" : "⛶ Fullscreen";
    document.body.style.overflow = _isFullscreen ? "hidden" : "";
    setTimeout(() => _net.fit({{ animation: {{ duration: 400 }} }}), 60);
}}

// ── Node summary panel ────────────────────────────────────────────────────────
const _LAYER_CSS = {{
    raw:     "background:#b3c6e7;border-color:#4472c4;color:#1a3a6e",
    silver:  "background:#d9d9d9;border-color:#7f7f7f;color:#333",
    gold:    "background:#ffe699;border-color:#c6a000;color:#5a4000",
    unknown: "background:#f0f0f0;border-color:#aaa;color:#555",
}};

// ── Relationship table renderer ───────────────────────────────────────────────
function _keyBadge(col, keyTypes) {{
    const k = (keyTypes || {{}})[col ? col.toUpperCase() : ""] || "";
    if (!k) return "";
    return `<span class="key-badge key-${{k.toLowerCase()}}">${{k}}</span>`;
}}

function _peerBtn(peerId) {{
    const peerNd = _ND.find(n => n.id === peerId);
    const lbl = peerNd ? peerNd.label : peerId.split(".").pop();
    return `<button class="rel-peer-btn" title="${{peerId}}"
        onclick="document.getElementById('node-filter').value='${{lbl}}';traceNode('${{lbl}}');showNodePanel('${{peerId}}')"
        >${{lbl}}</button>`;
}}

function _renderRelGroup(edges, peerProp, thisKeyTypes) {{
    if (!edges.length) return '<div class="np-empty">None</div>';
    const isIncoming = peerProp === "from";
    let html = "";
    for (const edge of edges) {{
        const peerId    = edge[peerProp];
        const peerNd    = _ND.find(n => n.id === peerId);
        const peerLabel = peerNd ? peerNd.label : peerId.split(".").pop();
        const pairs     = edge.columns || [];
        html += `<div class="rel-block">${{_peerBtn(peerId)}}`;
        if (pairs.length) {{
            html += `<table class="rel-table">`;
            const cap = Math.min(pairs.length, 10);
            for (let i = 0; i < cap; i++) {{
                const [sc, tc]  = pairs[i];
                const keyCol    = isIncoming ? tc : sc;
                const badge     = _keyBadge(keyCol, thisKeyTypes);
                // For incoming: show "SrcTable.srcCol → tgtCol"
                // For outgoing: show "srcCol → TgtTable.tgtCol"
                let srcCell, tgtCell;
                if (isIncoming) {{
                    srcCell = sc
                        ? `<td class="rel-src"><span class="rel-tbl">${{peerLabel}}.</span>${{sc}}</td>`
                        : `<td class="rel-src rel-expr"><span class="rel-tbl">${{peerLabel}}</span> expr</td>`;
                    tgtCell = `<td class="rel-tgt">${{tc}}</td>`;
                }} else {{
                    srcCell = sc
                        ? `<td class="rel-src">${{sc}}</td>`
                        : `<td class="rel-src rel-expr">expression</td>`;
                    tgtCell = `<td class="rel-tgt"><span class="rel-tbl">${{peerLabel}}.</span>${{tc}}</td>`;
                }}
                html += `<tr>${{srcCell}}<td class="rel-arr">&#8594;</td>${{tgtCell}}<td class="rel-key">${{badge}}</td></tr>`;
            }}
            if (pairs.length > 10)
                html += `<tr><td colspan="4" class="rel-more">+${{pairs.length - 10}} more</td></tr>`;
            html += `</table>`;
        }} else {{
            html += `<div class="rel-no-cols">No column data</div>`;
        }}
        html += `</div>`;
    }}
    return html;
}}

function showNodePanel(nodeId) {{
    const nd = _ND.find(n => n.id === nodeId);
    if (!nd) return;

    const {{ inn, out }} = _adj();
    const ancestors   = _bfs([nodeId], inn);
    const descendants = _bfs([nodeId], out);
    const thisKeys    = _KEYS[nodeId] || {{}};

    // Header
    document.getElementById("np-name").textContent = nd.label;
    const fullEl = document.getElementById("np-full");
    fullEl.textContent = nodeId !== nd.label ? nodeId : "";
    fullEl.style.display = nodeId !== nd.label ? "block" : "none";

    // Layer + type badges
    const layer = nd.layer || "unknown";
    const lb = document.getElementById("np-layer-badge");
    lb.textContent = layer.toUpperCase();
    lb.style.cssText = (_LAYER_CSS[layer] || _LAYER_CSS.unknown);
    document.getElementById("np-type-badge").textContent = nd.objType || "TABLE";

    // Incoming edges (this node is the target)
    const inEdges = _ED.filter(e => e.to === nodeId);
    document.getElementById("np-in-cnt").textContent = inEdges.length;
    document.getElementById("np-incoming").innerHTML = _renderRelGroup(inEdges, "from", thisKeys);

    // Outgoing edges (this node is the source)
    const outEdges = _ED.filter(e => e.from === nodeId);
    document.getElementById("np-out-cnt").textContent = outEdges.length;
    document.getElementById("np-outgoing").innerHTML = _renderRelGroup(outEdges, "to", thisKeys);

    // Footer totals
    document.getElementById("np-anc-total").textContent  = ancestors.size;
    document.getElementById("np-desc-total").textContent = descendants.size;

    document.getElementById("node-panel").classList.remove("hidden");
}}

function closeNodePanel() {{
    document.getElementById("node-panel").classList.add("hidden");
}}

// ── Toast ─────────────────────────────────────────────────────────────────────
let _toastTimer;
function showToast(msg) {{
    const t = document.getElementById("filter-toast");
    t.textContent = msg;
    t.classList.add("visible");
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(hideToast, 4000);
}}
function hideToast() {{
    document.getElementById("filter-toast").classList.remove("visible");
}}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {{
    _init();
    let _t;
    document.getElementById("node-filter").addEventListener("input", e => {{
        clearTimeout(_t);
        _t = setTimeout(() => traceNode(e.target.value), 280);
    }});
    document.getElementById("node-filter").addEventListener("keydown", e => {{
        if (e.key === "Escape") resetFilter();
    }});
}});
"""
    return net_html, net_js


# ── HTML page template ────────────────────────────────────────────────────────

def _render_page(title: str, body: str, net_html: str, net_js: str) -> str:
    esc = _html.escape(title)
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vis_tag    = '<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>' if net_js else ""
    mer_tag    = '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>\n<script>mermaid.initialize({{startOnLoad:true,theme:"default"}});</script>'
    net_script = f"<script>\n{net_js}\n</script>" if net_js else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc}</title>
{vis_tag}
{mer_tag}
<style>
/* ── Reset & base ─────────────────────────────────────────────────────────── */
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font:15px/1.65 "Segoe UI",system-ui,sans-serif;color:#222;background:#f7f8fc}}
a{{color:#1F497D}}

/* ── Header ──────────────────────────────────────────────────────────────── */
header{{background:linear-gradient(135deg,#1F497D 0%,#2E74B5 100%);color:#fff;padding:22px 40px 18px}}
header h1{{font-size:1.6rem;font-weight:600;margin-bottom:4px}}
.meta{{font-size:.82rem;opacity:.72}}

/* ── Main content ────────────────────────────────────────────────────────── */
main{{max-width:1140px;margin:0 auto;padding:28px 40px 70px}}
h1,h2,h3{{margin-top:1.5em;margin-bottom:.4em}}
h1{{font-size:1.35rem;color:#1F497D;border-bottom:2px solid #1F497D;padding-bottom:5px}}
h2{{font-size:1.18rem;color:#2E74B5;border-bottom:1px solid #c8d8ef;padding-bottom:4px}}
h3{{font-size:1.03rem;color:#333}}
p{{margin:.45em 0 .8em}}
ul,ol{{margin:.35em 0 .8em 1.5em}}
li{{margin:.12em 0}}
code{{font:13px "Cascadia Code","Consolas",monospace;background:#eef2ff;padding:1px 5px;border-radius:3px}}
pre{{background:#f4f4f4;border:1px solid #ddd;border-radius:5px;padding:14px;overflow-x:auto;margin:.7em 0}}
pre code{{background:none;padding:0}}
hr{{border:none;border-top:1px solid #ddd;margin:1.4em 0}}
strong{{font-weight:600}}
em{{font-style:italic}}
table{{border-collapse:collapse;width:100%;margin:.7em 0 1.1em;font-size:.9rem}}
th{{background:#1F497D;color:#fff;padding:7px 12px;text-align:left;font-weight:600}}
td{{padding:6px 12px;border-bottom:1px solid #e2e2e2}}
tr:nth-child(even) td{{background:#f5f7ff}}
.mermaid{{margin:1em 0;text-align:center}}

/* ── Lineage section ─────────────────────────────────────────────────────── */
#lineage-section{{margin-top:2.4em;transition:all .25s}}

/* toolbar */
.net-toolbar{{
  display:flex;align-items:center;gap:8px;
  margin:12px 0 8px;flex-wrap:wrap
}}
.net-search{{
  display:flex;align-items:center;flex:1;min-width:240px;
  border:1px solid #bbb;border-radius:7px;background:#fff;
  overflow:hidden;
}}
.net-search:focus-within{{border-color:#2E74B5;box-shadow:0 0 0 2px #c5d9f1}}
#node-filter{{
  flex:1;padding:8px 10px;border:none;outline:none;
  font-size:.95rem;background:transparent
}}
#btn-clear{{
  border:none;background:none;cursor:pointer;padding:6px 10px;
  font-size:1rem;color:#aaa;line-height:1
}}
#btn-clear:hover{{color:#555}}
.btn-action{{
  padding:7px 14px;border:1px solid #bbb;background:#fff;
  border-radius:7px;cursor:pointer;font-size:.88rem;
  white-space:nowrap;transition:background .15s
}}
.btn-action:hover{{background:#f0f4ff;border-color:#2E74B5;color:#2E74B5}}
.net-stats{{font-size:.82rem;color:#888;margin-left:auto;white-space:nowrap}}

/* legend */
.net-legend{{
  display:flex;gap:10px;font-size:.8rem;
  margin-bottom:8px;flex-wrap:wrap;align-items:center
}}
.net-legend span{{padding:3px 9px;border-radius:10px;font-weight:500}}
.leg-raw    {{background:#b3c6e7;border:1px solid #4472c4}}
.leg-silver {{background:#d9d9d9;border:1px solid #7f7f7f}}
.leg-gold   {{background:#ffe699;border:1px solid #c6a000}}
.leg-unknown{{background:#f0f0f0;border:1px solid #aaa}}
.leg-sep    {{flex:1}}
.leg-hint   {{background:none!important;border:none!important;color:#666;padding:0!important;display:flex;align-items:center;gap:4px}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%}}
.dot-match{{background:#e74c3c}}
.dot-anc  {{background:#2980b9}}
.dot-desc {{background:#27ae60}}

/* network container */
#net-wrap{{position:relative}}
#lineage-network{{
  width:100%;height:68vh;min-height:480px;max-height:780px;
  border:1px solid #d0d7e3;border-radius:8px;background:#fff;
  box-shadow:0 2px 8px rgba(0,0,0,.06)
}}

/* loading overlay */
#net-loading{{
  position:absolute;inset:0;z-index:10;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;
  background:rgba(255,255,255,.88);border-radius:8px;
  font-size:.95rem;color:#555;pointer-events:none
}}
.spinner{{
  width:34px;height:34px;border:3px solid #dce5f0;
  border-top-color:#2E74B5;border-radius:50%;
  animation:spin .7s linear infinite
}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}

/* toast */
#filter-toast{{
  position:absolute;bottom:14px;left:50%;transform:translateX(-50%);
  background:rgba(30,40,55,.82);color:#fff;
  padding:7px 18px;border-radius:20px;font-size:.84rem;
  white-space:nowrap;pointer-events:none;
  opacity:0;transition:opacity .25s
}}
#filter-toast.visible{{opacity:1}}

/* fullscreen */
#lineage-section.fullscreen{{
  position:fixed;inset:0;z-index:9999;
  background:#f7f8fc;padding:16px 20px;overflow-y:auto
}}
#lineage-section.fullscreen #lineage-network{{
  height:calc(100vh - 155px);max-height:none
}}

/* ── Node summary panel ──────────────────────────── */
.node-panel{{
  position:absolute;top:12px;right:12px;
  width:320px;
  background:#fff;border:1px solid #d0d7e3;border-radius:10px;
  box-shadow:0 6px 20px rgba(0,0,0,.13);
  z-index:20;font-size:.855rem;overflow:hidden;
  transition:opacity .18s,transform .18s
}}
.node-panel.hidden{{opacity:0;pointer-events:none;transform:translateY(-6px)}}
.np-hdr{{
  display:flex;align-items:flex-start;justify-content:space-between;
  padding:11px 12px 6px;border-bottom:1px solid #eef0f4;
  background:#f8f9fc
}}
.np-title-wrap{{flex:1;min-width:0}}
.np-name{{
  display:block;font-weight:700;font-size:.95rem;color:#1a2a40;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis
}}
.np-full{{
  display:block;font-size:.73rem;color:#888;margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis
}}
.np-close{{
  border:none;background:none;cursor:pointer;color:#aaa;
  font-size:1rem;padding:0 0 0 8px;line-height:1;flex-shrink:0
}}
.np-close:hover{{color:#555}}
.np-badges{{
  display:flex;gap:6px;padding:7px 12px 6px;flex-wrap:wrap
}}
.np-badge{{
  padding:2px 8px;border-radius:8px;border:1px solid transparent;
  font-size:.73rem;font-weight:600;line-height:1.5
}}
.np-badge-type{{
  background:#eef2ff;border-color:#c5d0f0;color:#3a4fa0
}}
.np-section{{padding:7px 12px 4px;border-top:1px solid #f0f2f6}}
.np-sec-title{{
  font-size:.75rem;font-weight:600;color:#888;text-transform:uppercase;
  letter-spacing:.04em;margin-bottom:4px;display:flex;align-items:center;gap:6px
}}
.np-cnt{{
  background:#e8edf5;color:#3a5278;border-radius:8px;
  padding:1px 6px;font-size:.72rem;font-weight:700
}}
.np-empty{{color:#aaa;font-style:italic;font-size:.8rem;padding:3px 6px}}
/* relationship blocks */
.np-rel-wrap{{max-height:200px;overflow-y:auto;padding:2px 0}}
.rel-block{{margin-bottom:6px}}
.rel-block:last-child{{margin-bottom:0}}
.rel-peer-btn{{
  background:none;border:none;cursor:pointer;padding:0;
  font-size:.78rem;font-weight:600;color:#2E74B5;
  display:inline-block;margin-bottom:3px
}}
.rel-peer-btn:hover{{text-decoration:underline}}
.rel-table{{border-spacing:0;width:100%;font-size:.71rem;font-family:"Cascadia Code","Consolas",monospace}}
.rel-table td{{padding:1px 3px;vertical-align:middle;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100px}}
.rel-src{{color:#2E74B5}}
.rel-tbl{{color:#9ab8d8;font-size:.68rem}}
.rel-expr{{color:#aaa;font-style:italic;font-family:"Segoe UI",sans-serif}}
.rel-arr{{color:#ccc;padding:0 3px;font-family:sans-serif}}
.rel-tgt{{color:#1a2a40}}
.rel-key{{width:28px}}
.rel-more{{color:#999;font-size:.68rem;font-family:"Segoe UI",sans-serif;padding-top:2px}}
.rel-no-cols{{color:#bbb;font-size:.72rem;font-style:italic;padding:1px 0}}
/* key type badges */
.key-badge{{
  display:inline-block;font-size:.62rem;font-weight:700;
  padding:1px 4px;border-radius:3px;letter-spacing:.02em;
  font-family:"Segoe UI",sans-serif
}}
.key-pk{{background:#fff3cd;border:1px solid #ffc107;color:#7a5200}}
.key-fk{{background:#cce5ff;border:1px solid #74b3f5;color:#004085}}
.np-footer{{
  padding:7px 12px 9px;border-top:1px solid #f0f2f6;
  font-size:.78rem;color:#777;text-align:center;background:#f8f9fc
}}

/* ── Edge column-mapping panel ───────────────────── */
.edge-panel{{
  position:absolute;top:12px;right:12px;
  width:320px;
  background:#fff;border:1px solid #d0d7e3;border-radius:10px;
  border-top:3px solid #2E74B5;
  box-shadow:0 6px 20px rgba(0,0,0,.13);
  z-index:20;font-size:.855rem;overflow:hidden;
  transition:opacity .18s,transform .18s
}}
.edge-panel.hidden{{opacity:0;pointer-events:none;transform:translateY(-6px)}}
.ep-stats{{
  padding:5px 12px;font-size:.76rem;color:#2E74B5;font-weight:600;
  background:#f0f5ff;border-bottom:1px solid #dce8f8
}}
.ep-col-wrap{{max-height:300px;overflow-y:auto;padding:8px 12px}}
.ep-col-table{{border-spacing:0;width:100%;font-size:.78rem;font-family:"Cascadia Code","Consolas",monospace}}
.ep-col-table td{{padding:2px 4px;vertical-align:middle;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}}
.ep-src{{color:#2E74B5}}
.ep-arr{{color:#bbb;padding:0 5px;font-family:sans-serif;font-size:.9em}}
.ep-tgt{{color:#1a2a40}}
.ep-expr{{color:#aaa;font-style:italic;font-family:"Segoe UI",sans-serif}}
</style>
</head>
<body>
<header>
  <h1>{esc}</h1>
  <p class="meta">Generated by Intelligent Doc Engine &nbsp;&middot;&nbsp; {ts}</p>
</header>
<main>
{body}
{net_html}
</main>
{net_script}
</body>
</html>"""


# ── Markdown → HTML ───────────────────────────────────────────────────────────

def _inline(text: str) -> str:
    text = _html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", text)
    return text


def _table_to_html(lines: list[str]) -> str:
    sep = re.compile(r"^\s*\|?[\s:|-]+\|?[\s:|-]*\|?\s*$")
    data = [ln for ln in lines if not sep.match(ln)]
    if not data:
        return ""

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    rows = [cells(ln) for ln in data]
    html_rows = ""
    for ri, row in enumerate(rows):
        tag = "th" if ri == 0 else "td"
        html_rows += "<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in row) + "</tr>\n"
    return f"<table>\n{html_rows}</table>"


def _md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Mermaid block → rendered in-browser by mermaid.js
        if line.startswith("```mermaid"):
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i]); i += 1
            i += 1
            out.append('<div class="mermaid">' + "\n".join(code) + "</div>")
            continue

        # Generic code block
        if line.startswith("```"):
            lang = _html.escape(line[3:].strip())
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(_html.escape(lines[i])); i += 1
            i += 1
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>" + "\n".join(code) + "</code></pre>")
            continue

        # Headings
        if line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:].strip())}</h3>"); i += 1; continue
        if line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:].strip())}</h2>"); i += 1; continue
        if line.startswith("# ") and not line.startswith("## "):
            out.append(f"<h1>{_inline(line[2:].strip())}</h1>"); i += 1; continue

        # Tables
        if line.strip().startswith("|"):
            tbl: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i]); i += 1
            out.append(_table_to_html(tbl)); continue

        # Bullet list
        if line.startswith("- ") or line.startswith("* "):
            items: list[str] = []
            while i < len(lines) and (lines[i].startswith("- ") or lines[i].startswith("* ")):
                items.append(f"<li>{_inline(lines[i][2:].strip())}</li>"); i += 1
            out.append("<ul>" + "".join(items) + "</ul>"); continue

        # Numbered list
        if re.match(r"^\d+\.\s", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i]):
                stripped = re.sub(r"^\d+\.\s", "", lines[i]).strip()
                items.append(f"<li>{_inline(stripped)}</li>"); i += 1
            out.append("<ol>" + "".join(items) + "</ol>"); continue

        # HR
        if line.strip() in ("---", "***", "___"):
            out.append("<hr>"); i += 1; continue

        # Blank line
        if not line.strip():
            i += 1; continue

        # Paragraph
        out.append(f"<p>{_inline(line.strip())}</p>")
        i += 1

    return "\n".join(out)
