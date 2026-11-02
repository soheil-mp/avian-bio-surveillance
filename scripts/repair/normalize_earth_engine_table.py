#!/usr/bin/env python3
"""Normalize legacy rows in ``google_earth_engine_tbl``.

Fixes existing data after the Earth Engine ingestion audit:
- converts legacy ``cloud_cover_pct`` fractions in [0, 1] to real percentages,
- replaces placeholder label strings like ``'None'`` with SQL NULL semantics,
- rewrites legacy full-payload ``profile_data`` blobs to residual-only JSON,
- backfills ``profiler_mode`` / ``profiler_elapsed_s`` from embedded ``_meta``
  when legacy payloads happen to contain it.

Usage::

    python scripts/repair/normalize_earth_engine_table.py --dry-run
    python scripts/repair/normalize_earth_engine_table.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psycopg2.extras


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.db.database import get_pool
from avian_biosurveillance.db.models.earth_engine import (
    SECTION_COLUMN_GROUPS,
    _build_residual_profile_data,
    build_section_availability_from_row,
)


SECTION_COLUMNS = tuple(
    dict.fromkeys(
        column for columns in SECTION_COLUMN_GROUPS.values() for column in columns
    )
)

LEGACY_PROFILE_KEYS = {
    "station_id",
    "source",
    "lat",
    "lon",
    "date",
    "elevation",
    "land_cover",
    "vegetation",
    "surface_temperature",
    "climate",
    "air_quality",
    "cloud_properties",
    "surface_water",
    "forest",
    "nighttime_lights",
    "population",
    "human_modification",
    "fire",
    "soil",
    "evapotranspiration",
    "sea_surface_temperature",
}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _looks_like_legacy_payload(profile_data: dict[str, Any]) -> bool:
    return any(key in profile_data for key in LEGACY_PROFILE_KEYS)


def _normalized_label(value: Any) -> str | None:
    if value in {None, "", "None", "NULL", "null"}:
        return None
    return str(value)


def _iter_updates(rows: Iterable[dict[str, Any]]) -> list[tuple[Any, ...]]:
    updates: list[tuple[Any, ...]] = []

    for row in rows:
        station_id = row["station_id"]
        observation_date = row["observation_date"]
        cloud_cover_pct = row["cloud_cover_pct"]
        dynamic_world_label = row["dynamic_world_label"]
        esa_worldcover_label = row["esa_worldcover_label"]
        modis_igbp_label = row["modis_igbp_label"]
        profile_data = row["profile_data"]
        section_availability = row["section_availability"]
        profiler_mode = row["profiler_mode"]
        profiler_elapsed_s = row["profiler_elapsed_s"]

        payload = _as_dict(profile_data)
        legacy_payload = _looks_like_legacy_payload(payload)
        normalized_profile = payload
        meta = payload.get("_meta") if legacy_payload else None

        normalized_cloud_cover = cloud_cover_pct
        if isinstance(cloud_cover_pct, (int, float)) and 0 <= cloud_cover_pct <= 1:
            normalized_cloud_cover = round(float(cloud_cover_pct) * 100, 2)

        normalized_dynamic_world = _normalized_label(dynamic_world_label)
        normalized_esa_worldcover = _normalized_label(esa_worldcover_label)
        normalized_modis_igbp = _normalized_label(modis_igbp_label)

        normalized_mode = profiler_mode or (
            meta.get("mode") if isinstance(meta, dict) else None
        )
        normalized_elapsed = profiler_elapsed_s
        if normalized_elapsed is None and isinstance(meta, dict):
            normalized_elapsed = meta.get("total_seconds")

        if legacy_payload:
            normalized_profile = json.loads(_build_residual_profile_data(payload))

        availability_row = {column: row.get(column) for column in SECTION_COLUMNS}
        availability_row["dynamic_world_label"] = normalized_dynamic_world
        availability_row["esa_worldcover_label"] = normalized_esa_worldcover
        availability_row["modis_igbp_label"] = normalized_modis_igbp
        availability_row["cloud_cover_pct"] = normalized_cloud_cover
        availability_row["profile_data"] = json.dumps(normalized_profile)
        normalized_section_availability = build_section_availability_from_row(
            availability_row
        )
        existing_section_availability = _as_dict(section_availability)

        changed = any(
            (
                normalized_cloud_cover != cloud_cover_pct,
                normalized_dynamic_world != dynamic_world_label,
                normalized_esa_worldcover != esa_worldcover_label,
                normalized_modis_igbp != modis_igbp_label,
                normalized_profile != payload,
                normalized_section_availability != existing_section_availability,
                normalized_mode != profiler_mode,
                normalized_elapsed != profiler_elapsed_s,
            )
        )
        if not changed:
            continue

        updates.append(
            (
                normalized_cloud_cover,
                normalized_dynamic_world,
                normalized_esa_worldcover,
                normalized_modis_igbp,
                psycopg2.extras.Json(normalized_profile),
                psycopg2.extras.Json(normalized_section_availability),
                normalized_mode,
                normalized_elapsed,
                station_id,
                observation_date,
            )
        )

    return updates


def run(*, dry_run: bool = False) -> int:
    pool = get_pool()
    selected_columns = [
        "station_id",
        "observation_date",
        *SECTION_COLUMNS,
        "profile_data",
        "section_availability",
        "profiler_mode",
        "profiler_elapsed_s",
    ]
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {', '.join(selected_columns)} FROM google_earth_engine_tbl"
        )
        rows = cur.fetchall()

    updates = _iter_updates(rows)
    if dry_run or not updates:
        print(
            f"normalize_earth_engine_table: {len(updates)} row(s) "
            f"{'would be updated' if dry_run else 'need no changes'}"
        )
        return len(updates)

    with pool.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            UPDATE google_earth_engine_tbl
            SET cloud_cover_pct = %s,
                dynamic_world_label = %s,
                esa_worldcover_label = %s,
                modis_igbp_label = %s,
                profile_data = %s,
                section_availability = %s,
                profiler_mode = %s,
                profiler_elapsed_s = %s
            WHERE station_id = %s
              AND observation_date = %s
            """,
            updates,
            page_size=500,
        )

    print(f"normalize_earth_engine_table: updated {len(updates)} row(s)")
    return len(updates)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize legacy google_earth_engine_tbl rows."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would change without updating the database.",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
