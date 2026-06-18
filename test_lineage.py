from src.parsers.sql_parser import parse_file
from src.lineage import build_graph
from src.mermaid_renderer import extract_mermaid_code, render_mermaid_to_png
from pathlib import Path

sql_files = list(Path("c:/Work/snowflake_project").rglob("*.sql"))
print(f"Found {len(sql_files)} SQL files")

infos = [parse_file(str(f)) for f in sql_files]
graph = build_graph(infos)

print(f"Edges: {len(graph.edges)}")

mermaid_block = graph.to_mermaid(title="Snowflake Lineage")
mermaid_code = extract_mermaid_code(mermaid_block)

print("\n--- Mermaid (first 500 chars) ---")
print(mermaid_code[:500])

result = render_mermaid_to_png(mermaid_code, "c:/Work/lineage_test.png")
print(f"\nPNG output: {result}")
