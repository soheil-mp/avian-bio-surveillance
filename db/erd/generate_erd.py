"""Generate ``data_model.drawio`` from the SQL schema files.

The SQL files under ``db/baseline/``, ``db/caggs/``, and ``db/views/`` are the
source of truth. This script parses those files into a lightweight schema model
and derives the drawing layout from the SQL file order and query dependencies.

Run via ``make erd`` or directly: ``python db/erd/generate_erd.py``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DB_ROOT = REPO_ROOT / "db"
OUTPUT_PATH = Path(__file__).with_name("data_model.drawio")


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass
class Column:
    """Column metadata rendered as an ERD row."""

    name: str
    data_type: str
    not_null: bool = False
    pk: bool = False
    fk: bool = False
    fk_ref: str = ""
    extra: str = ""
    partition_key: bool = False


@dataclass
class Table:
    """Table, continuous aggregate, or view metadata."""

    id_prefix: str
    name: str
    columns: list[Column]
    source_refs: list[str] = field(default_factory=list)
    source_path: str = ""
    source_group: str = ""
    source_order: int = 0
    object_order: int = 0
    x: int = 0
    y: int = 0
    width: int = 380
    pk_fill: str = "#D6E4F0"
    pk_stroke: str = "#B4CCE2"
    fk_fill: str = "#E1D5E7"
    fk_stroke: str = "#9673A6"
    is_cagg: bool = False
    is_view: bool = False
    is_hypertable: bool = False
    subtitle: str = ""

    @property
    def row_height(self) -> int:
        return 26

    @property
    def header_height(self) -> int:
        return 44 if self.subtitle else 30

    @property
    def height(self) -> int:
        return self.header_height + len(self.columns) * self.row_height

    def column_index(self, name: str) -> int | None:
        for idx, col in enumerate(self.columns, start=1):
            if col.name == name:
                return idx
        return None

    def row_id_for(self, name: str, *, default: int = 1) -> str:
        idx = self.column_index(name) or default
        return f"{self.id_prefix}-r{idx}"


@dataclass
class GroupLayout:
    """Placement metadata for tables that share a rendered source group."""

    name: str
    column_stacks: list[list[Table]]
    column_widths: list[int]
    width: int
    height: int


# ---------------------------------------------------------------------------
# SQL parsing helpers
# ---------------------------------------------------------------------------


CONSTRAINT_STARTERS = {
    "NOT",
    "NULL",
    "DEFAULT",
    "CHECK",
    "PRIMARY",
    "UNIQUE",
    "REFERENCES",
    "GENERATED",
    "COLLATE",
    "CONSTRAINT",
}

TABLE_CONSTRAINT_STARTERS = {
    "PRIMARY",
    "FOREIGN",
    "CONSTRAINT",
    "UNIQUE",
    "CHECK",
    "EXCLUDE",
}

TYPE_ALIASES = {
    "COUNT": "BIGINT",
    "AVG": "DOUBLE PRECISION",
    "SUM": "DOUBLE PRECISION",
    "MIN": "DOUBLE PRECISION",
    "MAX": "DOUBLE PRECISION",
    "TIME_BUCKET": "TIMESTAMPTZ",
    "JSONB_AGG": "JSONB",
    "JSONB_BUILD_OBJECT": "JSONB",
    "FORMAT": "TEXT",
    "LPAD": "TEXT",
}


def _read_sql(paths: list[Path]) -> str:
    return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)


def _source_group(path: Path) -> str:
    rel = path.relative_to(DB_ROOT)
    if rel.parts[0] == "baseline":
        return path.stem
    return rel.as_posix()


def _source_order(path: Path) -> int:
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return int(match.group(1))
    folder_order = {"baseline": 1000, "caggs": 2000, "views": 3000}
    try:
        folder = path.relative_to(DB_ROOT).parts[0]
    except ValueError:
        folder = ""
    return folder_order.get(folder, 9000)


def _strip_comments(sql: str) -> str:
    """Remove SQL comments while preserving quoted string contents."""
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    out: list[str] = []
    for line in sql.splitlines():
        in_single = False
        i = 0
        while i < len(line):
            ch = line[i]
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if ch == "'" and nxt == "'":
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single = not in_single
                out.append(ch)
                i += 1
                continue
            if not in_single and ch == "-" and nxt == "-":
                break
            out.append(ch)
            i += 1
        out.append("\n")
    return "".join(out)


def _find_matching_paren(sql: str, open_idx: int) -> int:
    depth = 0
    in_single = False
    i = open_idx
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if ch == "'" and nxt == "'":
            i += 2
            continue
        if ch == "'":
            in_single = not in_single
        elif not in_single and ch == "(":
            depth += 1
        elif not in_single and ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("Unbalanced parentheses while parsing SQL")


def _split_top_level_csv(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_single = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch == "'" and nxt == "'":
            i += 2
            continue
        if ch == "'":
            in_single = not in_single
        elif not in_single and ch == "(":
            depth += 1
        elif not in_single and ch == ")":
            depth -= 1
        elif not in_single and depth == 0 and ch == ",":
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _clean_identifier(value: str) -> str:
    return value.strip().strip('"')


def _normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _column_name_list(value: str) -> list[str]:
    return [_clean_identifier(v) for v in _split_top_level_csv(value)]


def _definition_name(definition: str) -> str:
    return _clean_identifier(definition.split(None, 1)[0])


def _parse_column(definition: str) -> Column | None:
    tokens = _normalize_ws(definition).split()
    if not tokens:
        return None
    if tokens[0].upper() in TABLE_CONSTRAINT_STARTERS:
        return None

    name = _clean_identifier(tokens[0])
    rest = tokens[1:]
    type_parts: list[str] = []
    for token in rest:
        if token.upper() in CONSTRAINT_STARTERS:
            break
        type_parts.append(token)

    normalized = _normalize_ws(definition)
    data_type = " ".join(type_parts) or "UNKNOWN"
    not_null = bool(re.search(r"\bNOT\s+NULL\b", normalized, re.I))
    pk = bool(re.search(r"\bPRIMARY\s+KEY\b", normalized, re.I))
    extra_parts: list[str] = []

    generated = re.search(
        r"\bGENERATED\s+ALWAYS\s+AS\s+IDENTITY\b",
        normalized,
        re.I,
    )
    if generated:
        extra_parts.append("GENERATED ALWAYS AS IDENTITY")

    default_match = re.search(
        r"\bDEFAULT\s+(.+?)(?=\s+(?:NOT\s+NULL|NULL|CHECK|PRIMARY\s+KEY|"
        r"UNIQUE|REFERENCES|CONSTRAINT|COLLATE)\b|$)",
        normalized,
        re.I,
    )
    if default_match:
        extra_parts.append(f"DEFAULT {default_match.group(1).strip()}")

    fk = False
    fk_ref = ""
    ref_match = re.search(
        r"\bREFERENCES\s+([a-zA-Z_][\w]*)\s*\(([^)]+)\)",
        normalized,
        re.I,
    )
    if ref_match:
        fk = True
        ref_table = ref_match.group(1)
        ref_cols = ", ".join(_column_name_list(ref_match.group(2)))
        fk_ref = f"{ref_table}({ref_cols})"

    return Column(
        name=name,
        data_type=data_type,
        not_null=not_null,
        pk=pk,
        fk=fk,
        fk_ref=fk_ref,
        extra=" ".join(extra_parts),
    )


def _parse_table_defs(
    sql: str,
    *,
    source_path: Path,
    object_offset: int = 0,
) -> dict[str, Table]:
    cleaned = _strip_comments(sql)
    tables: dict[str, Table] = {}
    pattern = re.compile(
        r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][\w]*)\s*\(",
        re.I,
    )

    source_rel = source_path.relative_to(DB_ROOT).as_posix()
    source_group = _source_group(source_path)
    source_order = _source_order(source_path)

    for object_index, match in enumerate(pattern.finditer(cleaned), start=1):
        name = match.group(1)
        open_idx = cleaned.find("(", match.end() - 1)
        close_idx = _find_matching_paren(cleaned, open_idx)
        body = cleaned[open_idx + 1:close_idx]
        definitions = _split_top_level_csv(body)
        columns: list[Column] = []

        for definition in definitions:
            column = _parse_column(definition)
            if column:
                columns.append(column)

        table = Table(
            id_prefix=_prefix_for(name),
            name=name,
            columns=columns,
            source_path=source_rel,
            source_group=source_group,
            source_order=source_order,
            object_order=object_offset + object_index,
        )
        column_map = {col.name: col for col in table.columns}

        for definition in definitions:
            normalized = _normalize_ws(definition)
            pk_match = re.search(
                r"\bPRIMARY\s+KEY\s*\(([^)]+)\)",
                normalized,
                re.I,
            )
            if pk_match:
                for col_name in _column_name_list(pk_match.group(1)):
                    if col_name in column_map:
                        column_map[col_name].pk = True
                        column_map[col_name].not_null = True

            fk_match = re.search(
                r"\bFOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+"
                r"([a-zA-Z_][\w]*)\s*\(([^)]+)\)",
                normalized,
                re.I,
            )
            if fk_match:
                source_cols = _column_name_list(fk_match.group(1))
                ref_table = fk_match.group(2)
                ref_cols = _column_name_list(fk_match.group(3))
                for idx, col_name in enumerate(source_cols):
                    if col_name not in column_map:
                        continue
                    ref_col = ref_cols[min(idx, len(ref_cols) - 1)]
                    column_map[col_name].fk = True
                    column_map[col_name].fk_ref = f"{ref_table}({ref_col})"

        tables[name] = table

    return tables


def _apply_alter_add_columns(sql: str, tables: dict[str, Table]) -> None:
    cleaned = _strip_comments(sql)
    pattern = re.compile(
        r"\bALTER\s+TABLE\s+([a-zA-Z_][\w]*)\s+ADD\s+COLUMN\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?([^;]+);",
        re.I | re.S,
    )
    for match in pattern.finditer(cleaned):
        table = tables.get(match.group(1))
        if not table:
            continue
        column = _parse_column(match.group(2))
        if column and table.column_index(column.name) is None:
            table.columns.append(column)


def _apply_alter_foreign_keys(sql: str, tables: dict[str, Table]) -> None:
    cleaned = _strip_comments(sql)
    pattern = re.compile(
        r"\bALTER\s+TABLE\s+([a-zA-Z_][\w]*)\s+.*?\bFOREIGN\s+KEY\s*"
        r"\(([^)]+)\)\s+REFERENCES\s+([a-zA-Z_][\w]*)\s*\(([^)]+)\)",
        re.I | re.S,
    )
    for match in pattern.finditer(cleaned):
        table = tables.get(match.group(1))
        if not table:
            continue
        source_cols = _column_name_list(match.group(2))
        ref_table = match.group(3)
        ref_cols = _column_name_list(match.group(4))
        for idx, source_col in enumerate(source_cols):
            col_idx = table.column_index(source_col)
            if col_idx is None:
                continue
            column = table.columns[col_idx - 1]
            ref_col = ref_cols[min(idx, len(ref_cols) - 1)]
            column.fk = True
            column.fk_ref = f"{ref_table}({ref_col})"


def _apply_hypertables(sql: str, tables: dict[str, Table]) -> None:
    cleaned = _strip_comments(sql)
    pattern = re.compile(
        r"\bcreate_hypertable\s*\(\s*'([^']+)'\s*,\s*'([^']+)'.*?"
        r"chunk_time_interval\s*=>\s*INTERVAL\s*'([^']+)'",
        re.I | re.S,
    )
    for table_name, partition_col, interval in pattern.findall(cleaned):
        table = tables.get(table_name)
        if not table:
            continue
        table.is_hypertable = True
        table.subtitle = f"hypertable · {interval} chunks"
        idx = table.column_index(partition_col)
        if idx is not None:
            table.columns[idx - 1].partition_key = True


def _infer_expr_type(expr: str, source_tables: dict[str, Table]) -> str:
    expr = expr.strip()
    upper = expr.upper()
    if upper.startswith("CASE "):
        branch_types: list[str] = []
        for branch_expr in re.findall(
            r"\bTHEN\s+(.+?)(?=\bWHEN\b|\bELSE\b|\bEND\b)",
            expr,
            re.I | re.S,
        ):
            branch_type = _infer_expr_type(branch_expr.strip(), source_tables)
            if branch_type != "UNKNOWN":
                branch_types.append(branch_type)

        else_match = re.search(r"\bELSE\s+(.+?)\s*END\b", expr, re.I | re.S)
        if else_match:
            branch_type = _infer_expr_type(else_match.group(1).strip(), source_tables)
            if branch_type != "UNKNOWN":
                branch_types.append(branch_type)

        if branch_types:
            return branch_types[0]

    for fn_name, data_type in TYPE_ALIASES.items():
        if upper.startswith(f"{fn_name}("):
            return data_type
    if "->>" in expr or "#>>" in expr:
        return "TEXT"
    if "->" in expr or "#>" in expr:
        return "JSONB"
    cast_match = re.search(
        r"::\s*([a-zA-Z_][\w]*(?:\s+[a-zA-Z_][\w]*)*)\s*$",
        expr,
    )
    if cast_match:
        return _normalize_ws(cast_match.group(1)).upper()
    if re.match(r"^'.*'$", expr):
        return "TEXT"
    if re.match(r"^\d+(\.\d+)?$", expr):
        return "DOUBLE PRECISION" if "." in expr else "INTEGER"

    col_name = expr.split(".")[-1].strip('"')
    for table in source_tables.values():
        idx = table.column_index(col_name)
        if idx is not None:
            return table.columns[idx - 1].data_type
    return "UNKNOWN"


def _select_alias(select_item: str) -> tuple[str, str]:
    item = _normalize_ws(select_item)
    alias_match = re.search(r"\s+AS\s+([a-zA-Z_][\w]*)$", item, re.I)
    if alias_match:
        return item[:alias_match.start()].strip(), alias_match.group(1)
    parts = item.rsplit(" ", 1)
    if len(parts) == 2 and re.match(r"^[a-zA-Z_][\w]*$", parts[1]):
        expr_upper = parts[0].upper()
        if "(" in expr_upper or "." in expr_upper or "::" in expr_upper:
            return parts[0].strip(), parts[1]
    return item, item.split(".")[-1].strip('"')


def _source_aliases(query: str, tables: dict[str, Table]) -> dict[str, Table]:
    aliases: dict[str, Table] = {}
    pattern = re.compile(
        r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)"
        r"(?:\s+(?:AS\s+)?([a-zA-Z_][\w]*))?",
        re.I,
    )
    for table_name, alias in pattern.findall(query):
        table = tables.get(table_name)
        if not table:
            continue
        aliases[alias or table_name] = table
        aliases[table_name] = table
    return aliases


def _ordered_source_names(query: str, tables: dict[str, Table]) -> list[str]:
    out: list[str] = []
    for source_table in _source_aliases(query, tables).values():
        if source_table.name not in out:
            out.append(source_table.name)
    return out


def _final_select_list(query: str) -> str | None:
    upper = query.upper()
    select_idx = upper.rfind("SELECT")
    if select_idx == -1:
        return None
    from_idx = upper.find("FROM", select_idx)
    if from_idx == -1:
        return None
    return query[select_idx + len("SELECT"):from_idx]


def _view_columns(query: str, tables: dict[str, Table]) -> list[Column]:
    select_list = _final_select_list(query)
    if select_list is None:
        return []
    aliases = _source_aliases(query, tables)
    columns: list[Column] = []

    for item in _split_top_level_csv(select_list):
        expr, alias = _select_alias(item)
        star_match = re.match(r"^([a-zA-Z_][\w]*)\.\*$", expr)
        if star_match:
            source = aliases.get(star_match.group(1))
            if source:
                columns.extend(
                    Column(
                        name=col.name,
                        data_type=col.data_type,
                        not_null=col.not_null,
                        pk=col.pk,
                        fk=col.fk,
                        fk_ref=col.fk_ref,
                        extra=col.extra,
                        partition_key=col.partition_key,
                    )
                    for col in source.columns
                )
            continue

        data_type = _infer_expr_type(expr, aliases)
        columns.append(Column(name=alias, data_type=data_type))
    return columns


def _parse_materialized_views(
    sql: str,
    tables: dict[str, Table],
    *,
    source_path: Path,
    object_offset: int = 0,
) -> dict[str, Table]:
    cleaned = _strip_comments(sql)
    result: dict[str, Table] = {}
    pattern = re.compile(
        r"\bCREATE\s+MATERIALIZED\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"([a-zA-Z_][\w]*)\b(.*?);",
        re.I | re.S,
    )
    source_rel = source_path.relative_to(DB_ROOT).as_posix()
    source_group = _source_group(source_path)
    source_order = _source_order(source_path)

    for object_index, (name, query) in enumerate(pattern.findall(cleaned), start=1):
        columns = _view_columns(query, tables)
        sources = _ordered_source_names(query, tables)
        result[name] = Table(
            id_prefix=_prefix_for(name),
            name=name,
            columns=columns,
            source_refs=sources,
            source_path=source_rel,
            source_group=source_group,
            source_order=source_order,
            object_order=object_offset + object_index,
            is_cagg=True,
            subtitle="continuous aggregate",
        )
    return result


def _parse_views(
    sql: str,
    tables: dict[str, Table],
    *,
    source_path: Path,
    object_offset: int = 0,
) -> dict[str, Table]:
    cleaned = _strip_comments(sql)
    result: dict[str, Table] = {}
    pattern = re.compile(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+([a-zA-Z_][\w]*)\s+AS\s+"
        r"(.*?);",
        re.I | re.S,
    )
    source_rel = source_path.relative_to(DB_ROOT).as_posix()
    source_group = _source_group(source_path)
    source_order = _source_order(source_path)

    for object_index, (name, query) in enumerate(pattern.findall(cleaned), start=1):
        columns = _view_columns(query, tables)
        sources = _ordered_source_names(query, tables)
        result[name] = Table(
            id_prefix=_prefix_for(name),
            name=name,
            columns=columns,
            source_refs=sources,
            source_path=source_rel,
            source_group=source_group,
            source_order=source_order,
            object_order=object_offset + object_index,
            is_view=True,
            is_cagg=True,
            subtitle="view",
        )
    return result


# ---------------------------------------------------------------------------
# Layout and styling
# ---------------------------------------------------------------------------


def _prefix_for(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    return safe


GAP = 50
COLUMN_GAP = 70
GROUP_COLUMN_GAP = 40
ROW_GAP = 80
TOP_Y = 100
LEFT_X = 50
MIN_WIDTH = 380
MAX_WIDTH = 520
TARGET_PAGE_ASPECT = 1.1


PALETTE = [
    ("#D6E4F0", "#B4CCE2"),
    ("#DAE9D5", "#B8D4AE"),
    ("#FCE5CD", "#E6C49E"),
    ("#D5E8D4", "#82B366"),
    ("#DAE8FC", "#6C8EBF"),
    ("#FFE6CC", "#D79B00"),
    ("#F8CECC", "#B85450"),
    ("#E1D5E7", "#9673A6"),
]


def _source_label(group: str) -> str:
    leaf = group.rsplit("/", 1)[-1]
    return re.sub(r"^\d+_", "", leaf).replace("_", " ").upper()


def _layout_group(table: Table, tables: dict[str, Table]) -> str:
    if (table.is_cagg or table.is_view) and table.source_refs:
        source = tables.get(table.source_refs[0])
        if source:
            return source.source_group
    return table.source_group or table.source_path or "unassigned"


def _display_width(table: Table) -> int:
    longest = max(
        (len(_col_display(col)) for col in table.columns),
        default=len(table.name),
    )
    return max(MIN_WIDTH, min(MAX_WIDTH, 220 + longest * 5))


def _style_table(table: Table, group_index: int) -> None:
    fill, stroke = PALETTE[group_index % len(PALETTE)]
    table.width = _display_width(table)
    table.pk_fill = fill
    table.pk_stroke = stroke

    if table.is_view:
        table.subtitle = table.subtitle or "view"
    elif table.is_cagg:
        table.subtitle = table.subtitle or "continuous aggregate"
    elif table.is_hypertable:
        table.subtitle = table.subtitle or "hypertable"
    elif not table.subtitle and table.source_group:
        table.subtitle = _source_label(table.source_group).title()


def _table_layout_key(table: Table) -> tuple[int, int, int, str]:
    return (
        1 if table.is_cagg or table.is_view else 0,
        table.source_order,
        table.object_order,
        table.name,
    )


def _stack_height(tables: list[Table]) -> int:
    if not tables:
        return 0
    return sum(table.height for table in tables) + GAP * (len(tables) - 1)


def _estimate_layout_targets(
    widths: list[int],
    heights: list[int],
    *,
    min_width: int,
    min_height: int,
) -> tuple[int, int]:
    total_area = sum(width * height for width, height in zip(widths, heights)) or 1
    target_width = max(min_width, math.ceil(math.sqrt(total_area * TARGET_PAGE_ASPECT)))
    target_height = max(
        min_height,
        math.ceil(math.sqrt(total_area / TARGET_PAGE_ASPECT)),
    )
    return target_width, target_height


def _build_group_layout(
    group: str,
    stack: list[Table],
    *,
    target_height: int,
) -> GroupLayout:
    if not stack:
        return GroupLayout(
            name=group,
            column_stacks=[],
            column_widths=[],
            width=MIN_WIDTH,
            height=0,
        )

    total_height = _stack_height(stack)
    tallest_table = max(table.height for table in stack)
    column_count = max(1, min(len(stack), math.ceil(total_height / target_height)))
    column_target_height = max(tallest_table, math.ceil(total_height / column_count))

    column_stacks: list[list[Table]] = [[]]
    column_widths = [0]
    column_heights = [0]

    for table in stack:
        projected_height = table.height
        if column_stacks[-1]:
            projected_height += GAP

        can_wrap = len(column_stacks) < column_count and bool(column_stacks[-1])
        if can_wrap and column_heights[-1] + projected_height > column_target_height:
            column_stacks.append([])
            column_widths.append(0)
            column_heights.append(0)

        column_stacks[-1].append(table)
        if column_heights[-1]:
            column_heights[-1] += GAP
        column_heights[-1] += table.height
        column_widths[-1] = max(column_widths[-1], table.width)

    width = sum(column_widths) + GROUP_COLUMN_GAP * (len(column_widths) - 1)
    height = max(column_heights)
    return GroupLayout(
        name=group,
        column_stacks=column_stacks,
        column_widths=column_widths,
        width=width,
        height=height,
    )


def _apply_layout(tables: dict[str, Table]) -> list[Table]:
    groups: dict[str, list[Table]] = {}
    group_order: dict[str, tuple[int, str]] = {}

    for table in tables.values():
        group = _layout_group(table, tables)
        groups.setdefault(group, []).append(table)
        group_order.setdefault(group, (table.source_order, group))

    ordered_groups = sorted(group_order, key=lambda key: group_order[key])
    prepared_groups: list[tuple[str, list[Table]]] = []
    raw_widths: list[int] = []
    raw_heights: list[int] = []
    max_table_height = 0

    for group_index, group in enumerate(ordered_groups):
        stack = sorted(groups[group], key=_table_layout_key)
        for table in stack:
            _style_table(table, group_index)
            max_table_height = max(max_table_height, table.height)
        prepared_groups.append((group, stack))
        raw_widths.append(max((table.width for table in stack), default=MIN_WIDTH))
        raw_heights.append(_stack_height(stack))

    target_width, target_height = _estimate_layout_targets(
        raw_widths,
        raw_heights,
        min_width=max(raw_widths, default=MIN_WIDTH),
        min_height=max_table_height or TOP_Y,
    )

    group_layouts = [
        _build_group_layout(group, stack, target_height=target_height)
        for group, stack in prepared_groups
    ]

    final_widths = [layout.width for layout in group_layouts]
    final_heights = [layout.height for layout in group_layouts]
    target_width, _ = _estimate_layout_targets(
        final_widths,
        final_heights,
        min_width=max(final_widths, default=MIN_WIDTH),
        min_height=max(final_heights, default=TOP_Y),
    )
    total_linear_width = sum(final_widths) + COLUMN_GAP * max(len(final_widths) - 1, 0)
    target_width = max(target_width, math.ceil(total_linear_width * 0.6))

    ordered: list[Table] = []
    x = LEFT_X
    y = TOP_Y
    row_height = 0

    for layout in group_layouts:
        if x > LEFT_X and x + layout.width > LEFT_X + target_width:
            x = LEFT_X
            y += row_height + ROW_GAP
            row_height = 0

        group_x = x
        for column_stack, column_width in zip(layout.column_stacks, layout.column_widths):
            column_y = y
            for table in column_stack:
                table.x = group_x
                table.y = column_y
                column_y += table.height + GAP
                ordered.append(table)
            group_x += column_width + GROUP_COLUMN_GAP

        row_height = max(row_height, layout.height)
        x += layout.width + COLUMN_GAP

    return ordered


# ---------------------------------------------------------------------------
# Schema assembly
# ---------------------------------------------------------------------------


def load_schema() -> list[Table]:
    baseline_paths = sorted((DB_ROOT / "baseline").glob("*.sql"))
    cagg_paths = sorted((DB_ROOT / "caggs").glob("*.sql"))
    view_paths = sorted((DB_ROOT / "views").glob("*.sql"))

    tables: dict[str, Table] = {}
    for path in baseline_paths:
        tables.update(
            _parse_table_defs(
                path.read_text(encoding="utf-8"),
                source_path=path,
            )
        )

    baseline_sql = _read_sql(baseline_paths)
    _apply_alter_add_columns(baseline_sql, tables)
    _apply_alter_foreign_keys(baseline_sql, tables)
    _apply_hypertables(baseline_sql, tables)

    for path in baseline_paths:
        tables.update(
            _parse_views(
                path.read_text(encoding="utf-8"),
                tables,
                source_path=path,
            )
        )

    for path in cagg_paths:
        tables.update(
            _parse_materialized_views(
                path.read_text(encoding="utf-8"),
                tables,
                source_path=path,
            )
        )

    for path in view_paths:
        tables.update(
            _parse_views(
                path.read_text(encoding="utf-8"),
                tables,
                source_path=path,
            )
        )

    return _apply_layout(tables)


# ---------------------------------------------------------------------------
# XML generation helpers
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _col_display(col: Column) -> str:
    parts = [col.name, " ", col.data_type]
    if col.not_null:
        parts.append("  NOT NULL")
    if col.extra:
        parts.extend(["  ", col.extra])
    if col.partition_key:
        parts.append("  (partition key)")
    return "".join(parts)


def build_table_xml(tbl: Table) -> list[str]:
    lines: list[str] = []
    p = tbl.id_prefix
    val_w = tbl.width - 35
    header_style_parts = [
        "shape=table",
        f"startSize={tbl.header_height}",
        "container=1",
        "collapsible=0",
        "childLayout=tableLayout",
        "fixedRows=1",
        "rowLines=1",
        "fontStyle=1",
        "align=center",
        "resizeLast=1",
        "fontSize=13",
        "rounded=1",
        "arcSize=6",
        "shadow=1",
        "html=1",
        "fillColor=#f5f5f5",
        "fontColor=#333333",
        "strokeColor=#666666",
    ]
    if tbl.is_cagg or tbl.is_view:
        header_style_parts.extend(["dashed=1", "dashPattern=8 4"])
    header_style = ";".join(header_style_parts) + ";"

    display_name = tbl.name
    if tbl.subtitle:
        display_name += (
            "&lt;br&gt;&lt;font style=&quot;font-size:9px;font-weight:normal;"
            f"color:#666;&quot;&gt;{_esc(tbl.subtitle)}&lt;/font&gt;"
        )

    lines.append(
        f'                <mxCell id="{p}-h" value="{display_name}" '
        f'style="{header_style}" parent="1" vertex="1">'
    )
    lines.append(
        f'                    <mxGeometry x="{tbl.x}" y="{tbl.y}" '
        f'width="{tbl.width}" height="{tbl.height}" as="geometry"/>'
    )
    lines.append("                </mxCell>")

    for idx, col in enumerate(tbl.columns):
        row_y = tbl.header_height + idx * tbl.row_height
        row_id = f"{p}-r{idx + 1}"
        key_id = f"{p}-k{idx + 1}"
        val_id = f"{p}-v{idx + 1}"

        if col.pk:
            row_fill = tbl.pk_fill
            row_stroke = f"strokeColor={tbl.pk_stroke};"
            key_val = "PK"
            key_style = (
                "shape=partialRectangle;connectable=0;top=0;left=0;bottom=0;"
                "right=0;fontStyle=0;overflow=hidden;fontSize=9;"
                "align=center;"
            )
            val_style = (
                "shape=partialRectangle;connectable=0;top=0;left=0;bottom=0;"
                "right=0;overflow=hidden;fontSize=10;fontStyle=0;"
                "spacingLeft=6;"
            )
        elif col.fk:
            row_fill = tbl.fk_fill
            row_stroke = f"strokeColor={tbl.fk_stroke};"
            key_val = "FK"
            key_style = (
                "shape=partialRectangle;connectable=0;top=0;left=0;bottom=0;"
                "right=0;fontStyle=1;overflow=hidden;fontSize=9;"
                "align=center;fontColor=#7B2D8E;"
            )
            val_style = (
                "shape=partialRectangle;connectable=0;top=0;left=0;bottom=0;"
                "right=0;overflow=hidden;fontSize=10;fontStyle=0;"
                "spacingLeft=6;"
            )
        elif col.not_null:
            row_fill = "#FFFFFF"
            row_stroke = ""
            key_val = "NN"
            key_style = (
                "shape=partialRectangle;connectable=0;fillColor=#FFFFFF;"
                "top=0;left=0;bottom=0;right=0;overflow=hidden;"
                "fontSize=8;fontColor=#B05000;align=center;fontStyle=2;"
            )
            val_style = (
                "shape=partialRectangle;connectable=0;fillColor=#FFFFFF;"
                "top=0;left=0;bottom=0;right=0;overflow=hidden;"
                "fontSize=10;fontColor=#333333;spacingLeft=6;"
            )
        else:
            row_fill = "#FFFFFF"
            row_stroke = ""
            key_val = ""
            key_style = (
                "shape=partialRectangle;connectable=0;fillColor=#FFFFFF;"
                "top=0;left=0;bottom=0;right=0;overflow=hidden;"
                "fontSize=8;fontColor=#999999;align=center;"
            )
            val_style = (
                "shape=partialRectangle;connectable=0;fillColor=#FFFFFF;"
                "top=0;left=0;bottom=0;right=0;overflow=hidden;"
                "fontSize=10;fontColor=#333333;spacingLeft=6;"
            )

        row_style = (
            "shape=tableRow;horizontal=0;startSize=0;swimlaneHead=0;"
            "swimlaneBody=0;"
            f"fillColor={row_fill};collapsible=0;dropTarget=0;"
            "points=[[0,0.5],[1,0.5]];portConstraint=eastwest;"
            f"fontSize=10;top=0;left=0;right=0;bottom=0;{row_stroke}"
        )
        lines.append(
            f'                <mxCell id="{row_id}" value="" '
            f'style="{row_style}" parent="{p}-h" vertex="1">'
        )
        lines.append(
            f'                    <mxGeometry y="{row_y}" width="{tbl.width}" '
            f'height="{tbl.row_height}" as="geometry"/>'
        )
        lines.append("                </mxCell>")

        lines.append(
            f'                <mxCell id="{key_id}" value="{key_val}" '
            f'style="{key_style}" parent="{row_id}" vertex="1">'
        )
        lines.append(
            f'                    <mxGeometry width="35" '
            f'height="{tbl.row_height}" '
            'as="geometry">'
        )
        lines.append(
            f'                        <mxRectangle width="35" '
            f'height="{tbl.row_height}" as="alternateBounds"/>'
        )
        lines.append("                    </mxGeometry>")
        lines.append("                </mxCell>")

        display = _esc(_col_display(col))
        lines.append(
            f'                <mxCell id="{val_id}" value="{display}" '
            f'style="{val_style}" parent="{row_id}" vertex="1">'
        )
        lines.append(
            f'                    <mxGeometry x="35" width="{val_w}" '
            f'height="{tbl.row_height}" as="geometry">'
        )
        lines.append(
            f'                        <mxRectangle width="{val_w}" '
            f'height="{tbl.row_height}" as="alternateBounds"/>'
        )
        lines.append("                    </mxGeometry>")
        lines.append("                </mxCell>")

    return lines


def build_edge(
    edge_id: str,
    source_row: str,
    target_row: str,
    label: str = "",
    dashed: bool = False,
) -> list[str]:
    if dashed:
        style_parts = [
            "endArrow=open",
            "startArrow=none",
            "endFill=0",
            "startFill=0",
            "edgeStyle=orthogonalEdgeStyle",
            "rounded=1",
            "strokeColor=#999999",
            "fontSize=9",
            "dashed=1",
            "dashPattern=8 4",
        ]
    else:
        style_parts = [
            "endArrow=ERone",
            "startArrow=ERmany",
            "endFill=0",
            "startFill=0",
            "edgeStyle=orthogonalEdgeStyle",
            "rounded=1",
            "strokeColor=#666666",
            "fontSize=10",
        ]
    style = ";".join(style_parts) + ";"
    val = _esc(label) if label else ""
    return [
        f'                <mxCell id="{edge_id}" value="{val}" '
        f'style="{style}" parent="1" edge="1" '
        f'source="{source_row}" target="{target_row}">',
        '                    <mxGeometry relative="1" as="geometry"/>',
        "                </mxCell>",
    ]


def _fk_edges(tables: list[Table]) -> list[list[str]]:
    by_name = {table.name: table for table in tables}
    edges: list[list[str]] = []
    for table in tables:
        for idx, col in enumerate(table.columns, start=1):
            if not col.fk or not col.fk_ref:
                continue
            ref_match = re.match(r"([a-zA-Z_][\w]*)\(([^)]+)\)", col.fk_ref)
            if not ref_match:
                continue
            target = by_name.get(ref_match.group(1))
            if not target:
                continue
            target_col = _column_name_list(ref_match.group(2))[0]
            edge_id = f"rel-{table.id_prefix}-{target.id_prefix}-{col.name}"
            edges.append(
                build_edge(
                    edge_id,
                    f"{table.id_prefix}-r{idx}",
                    target.row_id_for(target_col),
                    label=f"FK {col.name}",
                )
            )
    return edges


def _source_edges(tables: list[Table]) -> list[list[str]]:
    by_name = {table.name: table for table in tables}
    edges: list[list[str]] = []
    seen: set[str] = set()
    for table in tables:
        for source_name in table.source_refs:
            source = by_name.get(source_name)
            if not source:
                continue
            edge_id = f"rel-{table.id_prefix}-{source.id_prefix}-source"
            if edge_id in seen:
                continue
            seen.add(edge_id)
            edges.append(
                build_edge(
                    edge_id,
                    table.row_id_for(
                        table.columns[0].name if table.columns else "",
                        default=1,
                    ),
                    source.row_id_for(
                        source.columns[0].name if source.columns else "",
                        default=1,
                    ),
                    label="reads from" if table.is_view else "materializes from",
                    dashed=True,
                )
            )
    return edges


# ---------------------------------------------------------------------------
# Build the full document
# ---------------------------------------------------------------------------


def generate() -> str:
    tables = load_schema()
    max_x = max(t.x + t.width for t in tables) + 100
    max_y = max(t.y + t.height for t in tables) + 100
    lines: list[str] = []

    lines.append('<mxfile host="Cursor">')
    lines.append('    <diagram id="erd-avian-biosurveillance" name="Data Model">')
    lines.append(
        f'        <mxGraphModel dx="1200" dy="900" grid="1" gridSize="10" '
        f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
        f'pageScale="1" pageWidth="{max_x}" pageHeight="{max_y}" math="0" '
        f'shadow="0">'
    )
    lines.append("            <root>")
    lines.append('                <mxCell id="0"/>')
    lines.append('                <mxCell id="1" parent="0"/>')

    title_w = max_x - 100
    lines.append(
        '                <mxCell id="title" '
        'value="Avian Biosurveillance — Data Model (SQL-generated)" '
        'style="text;html=1;fontSize=22;fontStyle=1;align=center;'
        'verticalAlign=middle;whiteSpace=wrap;rounded=0;fillColor=none;'
        'strokeColor=none;fontColor=#1F3864;" parent="1" vertex="1">'
    )
    lines.append(
        f'                    <mxGeometry x="50" y="30" width="{title_w}" '
        f'height="40" as="geometry"/>'
    )
    lines.append("                </mxCell>")

    by_name = {table.name: table for table in tables}
    rendered_groups: dict[str, list[Table]] = {}
    for table in tables:
        rendered_groups.setdefault(_layout_group(table, by_name), []).append(table)

    for group_index, (group, members) in enumerate(
        sorted(
            rendered_groups.items(),
            key=lambda item: min(member.x for member in item[1]),
        )
    ):
        gid = f"grp-{_prefix_for(group)}"
        label = _source_label(group)
        gx = min(member.x for member in members)
        gy = min(member.y for member in members) - 22
        gw = max(member.x + member.width for member in members) - gx
        color = PALETTE[group_index % len(PALETTE)][1]
        lines.append(
            f'                <mxCell id="{gid}" value="{label}" '
            'style="text;html=1;fontSize=11;fontStyle=1;align=left;'
            'verticalAlign=bottom;whiteSpace=nowrap;fillColor=none;'
            f'strokeColor=none;fontColor={color};spacingLeft=4;" '
            'parent="1" vertex="1">'
        )
        lines.append(
            f'                    <mxGeometry x="{gx}" y="{gy}" width="{gw}" '
            'height="18" as="geometry"/>'
        )
        lines.append("                </mxCell>")

    for tbl in tables:
        lines.extend(build_table_xml(tbl))

    edge_groups = (
        _fk_edges(tables)
        + _source_edges(tables)
    )
    for edge in edge_groups:
        lines.extend(edge)

    lines.append("            </root>")
    lines.append("        </mxGraphModel>")
    lines.append("    </diagram>")
    lines.append("</mxfile>")
    return "\n".join(lines)


if __name__ == "__main__":
    schema = load_schema()
    content = generate()
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"Generated {OUTPUT_PATH}  ({len(content):,} chars)")
    for rendered_table in schema:
        print(
            f"  {rendered_table.name:40s}  cols={len(rendered_table.columns):3d}  "
            f"pos=({rendered_table.x},{rendered_table.y})  "
            f"size={rendered_table.width}x{rendered_table.height}"
        )
