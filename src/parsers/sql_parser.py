"""Parse Snowflake SQL files and extract table definitions, transformations, and lineage."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import sqlglot
    from sqlglot import exp
    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False


@dataclass
class TableRef:
    schema: Optional[str]
    name: str

    def __str__(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def __hash__(self):
        return hash(str(self).upper())

    def __eq__(self, other):
        return str(self).upper() == str(other).upper()


@dataclass
class ColumnMapping:
    target_col: str
    source_table: Optional[str] = None  # None = unresolvable (multi-source or expression)
    source_col: Optional[str] = None    # None = computed/derived expression


@dataclass
class SQLFileInfo:
    path: str
    layer: str           # raw / silver / gold / unknown
    object_type: str     # TABLE / VIEW / PIPE / PROCEDURE / UNKNOWN
    object_name: str
    targets: list[TableRef] = field(default_factory=list)
    sources: list[TableRef] = field(default_factory=list)
    ctes: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    column_lineage: list[ColumnMapping] = field(default_factory=list)
    pk_columns: list[str] = field(default_factory=list)
    load_strategy: str = ""   # MERGE / INSERT / COPY / FULL_REFRESH / UNKNOWN
    description: str = ""
    raw_sql: str = ""

    @property
    def primary_target(self) -> Optional[TableRef]:
        return self.targets[0] if self.targets else None


def parse_file(path: str) -> SQLFileInfo:
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_sql(content, path=path)


def parse_sql(sql: str, path: str = "") -> SQLFileInfo:
    layer = _infer_layer(path, sql)
    info = SQLFileInfo(path=path, layer=layer, object_type="UNKNOWN",
                       object_name="", raw_sql=sql)

    if HAS_SQLGLOT:
        _parse_with_sqlglot(sql, info)
    else:
        _parse_with_regex(sql, info)

    upper = sql.upper()
    if "MERGE INTO" in upper:
        info.load_strategy = "MERGE (SCD/UPSERT)"
    elif "COPY INTO" in upper:
        info.load_strategy = "COPY (BULK LOAD)"
    elif "CREATE PIPE" in upper:
        info.load_strategy = "SNOWPIPE (AUTO-INGEST)"
    elif "TRUNCATE TABLE" in upper and "INSERT INTO" in upper:
        info.load_strategy = "TRUNCATE + INSERT (FULL REFRESH)"
    elif "INSERT INTO" in upper:
        info.load_strategy = "INCREMENTAL INSERT"
    elif "CREATE OR REPLACE" in upper:
        info.load_strategy = "FULL REFRESH (CREATE OR REPLACE)"

    cte_names_upper = {c.upper() for c in info.ctes}
    info.sources = [s for s in info.sources
                    if str(s).upper() not in cte_names_upper
                    and str(s).upper() != str(info.primary_target or "").upper()]

    if HAS_SQLGLOT and not info.column_lineage:
        _populate_column_lineage(sql, info)

    return info


def _parse_with_sqlglot(sql: str, info: SQLFileInfo) -> None:
    try:
        statements = sqlglot.parse(sql, dialect="snowflake", error_level=sqlglot.ErrorLevel.WARN)
    except Exception:
        _parse_with_regex(sql, info)
        return

    for stmt in (s for s in statements if s is not None):
        with_clause = stmt.find(exp.With)
        if with_clause:
            for cte in with_clause.find_all(exp.CTE):
                info.ctes.append(cte.alias)

        if isinstance(stmt, exp.Create):
            tbl = stmt.find(exp.Table)
            if tbl:
                info.object_type = "VIEW" if stmt.args.get("kind", "").upper() == "VIEW" else "TABLE"
                info.object_name = tbl.name
                info.targets.append(_table_ref(tbl))
                # Parse primary key columns from CREATE TABLE
                schema = stmt.find(exp.Schema)
                if schema:
                    for expr in schema.expressions:
                        if isinstance(expr, exp.ColumnDef):
                            for c in expr.constraints:
                                if isinstance(c.kind, exp.PrimaryKeyColumnConstraint):
                                    info.pk_columns.append(expr.name)
                        elif isinstance(expr, exp.PrimaryKey):
                            for col in expr.find_all(exp.Column):
                                info.pk_columns.append(col.name)
        elif isinstance(stmt, exp.Insert):
            tbl = stmt.find(exp.Table)
            if tbl:
                info.targets.append(_table_ref(tbl))
                info.object_name = tbl.name
        elif isinstance(stmt, exp.Merge):
            tbl = stmt.args.get("this")
            if tbl and isinstance(tbl, exp.Table):
                info.targets.append(_table_ref(tbl))
                info.object_name = tbl.name
                info.object_type = "TABLE"

        if "CREATE PIPE" in stmt.sql(dialect="snowflake").upper()[:50]:
            info.object_type = "PIPE"

        cte_names = {c.upper() for c in info.ctes}
        target_names = {str(t).upper() for t in info.targets}

        for tbl in stmt.find_all(exp.Table):
            ref = _table_ref(tbl)
            ref_str = str(ref).upper()
            if (ref_str not in cte_names
                    and ref_str not in target_names
                    and ref_str not in {str(t).upper() for t in info.sources}):
                info.sources.append(ref)

        if not info.columns:
            sel = stmt.find(exp.Select)
            if sel:
                for col in sel.expressions[:20]:
                    alias = col.alias or (col.name if hasattr(col, "name") else "")
                    if alias:
                        info.columns.append(alias)

    if not info.object_type or info.object_type == "UNKNOWN":
        _infer_object_type_regex(sql, info)


def _parse_with_regex(sql: str, info: SQLFileInfo) -> None:
    upper = sql.upper()

    for pat in [
        r"CREATE(?:\s+OR\s+REPLACE)?\s+(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([.\w]+)",
        r"MERGE\s+INTO\s+([.\w]+)",
        r"INSERT\s+INTO\s+([.\w]+)",
        r"COPY\s+INTO\s+([.\w]+)",
    ]:
        for m in re.finditer(pat, upper):
            raw = m.group(1)
            info.targets.append(_ref_from_str(raw))
            if not info.object_name:
                info.object_name = raw.split(".")[-1]

    for m in re.finditer(r"\bWITH\b.*?(\w+)\s+AS\s*\(", upper):
        info.ctes.append(m.group(1))

    for pat in [r"\bFROM\s+([.\w]+)", r"\bJOIN\s+([.\w]+)"]:
        for m in re.finditer(pat, upper):
            info.sources.append(_ref_from_str(m.group(1)))

    _infer_object_type_regex(sql, info)


def _infer_object_type_regex(sql: str, info: SQLFileInfo) -> None:
    upper = sql.upper()
    if "CREATE" in upper and "VIEW" in upper:
        info.object_type = "VIEW"
    elif "CREATE" in upper and "TABLE" in upper:
        info.object_type = "TABLE"
    elif "CREATE" in upper and "PIPE" in upper:
        info.object_type = "PIPE"
    elif "CREATE" in upper and "PROCEDURE" in upper:
        info.object_type = "PROCEDURE"
    elif "MERGE INTO" in upper:
        info.object_type = "TABLE"
    elif "INSERT INTO" in upper:
        info.object_type = "TABLE"


def _infer_layer(path: str, sql: str) -> str:
    lower = (path + " " + sql[:200]).lower()
    if "/raw/" in lower or "\\raw\\" in lower or "schema raw" in lower or "use schema raw" in lower:
        return "raw"
    if "/stage/" in lower or "\\stage\\" in lower or "schema stage" in lower or "/stg/" in lower:
        return "stage"
    if "/silver/" in lower or "\\silver\\" in lower or "schema silver" in lower:
        return "silver"
    if "/mart/" in lower or "\\mart\\" in lower or "schema mart" in lower:
        return "mart"
    if "/gold/" in lower or "\\gold\\" in lower or "schema gold" in lower:
        return "gold"
    return "unknown"


def _populate_column_lineage(sql: str, info: SQLFileInfo) -> None:
    """Parse the first SELECT in *sql* and map each output column to its source table/column."""
    try:
        stmts = sqlglot.parse(sql, dialect="snowflake", error_level=sqlglot.ErrorLevel.WARN)
        for stmt in (s for s in stmts if s is not None):
            sel = stmt.find(exp.Select)
            if sel:
                info.column_lineage = _extract_column_mappings(sel, info.sources)
                break
    except Exception:
        pass


def _extract_column_mappings(sel: "exp.Select", sources: list[TableRef]) -> list[ColumnMapping]:
    """Return column-to-source mappings for a SELECT node, capped at 20 columns."""
    # Build alias→full_name from this SELECT's own FROM/JOIN — more precise than global sources,
    # since the global list can include USE SCHEMA targets or MERGE targets.
    alias_to_full: dict[str, str] = {}
    # sqlglot uses "from" for top-level SELECTs and "from_" inside MERGE/subquery contexts
    from_expr = sel.args.get("from") or sel.args.get("from_")
    joins = sel.args.get("joins") or []
    for node in ([from_expr] if from_expr else []) + list(joins):
        if node is None:
            continue
        for tbl in node.find_all(exp.Table):
            ref = _table_ref(tbl)
            full = str(ref)
            alias_to_full[tbl.name.upper()] = full
            if tbl.alias:
                alias_to_full[tbl.alias.upper()] = full

    unique_tables = list(dict.fromkeys(alias_to_full.values()))
    # Fall back to global sources if FROM clause yielded nothing (e.g. regex-parsed info)
    if not unique_tables and len(sources) == 1:
        single_src: str | None = str(sources[0])
    else:
        single_src = unique_tables[0] if len(unique_tables) == 1 else None

    result: list[ColumnMapping] = []
    for col_expr in sel.expressions[:20]:
        alias = col_expr.alias if hasattr(col_expr, "alias") and col_expr.alias else None
        inner = col_expr.this if isinstance(col_expr, exp.Alias) else col_expr

        if isinstance(inner, exp.Star):
            result.append(ColumnMapping(target_col="*", source_table=single_src, source_col="*"))
        elif isinstance(inner, exp.Column):
            if isinstance(inner.this, exp.Star):
                # Qualified star: tbl.*
                tbl_key = (inner.table or "").upper()
                tbl_full = alias_to_full.get(tbl_key) or inner.table or single_src
                result.append(ColumnMapping(target_col="*", source_table=tbl_full, source_col="*"))
            else:
                col_name = inner.name
                tbl_key = (inner.table or "").upper()
                tbl_full = alias_to_full.get(tbl_key) or inner.table or single_src
                result.append(ColumnMapping(
                    target_col=alias or col_name,
                    source_table=tbl_full,
                    source_col=col_name,
                ))
        else:
            # Computed/expression column — attribute to single source when unambiguous
            result.append(ColumnMapping(
                target_col=alias or "(expression)",
                source_table=single_src,
                source_col=None,
            ))

    return result


def _table_ref(tbl: "exp.Table") -> TableRef:
    db = tbl.args.get("db")
    db_name = db.name if db else None
    return TableRef(schema=db_name, name=tbl.name)


def _ref_from_str(raw: str) -> TableRef:
    parts = raw.strip().split(".")
    if len(parts) >= 2:
        return TableRef(schema=parts[-2], name=parts[-1])
    return TableRef(schema=None, name=parts[0])
