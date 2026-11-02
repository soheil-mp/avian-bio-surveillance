"""Orchestration Diagram Generator.

Generates a `draw.io <https://draw.io>`_ XML diagram visualising the
Dagster orchestration architecture:

- Scheduled deployments (``serve.py``)
- Daily pipeline flow with three phases
- Manual BirdWeather repair flow
- Target database tables
- Colour-coded legend

Usage::

    python -m avian_biosurveillance.orchestration.generate_drawio

Output:  ``orchestration_diagram.drawio`` (same directory).
"""

from __future__ import annotations

import html
import itertools
import logging
from pathlib import Path


# ── XML helpers ─────────────────────────────────────────────────────

# IDs 0 and 1 are reserved for root cells.
_id_counter = [itertools.count(start=2)]


def _next_id() -> int:
    """Return a monotonically increasing cell ID."""
    return next(_id_counter[0])


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


# ── Styles ──────────────────────────────────────────────────────────

TITLE = (
    "rounded=0;whiteSpace=wrap;html=1;fillColor=#1B1B2F;"
    "fontColor=#EEEEEE;fontSize=18;fontStyle=1;strokeColor=none;"
    "shadow=1;align=center;"
)

DEPLOYMENT = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#283593;"
    "fontColor=#FFFFFF;fontSize=11;fontStyle=1;strokeColor=#1a237e;"
    "shadow=1;arcSize=12;"
)

FLOW_CONTAINER = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#e8eaf6;"
    "strokeColor=#5c6bc0;fontStyle=1;fontSize=12;fontColor=#283593;"
    "shadow=1;arcSize=8;container=1;collapsible=0;verticalAlign=top;"
)

PHASE = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#fafafa;"
    "strokeColor=#bdbdbd;dashed=1;dashPattern=5 5;"
    "verticalAlign=top;fontStyle=1;fontSize=10;fontColor=#616161;"
    "container=1;collapsible=0;"
)

TASK_INGEST = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#c8e6c9;"
    "strokeColor=#2e7d32;fontColor=#1b5e20;fontSize=10;"
    "arcSize=10;shadow=0;"
)

TASK_SOVON = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#e8f5e9;"
    "strokeColor=#388e3c;fontColor=#1b5e20;fontSize=10;"
    "arcSize=10;shadow=0;dashed=1;dashPattern=3 3;"
)

TASK_POSTPROCESS = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#fff3e0;"
    "strokeColor=#ef6c00;fontColor=#e65100;fontSize=10;"
    "arcSize=10;shadow=0;"
)

TASK_AUDIT = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#ffebee;"
    "strokeColor=#c62828;fontColor=#b71c1c;fontSize=10;"
    "arcSize=10;shadow=0;"
)

TASK_BACKFILL = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#e3f2fd;"
    "strokeColor=#1565c0;fontColor=#0d47a1;fontSize=10;"
    "arcSize=10;shadow=0;"
)

DB_TABLE = (
    "shape=cylinder3;whiteSpace=wrap;html=1;fillColor=#eceff1;"
    "strokeColor=#546e7a;fontColor=#37474f;fontSize=9;"
    "boundedLbl=1;size=8;shadow=0;"
)

EDGE_SOLID = (
    "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;"
    "jettySize=auto;html=1;strokeColor=#616161;"
    "strokeWidth=1.5;endArrow=blockThin;endFill=1;"
)

EDGE_DASHED = (
    "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;"
    "jettySize=auto;html=1;strokeColor=#9e9e9e;"
    "strokeWidth=1;dashed=1;dashPattern=5 5;"
    "endArrow=blockThin;endFill=1;"
)

EDGE_PHASE = (
    "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;"
    "jettySize=auto;html=1;strokeColor=#5c6bc0;"
    "strokeWidth=2;endArrow=blockThin;endFill=1;"
)

LEGEND = (
    "rounded=1;whiteSpace=wrap;html=1;fillColor=#FAFAFA;"
    "strokeColor=#BDBDBD;fontSize=10;verticalAlign=top;fontStyle=1;"
    "fontColor=#424242;shadow=1;"
)


# ── Cell builders ───────────────────────────────────────────────────


def _cell(
    cid: int,
    value: str,
    style: str,
    x: int,
    y: int,
    w: int,
    h: int,
    parent: int = 1,
) -> str:
    return (
        f'        <mxCell id="{cid}" value="{_esc(value)}" '
        f'style="{style}" vertex="1" parent="{parent}">\n'
        f'          <mxGeometry x="{x}" y="{y}" '
        f'width="{w}" height="{h}" as="geometry"/>\n'
        f"        </mxCell>\n"
    )


def _container(
    cid: int,
    value: str,
    style: str,
    x: int,
    y: int,
    w: int,
    h: int,
    parent: int = 1,
) -> str:
    return (
        f'        <mxCell id="{cid}" value="{_esc(value)}" '
        f'style="{style}" vertex="1" parent="{parent}">\n'
        f'          <mxGeometry x="{x}" y="{y}" '
        f'width="{w}" height="{h}" as="geometry"/>\n'
        f"        </mxCell>\n"
    )


def _edge(
    cid: int,
    source: int,
    target: int,
    style: str = EDGE_SOLID,
    label: str = "",
    parent: int = 1,
) -> str:
    val = f'value="{_esc(label)}" ' if label else ""
    return (
        f'        <mxCell id="{cid}" {val}'
        f'style="{style}" edge="1" '
        f'source="{source}" target="{target}" parent="{parent}">\n'
        f'          <mxGeometry relative="1" as="geometry"/>\n'
        f"        </mxCell>\n"
    )


# ── Main generator ──────────────────────────────────────────────────


def generate() -> str:
    """Build the full draw.io XML string.

    Returns:
        Complete XML document string ready to be written to a
        ``.drawio`` file.
    """
    _id_counter[0] = itertools.count(start=2)

    cells: list[str] = []

    # ── Canvas dimensions ───────────────────────────────────────────
    CW = 1440  # canvas width
    # Heights are determined by layout below

    # ── Title ───────────────────────────────────────────────────────
    title_id = _next_id()
    cells.append(
        _cell(
            title_id,
            "Avian Biosurveillance — Orchestration Architecture",
            TITLE,
            0,
            0,
            CW,
            50,
        )
    )

    # ── Scheduled Deployments ───────────────────────────────────────
    dep_daily_id = _next_id()
    cells.append(
        _cell(
            dep_daily_id,
            "<b>legacy daily-pipeline</b><br/><i>CLI/notebook compatibility only</i>",
            DEPLOYMENT,
            180,
            70,
            300,
            60,
        )
    )

    dep_backfill_id = _next_id()
    cells.append(
        _cell(
            dep_backfill_id,
            "<b>birdweather-repair</b><br/><i>manual / ad hoc</i>",
            DEPLOYMENT,
            960,
            70,
            300,
            60,
        )
    )

    # ── Daily Pipeline Flow ─────────────────────────────────────────
    DP_X, DP_Y, DP_W, DP_H = 30, 170, 880, 650
    dp_flow_id = _next_id()
    cells.append(
        _container(
            dp_flow_id,
            "legacy daily-pipeline flow",
            FLOW_CONTAINER,
            DP_X,
            DP_Y,
            DP_W,
            DP_H,
        )
    )

    # Phase 1 — Parallel Ingestion
    P1_X, P1_Y, P1_W, P1_H = 20, 40, 840, 150
    p1_id = _next_id()
    cells.append(
        _container(
            p1_id,
            "Phase 1 — Parallel Ingestion (concurrent task.submit)",
            PHASE,
            P1_X,
            P1_Y,
            P1_W,
            P1_H,
            parent=dp_flow_id,
        )
    )

    # 7 ingest tasks inside Phase 1
    TASK_W, TASK_H = 105, 50
    TASK_GAP = 10
    task_y = 35  # relative to phase container
    task_x_start = 20

    ingest_tasks = [
        ("birdweather\n(24 h)", TASK_INGEST),
        ("knmi\n(26 h)", TASK_INGEST),
        ("seismic\n(30 days)", TASK_INGEST),
        ("ebird\n(14 days)", TASK_INGEST),
        ("movebank\n(7 days)", TASK_INGEST),
        ("earth-engine\n(3 days)", TASK_INGEST),
        ("sovon-download\n(charts)", TASK_INGEST),
    ]

    ingest_ids = []
    for i, (label, style) in enumerate(ingest_tasks):
        tid = _next_id()
        tx = task_x_start + i * (TASK_W + TASK_GAP)
        cells.append(_cell(tid, label, style, tx, task_y, TASK_W, TASK_H, parent=p1_id))
        ingest_ids.append(tid)

    sovon_download_id = ingest_ids[-1]

    # Sovon Extract (between Phase 1 and Phase 2, depends on download)
    sovon_extract_id = _next_id()
    cells.append(
        _cell(
            sovon_extract_id,
            "sovon-extract\n(Gemini Vision)",
            TASK_SOVON,
            610,
            210,
            150,
            45,
            parent=dp_flow_id,
        )
    )

    # Phase 2 — Post-Processing
    P2_X, P2_Y, P2_W, P2_H = 20, 280, 840, 110
    p2_id = _next_id()
    cells.append(
        _container(
            p2_id,
            "Phase 2 — Post-Processing (sequential, if target=database)",
            PHASE,
            P2_X,
            P2_Y,
            P2_W,
            P2_H,
            parent=dp_flow_id,
        )
    )

    dedup_id = _next_id()
    cells.append(
        _cell(
            dedup_id,
            "birdweather-dedup\n(species+station+time)",
            TASK_POSTPROCESS,
            160,
            40,
            210,
            50,
            parent=p2_id,
        )
    )

    station_refresh_id = _next_id()
    cells.append(
        _cell(
            station_refresh_id,
            "station-summary-refresh\n(PL/pgSQL aggregate)",
            TASK_POSTPROCESS,
            460,
            40,
            220,
            50,
            parent=p2_id,
        )
    )

    # Phase 3 — Quality Audit
    P3_X, P3_Y, P3_W, P3_H = 20, 415, 840, 100
    p3_id = _next_id()
    cells.append(
        _container(
            p3_id,
            "Phase 3 — Quality Audit (if target=database)",
            PHASE,
            P3_X,
            P3_Y,
            P3_W,
            P3_H,
            parent=dp_flow_id,
        )
    )

    quality_id = _next_id()
    cells.append(
        _cell(
            quality_id,
            "quality-audit\n(ISO 25012, 6 dimensions)",
            TASK_AUDIT,
            310,
            35,
            210,
            50,
            parent=p3_id,
        )
    )

    # Ad-hoc flows note
    adhoc_id = _next_id()
    cells.append(
        _cell(
            adhoc_id,
            "<i>Ad-hoc: each source flow remains importable "
            "for manual / one-off runs</i>",
            "text;html=1;align=center;verticalAlign=middle;"
            "whiteSpace=wrap;rounded=0;fontSize=9;fontColor=#757575;",
            120,
            540,
            640,
            30,
            parent=dp_flow_id,
        )
    )

    # ── BirdWeather Repair Flow ─────────────────────────────────────
    BF_X, BF_Y, BF_W, BF_H = 940, 170, 340, 290
    bf_flow_id = _next_id()
    cells.append(
        _container(
            bf_flow_id,
            "birdweather-repair flow",
            FLOW_CONTAINER,
            BF_X,
            BF_Y,
            BF_W,
            BF_H,
        )
    )

    bf_detect_id = _next_id()
    cells.append(
        _cell(
            bf_detect_id,
            "find-missing-dates\n(DB gap scan, repair window)",
            TASK_BACKFILL,
            30,
            45,
            280,
            50,
            parent=bf_flow_id,
        )
    )

    bf_fetch_id = _next_id()
    cells.append(
        _cell(
            bf_fetch_id,
            "fetch per missing date\n(sequential repair)",
            TASK_BACKFILL,
            30,
            115,
            280,
            50,
            parent=bf_flow_id,
        )
    )

    bf_dedup_id = _next_id()
    cells.append(
        _cell(
            bf_dedup_id,
            "<i>concurrency_limit=1<br/>queues behind previous run</i>",
            "text;html=1;align=center;verticalAlign=middle;"
            "whiteSpace=wrap;rounded=0;fontSize=9;fontColor=#757575;",
            30,
            200,
            280,
            40,
            parent=bf_flow_id,
        )
    )

    # ── Database Tables ─────────────────────────────────────────────
    DB_Y = 870
    db_tables = [
        "birdweather_tbl",
        "knmi_tbl",
        "seismic_tbl",
        "ebird_tbl",
        "movebank_tbl",
        "google_earth\n_engine_tbl",
        "sovon_tbl",
        "recording_\nstations_tbl",
        "data_quality\n_tbl",
    ]
    DB_W, DB_H = 130, 55
    DB_GAP = 12
    total_db_w = len(db_tables) * DB_W + (len(db_tables) - 1) * DB_GAP
    db_x_start = (CW - total_db_w) // 2

    db_label_id = _next_id()
    cells.append(
        _cell(
            db_label_id,
            "<b>TimescaleDB / PostgreSQL 16</b>",
            "text;html=1;align=center;verticalAlign=middle;"
            "whiteSpace=wrap;rounded=0;fontSize=11;fontColor=#546e7a;"
            "fontStyle=1;",
            0,
            DB_Y - 30,
            CW,
            25,
        )
    )

    db_ids: list[int] = []
    for i, tbl in enumerate(db_tables):
        did = _next_id()
        dx = db_x_start + i * (DB_W + DB_GAP)
        cells.append(_cell(did, tbl, DB_TABLE, dx, DB_Y, DB_W, DB_H))
        db_ids.append(did)

    # ── Legend ──────────────────────────────────────────────────────
    legend_id = _next_id()
    legend_html = (
        "<b>Legend</b><hr/>"
        '<table style="font-size:9px;border-collapse:collapse;" cellpadding="3">'
        '<tr><td style="background:#283593;color:#fff;border-radius:4px;"'
        ">&nbsp;&nbsp;&nbsp;</td>"
        "<td>Scheduled deployment (cron)</td></tr>"
        '<tr><td style="background:#c8e6c9;border:1px solid #2e7d32;'
        'border-radius:4px;">&nbsp;&nbsp;&nbsp;</td>'
        "<td>Ingestion task</td></tr>"
        '<tr><td style="background:#fff3e0;border:1px solid #ef6c00;'
        'border-radius:4px;">&nbsp;&nbsp;&nbsp;</td>'
        "<td>Post-processing task</td></tr>"
        '<tr><td style="background:#ffebee;border:1px solid #c62828;'
        'border-radius:4px;">&nbsp;&nbsp;&nbsp;</td>'
        "<td>Quality audit task</td></tr>"
        '<tr><td style="background:#e3f2fd;border:1px solid #1565c0;'
        'border-radius:4px;">&nbsp;&nbsp;&nbsp;</td>'
        "<td>Backfill task</td></tr>"
        '<tr><td style="background:#e8eaf6;border:1px solid #5c6bc0;'
        'border-radius:4px;">&nbsp;&nbsp;&nbsp;</td>'
        "<td>Flow container</td></tr>"
        '<tr><td style="background:#eceff1;border:1px solid #546e7a;'
        'border-radius:4px;">&nbsp;&nbsp;&nbsp;</td>'
        "<td>Database table</td></tr>"
        "</table>"
    )
    cells.append(_cell(legend_id, legend_html, LEGEND, 940, 510, 340, 230))

    # ── Edges ───────────────────────────────────────────────────────

    # Deployment → Flow triggers
    cells.append(_edge(_next_id(), dep_daily_id, dp_flow_id, EDGE_SOLID, "triggers"))
    cells.append(_edge(_next_id(), dep_backfill_id, bf_flow_id, EDGE_SOLID, "triggers"))

    # Phase 1 → sovon extract (conditional)
    cells.append(
        _edge(
            _next_id(),
            sovon_download_id,
            sovon_extract_id,
            EDGE_DASHED,
            "if succeeded",
            parent=dp_flow_id,
        )
    )

    # Phase flow: Phase 1 → Phase 2 → Phase 3
    cells.append(_edge(_next_id(), p1_id, p2_id, EDGE_PHASE, "", parent=dp_flow_id))
    cells.append(_edge(_next_id(), p2_id, p3_id, EDGE_PHASE, "", parent=dp_flow_id))

    # Sovon extract also feeds into Phase 2 ordering (visual only)
    cells.append(
        _edge(_next_id(), sovon_extract_id, p2_id, EDGE_DASHED, "", parent=dp_flow_id)
    )

    # Backfill internal: detect → fetch
    cells.append(
        _edge(_next_id(), bf_detect_id, bf_fetch_id, EDGE_SOLID, "", parent=bf_flow_id)
    )

    # Daily pipeline → database (dashed, write)
    cells.append(_edge(_next_id(), dp_flow_id, db_ids[0], EDGE_DASHED, "writes"))
    # Backfill → birdweather_tbl specifically
    cells.append(_edge(_next_id(), bf_flow_id, db_ids[0], EDGE_DASHED, "writes"))

    # ── Assemble XML ────────────────────────────────────────────────
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<mxfile host="draw.io" type="device">\n',
        '  <diagram name="Orchestration" id="orch-1">\n',
        f'    <mxGraphModel dx="{CW + 40}" dy="1000" grid="1" '
        'gridSize="10" guides="1" tooltips="1" connect="1" '
        'arrows="1" fold="1" page="1" pageScale="1" '
        f'pageWidth="{CW + 40}" pageHeight="1000" math="0" shadow="0">\n',
        "      <root>\n",
        '        <mxCell id="0"/>\n',
        '        <mxCell id="1" parent="0"/>\n',
    ]
    xml_parts.extend(cells)
    xml_parts.append("      </root>\n")
    xml_parts.append("    </mxGraphModel>\n")
    xml_parts.append("  </diagram>\n")
    xml_parts.append("</mxfile>\n")

    return "".join(xml_parts)


# ── Entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    out = Path(__file__).with_name("orchestration_diagram.drawio")
    xml = generate()
    out.write_text(xml, encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info("Wrote %s  (%s bytes)", out, f"{len(xml):,}")
