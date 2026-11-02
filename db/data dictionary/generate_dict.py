"""Generate ``db/dict/data_dictionary.md`` from the SQL schema files.

The SQL files under ``db/baseline/``, ``db/caggs/``, and ``db/views/`` are the
source of truth for all structural metadata (column names, types, nullability,
defaults, PK/FK constraints).  Column and table descriptions are drawn from
``COMMENT ON TABLE / COLUMN`` statements in those same files; inline ``--``
comments on column definition lines serve as fallback descriptions.

Run via ``make dict`` or directly: ``python db/dict/generate_dict.py``.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared parsing helpers imported from the ERD generator
# ---------------------------------------------------------------------------

_ERD_DIR = Path(__file__).resolve().parents[1] / "erd"
sys.path.insert(0, str(_ERD_DIR))

from generate_erd import (  # noqa: E402  (path manipulation required)
    Column,
    Table,
    _find_matching_paren,
    load_schema,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_ROOT = REPO_ROOT / "db"
OUTPUT_PATH = Path(__file__).resolve().with_name("data_dictionary.md")
EXTERNAL_DATASETS_PATH = Path(__file__).with_name("_external_datasets.md")

# ---------------------------------------------------------------------------
# Additional SQL parsing (not needed by the ERD generator)
# ---------------------------------------------------------------------------


_SQL_LITERAL_SEQUENCE = (
    r"((?:\$\$.*?\$\$|'(?:[^']|'')*')(?:\s+(?:\$\$.*?\$\$|'(?:[^']|'')*'))*)"
)


def _decode_comment_literal_sequence(literal_expr: str) -> str:
    """Decode one or more adjacent SQL string literals into plain text."""
    parts: list[str] = []
    token_re = re.compile(r"\$\$(.*?)\$\$|'((?:[^']|'')*)'", re.S)
    for match in token_re.finditer(literal_expr):
        text = (match.group(1) or match.group(2) or "").replace("''", "'")
        parts.append(text)
    return "".join(parts).strip()


def _parse_comments(sql: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(object_comments, col_comments)`` parsed from COMMENT ON statements.

    Table, view, and materialized-view comments are all stored in the first
    mapping, keyed by object name. Both single-quoted and dollar-quoted string
    literals are handled.
    Keys for col_comments are ``"table_name.col_name"``.
    """
    object_re = re.compile(
        r"\bCOMMENT\s+ON\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW)\s+([a-zA-Z_]\w*)\s+IS\s+"
        + _SQL_LITERAL_SEQUENCE,
        re.I | re.S,
    )
    col_re = re.compile(
        r"\bCOMMENT\s+ON\s+COLUMN\s+([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s+IS\s+"
        + _SQL_LITERAL_SEQUENCE,
        re.I | re.S,
    )

    table_comments: dict[str, str] = {}
    for m in object_re.finditer(sql):
        text = _decode_comment_literal_sequence(m.group(2))
        table_comments[m.group(1)] = text

    col_comments: dict[str, str] = {}
    for m in col_re.finditer(sql):
        text = _decode_comment_literal_sequence(m.group(3))
        col_comments[f"{m.group(1)}.{m.group(2)}"] = text

    return table_comments, col_comments


def _parse_header_info(raw_sql: str, table_name: str) -> dict[str, str]:
    """Extract structured key-value pairs from the ``--`` header block above a DDL object.

    Parses lines of the form ``-- Key  : value`` immediately before the
    ``CREATE TABLE / VIEW`` statement and returns them keyed by lowercase name.
    """
    pattern = re.compile(
        r"\bCREATE\s+(?:TABLE|MATERIALIZED\s+VIEW|(?:OR\s+REPLACE\s+)?VIEW)"
        r"\s+(?:IF\s+NOT\s+EXISTS\s+)?" + re.escape(table_name) + r"\b",
        re.I,
    )
    match = pattern.search(raw_sql)
    if not match:
        return {}

    lines = raw_sql[: match.start()].splitlines()
    result: dict[str, str] = {}
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.startswith("--"):
            if stripped:
                break
            continue
        kv = re.match(r"^--\s+([\w][\w\s]*):\s*(.+)$", stripped)
        if kv:
            key = kv.group(1).strip().lower().replace(" ", "_")
            result.setdefault(key, kv.group(2).strip())
    return result


def _parse_inline_col_comments(raw_sql: str, table_name: str) -> dict[str, str]:
    """Extract trailing ``-- comment`` from column definition lines inside CREATE TABLE."""
    pattern = re.compile(
        r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + re.escape(table_name) + r"\s*\(",
        re.I,
    )
    match = pattern.search(raw_sql)
    if not match:
        return {}

    open_idx = raw_sql.find("(", match.end() - 1)
    try:
        close_idx = _find_matching_paren(raw_sql, open_idx)
    except ValueError:
        return {}

    body = raw_sql[open_idx + 1 : close_idx]
    result: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if re.match(r"^(PRIMARY|FOREIGN|CONSTRAINT|UNIQUE|CHECK|EXCLUDE)\b", stripped, re.I):
            continue
        comment_match = re.search(r"--\s*(.+)$", line)
        if not comment_match:
            continue
        col_match = re.match(r'\s+"?([a-zA-Z_]\w*)"?\s+', line)
        if col_match:
            result[col_match.group(1)] = comment_match.group(1).strip()
    return result


# ---------------------------------------------------------------------------
# Human-readable section titles
# ---------------------------------------------------------------------------

_TITLE_OVERRIDES: dict[str, str] = {
    # Baseline tables
    "birdweather_tbl": "BirdWeather",
    "birdweather_stations_tbl": "BirdWeather Stations",
    "knmi_tbl": "KNMI Weather",
    "sovon_tbl": "Sovon Mortality",
    "ebird_tbl": "eBird",
    "movebank_tbl": "Movebank",
    "google_earth_engine_tbl": "Google Earth Engine",
    "grid_cells_tbl": "Canonical Grid Cells",
    "cds_drought_tbl": "CDS Drought Grid",
    "seismic_tbl": "KNMI Seismic",
    "data_quality_tbl": "Data Quality Audit",
    "species_tbl": "Species Reference",
    "movebank_tracks_tbl": "Movebank Tracks",
    # Continuous aggregates
    "birdweather_daily_cagg_tbl": "BirdWeather Daily",
    "knmi_daily_cagg_tbl": "KNMI Daily",
    # Views
    "google_earth_engine_grid_tbl": "GEE Grid",
    "cds_drought_v": "CDS Drought",
}


def _table_group(table: Table) -> int:
    """Sort key: 0 = base table, 1 = cagg, 2 = view."""
    # generate_erd.py sets is_cagg=True on both caggs AND views;
    # views are distinguished by is_view=True.
    if table.is_view:
        return 2
    if table.is_cagg:
        return 1
    return 0


def _table_title(table: Table) -> str:
    if table.name in _TITLE_OVERRIDES:
        title = _TITLE_OVERRIDES[table.name]
    else:
        name = re.sub(r"_(cagg_tbl|tbl|v)$", "", table.name)
        title = name.replace("_", " ").title()

    # generate_erd.py marks views with is_view=True AND is_cagg=True;
    # caggs (materialized views) have is_cagg=True but is_view=False.
    if table.is_view:
        return f"{title} (View)"
    if table.is_cagg:
        return f"{title} (Continuous Aggregate)"
    return title


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _default_str(col: Column) -> str:
    m = re.search(r"\bDEFAULT\s+(.+)$", col.extra, re.I)
    return f"`{m.group(1).strip()}`" if m else "`—`"


def _render_table_section(
    table: Table,
    table_comments: dict[str, str],
    col_comments: dict[str, str],
    inline_col_comments: dict[str, str],
    header_info: dict[str, str],
) -> list[str]:
    lines: list[str] = []
    lines.append(f"## {_table_title(table)}")
    lines.append("")

    # Source object line
    if table.is_view:
        obj_type = "view"
    elif table.is_cagg:
        obj_type = "continuous aggregate"
    elif table.is_hypertable:
        obj_type = f"TimescaleDB {table.subtitle}"
    else:
        obj_type = "database table"
    lines.append(f"- **Source object:** `{table.name}` ({obj_type}; `{table.source_path}`)")

    grain = header_info.get("grain", "")
    if grain:
        lines.append(f"- **Grain:** {grain}")

    pk_cols = [col.name for col in table.columns if col.pk]
    if pk_cols:
        pk_str = (
            "(" + ", ".join(f"`{c}`" for c in pk_cols) + ")"
            if len(pk_cols) > 1
            else f"`{pk_cols[0]}`"
        )
        lines.append(f"- **Primary key:** {pk_str}")

    fk_entries = [(col.name, col.fk_ref) for col in table.columns if col.fk and col.fk_ref]
    if fk_entries:
        lines.append("- **Foreign keys:**")
        for col_name, fk_ref in fk_entries:
            lines.append(f"  - `{col_name}` → `{fk_ref}`")

    if table.source_refs:
        refs = ", ".join(f"`{r}`" for r in table.source_refs)
        verb = "reads from" if table.is_view else "materializes from"
        lines.append(f"- **Sources:** {verb} {refs}")

    overview = table_comments.get(table.name, "") or header_info.get("purpose", "")
    if overview:
        lines.append(f"- **Overview:** {overview}")

    lines.append("")
    lines.append("| Column | Type | Nullable | Default | Constraints / notes | Description |")
    lines.append("|---|---|---|---|---|---|")

    for col in table.columns:
        nullable = "No" if col.not_null else "Yes"
        default = _default_str(col)

        notes_parts: list[str] = []
        if col.pk:
            notes_parts.append("PK")
        if col.partition_key:
            notes_parts.append("partition key")
        if col.fk and col.fk_ref:
            notes_parts.append(f"FK → `{col.fk_ref}`")
        if "GENERATED ALWAYS AS IDENTITY" in col.extra:
            notes_parts.append("identity column")

        key = f"{table.name}.{col.name}"
        desc = col_comments.get(key, "") or inline_col_comments.get(col.name, "")
        notes = "; ".join(notes_parts) if notes_parts else "—"

        lines.append(
            f"| `{col.name}` | `{col.data_type}` | {nullable} | {default} | {notes} | {desc} |"
        )

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------

_EXT_HEADINGS = [
    "AudioDB (MinIO Object Storage)",
]


def _collect_annotations(
    all_paths: list[Path],
    table_names: set[str],
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    table_comments: dict[str, str] = {}
    col_comments: dict[str, str] = {}
    inline_col_comments: dict[str, dict[str, str]] = {}
    header_infos: dict[str, dict[str, str]] = {}

    for path in all_paths:
        raw = path.read_text(encoding="utf-8")
        tc, cc = _parse_comments(raw)
        table_comments.update(tc)
        col_comments.update(cc)
        for tbl in table_names:
            if tbl not in header_infos:
                hi = _parse_header_info(raw, tbl)
                if hi:
                    header_infos[tbl] = hi
            if tbl not in inline_col_comments:
                ic = _parse_inline_col_comments(raw, tbl)
                if ic:
                    inline_col_comments[tbl] = ic

    return table_comments, col_comments, inline_col_comments, header_infos


def build_document(tables: list[Table]) -> str:
    table_names = {t.name for t in tables}
    all_paths = (
        sorted((DB_ROOT / "baseline").glob("*.sql"))
        + sorted((DB_ROOT / "caggs").glob("*.sql"))
        + sorted((DB_ROOT / "views").glob("*.sql"))
        + sorted((DB_ROOT / "migrations").glob("*.sql"))
    )
    table_comments, col_comments, inline_col_comments, header_infos = _collect_annotations(
        all_paths, table_names
    )

    lines: list[str] = [
        "# Data Dictionary",
        "",
        "<!-- AUTO-GENERATED — do not edit directly. "
        f"Run `make dict` in `db/` to regenerate (last generated: {date.today().isoformat()}). -->",
        "<!-- Source: db/baseline/*.sql · db/caggs/*.sql · db/views/*.sql "
        "· db/dict/_external_datasets.md -->",
        "",
        "## Contents",
        "",
    ]

    for table in tables:
        title = _table_title(table)
        anchor = re.sub(r"[^a-z0-9-]", "", title.lower().replace(" ", "-"))
        lines.append(f"- [{title}](#{anchor})")

    for h in _EXT_HEADINGS:
        anchor = re.sub(r"[^a-z0-9-]", "", h.lower().replace(" ", "-"))
        lines.append(f"- [{h}](#{anchor})")

    lines.append("")

    for table in tables:
        lines.extend(
            _render_table_section(
                table,
                table_comments,
                col_comments,
                inline_col_comments.get(table.name, {}),
                header_infos.get(table.name, {}),
            )
        )

    body = "\n".join(lines)

    if EXTERNAL_DATASETS_PATH.exists():
        ext = EXTERNAL_DATASETS_PATH.read_text(encoding="utf-8")
        ext_body = re.sub(r"^<!--.*?-->\n\n?", "", ext, flags=re.S)
        body = body + "\n" + ext_body

    return body


if __name__ == "__main__":
    schema = load_schema()
    schema.sort(key=lambda t: (_table_group(t), t.source_order, t.object_order))

    content = build_document(schema)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"Generated {OUTPUT_PATH}  ({len(content):,} chars)")
    for tbl in schema:
        print(f"  {tbl.name:50s}  cols={len(tbl.columns):3d}")
