"""Data-quality audit for the Avian Biosurveillance platform.

Evaluates six ISO 25012 / DAMA DMBOK quality dimensions — Completeness,
Timeliness, Accuracy, Consistency, Uniqueness, Validity — against the live
database tables (``birdweather_tbl``, ``knmi_tbl``,
``sovon_tbl``, ``ebird_tbl``, ``movebank_tbl``,
``google_earth_engine_grid_tbl``,
``seismic_tbl``), then exports CSV reports:

    data/quality/data_quality_summary.csv   — per-table dimension scores (0–100)
    data/quality/data_quality_details.csv   — individual metric results
    data/quality/data_quality_issues.csv    — FAIL + WARN items only
    data/quality/data_quality_stations.csv  — per-station quality profile

Metrics that are enforced by database constraints (CHECK, PK) or that
measure geographic relevance rather than data quality are reported as
INFO-only and do not affect dimension scores.

Per-station trust and operational-health scores are computed by
``refresh_station_summary()`` in SQL from recent coverage, gap burden, volume
floor, classifier confidence quality, stability, metadata integrity, duplicate
pressure, and freshness — see ``birdweather_stations_tbl``.

Programmatic usage::

    from avian_biosurveillance.data.quality import main
    exit_code = main()

CLI usage::

    python scripts/ops/data_quality.py
    python scripts/ops/data_quality.py --output data/quality -v
    python scripts/ops/data_quality.py --fail-under 70
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.config.species import (
    load_registry,
    registry as _default_registry,
)
from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.models.movebank import MovebankEvent

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer from the environment, falling back to *default*."""
    import os

    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %d.", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Netherlands bounding box (WGS 84)
NL_LAT_MIN, NL_LAT_MAX = 50.75, 53.70
NL_LON_MIN, NL_LON_MAX = 3.33, 7.22

# Backward-compatible alias — callers that imported load_target_species can
# still do so, but the canonical entry-point is avian_biosurveillance.config.species.

def load_target_species(config_path: Path | None = None) -> list[str]:
    """Load target species English names from the shared registry."""
    return load_registry(config_path).english_names


# Module-level reference — populated at import with defaults, overridden in run_audit().
TARGET_SPECIES: list[str] = list(_default_registry.english_names)

TABLES = [
    "birdweather_tbl",
    "knmi_tbl",
    "sovon_tbl",
    "ebird_tbl",
    "movebank_tbl",
    "google_earth_engine_grid_tbl",
    "seismic_tbl",
]

# Expected columns per table (from db/baseline/*.sql) for schema-drift check.
# NOTE: knmi_daily_summary removed in v6 — replaced by knmi_daily_cagg_tbl.
_EXPECTED_COLUMNS: dict[str, set[str]] = {
    "birdweather_tbl": {
        # Identity & timestamps
        "detection_id", "detected_at", "observation_date",
        # Detection metrics
        "confidence", "score", "probability", "certainty", "eclipse",
        # FK to species_tbl
        "species_id",
        # Logical FK to birdweather_stations_tbl (also supplies coordinates)
        "station_id",
        # Soundscape / audio (per-clip duration is derived as
        # ``audio_end_time_s - audio_start_time_s`` and not stored)
        "soundscape_id", "audio_url", "audio_filename",
        "audio_filesize_bytes",
        "audio_start_time_s", "audio_end_time_s", "soundscape_at",
        # AST background-sound classification (single JSONB envelope)
        "background_sound_clf",
        # Feature-engineering outputs
        "birdnet_embedding", "perch_v2_embedding", "feature_artifacts",
        # Processed-clip availability flag
        "has_processed_clip",
        # Per-detection enrichment payloads
        "station_weather", "station_air_pollution", "station_sensors",
    },
    "knmi_tbl": {
        "observed_at", "observation_date", "station_id", "station_name",
        "station_lat", "station_lon", "station_elevation_m",
        "temperature_c", "relative_humidity_pct", "wind_speed_ms",
        "wind_direction_deg", "pressure_hpa", "precipitation_mm",
        "solar_radiation_wm2", "cloud_cover_okta",
    },
    "sovon_tbl": {
        "mortality_id", "species_english", "species_dutch",
        "sovon_species_id", "year", "month", "observation_date",
        "dead_bird_count", "n_total", "source_image",
    },
    "ebird_tbl": {
        "sub_id", "species_code", "obs_dt", "observation_date",
        "common_name", "scientific_name", "how_many", "presence_noted",
        "loc_id", "loc_name", "lat", "lng", "location_private",
        "obs_valid", "obs_reviewed",
        "subnational2_code", "subnational2_name",
        "subnational1_code", "subnational1_name",
        "country_code", "country_name",
        "user_display_name", "checklist_id",
        "has_comments", "has_rich_media",
        "num_species_all_time", "is_hotspot",
    },
    "movebank_tbl": set(MovebankEvent.column_names()),
    "google_earth_engine_grid_tbl": {
        "grid_cell_id", "observation_date",
        "lat", "lon",
        "bbox_min_lat", "bbox_min_lon", "bbox_max_lat", "bbox_max_lon",
        "grid_size_km", "source",
        "elevation_m", "ndvi", "evi",
        "temperature_2m_c", "total_precipitation_mm",
        "soil_moisture", "wind_speed_10m_ms",
        "cloud_cover_pct", "no2_mol_m2",
        "land_cover_class", "forest_cover_pct", "surface_water_pct",
        "profile_data", "section_availability",
        "profiler_mode", "profiler_elapsed_s",
    },
    "seismic_tbl": {
        "event_id", "event_time",
        "latitude", "longitude", "depth_km",
        "magnitude", "magnitude_type",
        "description", "region",
        "attributes",
    },
}

# Dimension weights for composite table-level score (sums to 1.0).
# Completeness & timeliness weigh most — critical for downstream ML pipeline.
# Accuracy is still important but several sub-metrics are now INFO-only
# (geographic relevance, constraint-enforced ranges).
DIMENSION_WEIGHTS: dict[str, float] = {
    "completeness": 0.30,
    "timeliness":   0.25,
    "accuracy":     0.20,
    "consistency":  0.15,
    "uniqueness":   0.05,
    "validity":     0.05,
}


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class MetricResult:
    """One quality measurement."""
    table: str
    dimension: str
    metric: str
    value: float
    threshold: float | None = None
    status: str = ""          # PASS / WARN / FAIL / INFO
    detail: str = ""

    def score(self) -> float:
        """Return normalised 0–100 score for this metric."""
        if self.threshold is None:
            return self.value if 0 <= self.value <= 100 else 0.0
        return 100.0 if self.value >= self.threshold else self.value


@dataclass
class QualityReport:
    """Accumulates metric results across all tables."""
    metrics: list[MetricResult] = field(default_factory=list)

    def add(self, m: MetricResult) -> None:
        self.metrics.append(m)

    def dimension_score(self, table: str, dimension: str) -> float:
        """Mean score for a (table, dimension) pair."""
        scores = [
            m.score() for m in self.metrics
            if m.table == table and m.dimension == dimension and m.status != "INFO"
        ]
        return round(sum(scores) / len(scores), 2) if scores else 0.0

    def composite_score(self, table: str) -> float:
        """Weighted mean of dimension scores for a table (skips empty dims)."""
        dims_present = {
            d for d in DIMENSION_WEIGHTS
            if any(
                m.status != "INFO"
                for m in self.metrics
                if m.table == table and m.dimension == d
            )
        }
        if not dims_present:
            return 0.0
        total_weight = sum(DIMENSION_WEIGHTS[d] for d in dims_present)
        weighted = sum(
            DIMENSION_WEIGHTS[d] * self.dimension_score(table, d)
            for d in dims_present
        )
        return round(weighted / total_weight, 2) if total_weight else 0.0


# ---------------------------------------------------------------------------
# Helper: run a single query, return scalar or rows
# ---------------------------------------------------------------------------

def _scalar(cur: Any, sql: str, params: tuple = ()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def _rows(cur: Any, sql: str, params: tuple = ()) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


def _table_exists(cur: Any, table: str) -> bool:
    """Return whether a public table exists in the connected database."""
    return bool(_scalar(cur, """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        )
    """, (table,)))


def _pct(part: float | None, whole: float | None) -> float:
    """Safe percentage calculation."""
    if not whole or part is None:
        return 0.0
    return round(100.0 * part / whole, 2)


def _shannon_diversity(counts: list[int]) -> float:
    """Shannon diversity index H' from a list of species counts."""
    total = sum(counts)
    if total == 0:
        return 0.0
    h = 0.0
    for c in counts:
        if c > 0:
            p = c / total
            h -= p * math.log(p)
    return round(h, 4)


_EARTH_ENGINE_TABLE_CONFIGS: dict[str, dict[str, str]] = {
    "google_earth_engine_grid_tbl": {
        "entity_key": "grid_cell_id",
        "entity_metric": "grid_cell_count",
        "entity_detail": "grid cells",
    },
}

_EARTH_ENGINE_COMPLETENESS_COLUMNS = (
    "ndvi",
    "temperature_2m_c",
    "total_precipitation_mm",
    "elevation_m",
    "soil_moisture",
    "cloud_cover_pct",
)


def _add_earth_engine_completeness_metrics(
    cur: Any,
    report: QualityReport,
    *,
    table: str,
) -> None:
    """Add completeness metrics for Earth Engine tables."""
    if not _table_exists(cur, table):
        report.add(MetricResult(
            table,
            "completeness",
            "table_exists",
            0,
            status="FAIL",
            detail="Table is missing from public schema",
        ))
        return

    total = _scalar(cur, f"SELECT COUNT(*) FROM {table}") or 0  # noqa: S608
    report.add(MetricResult(table, "completeness", "total_rows", total, status="INFO"))
    if total <= 0:
        return

    for col in _EARTH_ENGINE_COMPLETENESS_COLUMNS:
        nn = _scalar(cur, f"SELECT COUNT({col}) FROM {table}") or 0  # noqa: S608
        pct = _pct(nn, total)
        report.add(MetricResult(
            table,
            "completeness",
            f"non_null_pct_{col}",
            pct,
            70.0,
            "PASS" if pct >= 70 else "WARN",
        ))

    config = _EARTH_ENGINE_TABLE_CONFIGS[table]
    entity_count = _scalar(
        cur,
        f"SELECT COUNT(DISTINCT {config['entity_key']}) FROM {table}",  # noqa: S608
    ) or 0
    report.add(MetricResult(
        table,
        "completeness",
        config["entity_metric"],
        entity_count,
        status="INFO",
        detail=f"{entity_count} distinct {config['entity_detail']}",
    ))

    date_range = _rows(cur, f"""
        SELECT MIN(observation_date), MAX(observation_date),
               COUNT(DISTINCT observation_date)
        FROM {table}
    """)
    if date_range and date_range[0][0]:
        mn, mx, distinct_days = date_range[0]
        expected_days = (mx - mn).days + 1
        pct = _pct(distinct_days, expected_days)
        report.add(MetricResult(
            table,
            "completeness",
            "temporal_coverage_pct",
            pct,
            50.0,
            "PASS" if pct >= 50 else "WARN",
            f"{distinct_days}/{expected_days} days ({mn} – {mx})",
        ))

    nonempty = _scalar(cur, f"""
        SELECT COUNT(*) FROM {table}
        WHERE profile_data IS NOT NULL AND profile_data != '{{}}'::jsonb
    """) or 0
    report.add(MetricResult(
        table,
        "completeness",
        "profile_data_nonempty_pct",
        _pct(nonempty, total),
        status="INFO",
        detail=(
            f"{nonempty}/{total} have non-empty residual profile_data; "
            "empty JSON is expected when all mapped fields were captured"
        ),
    ))


def _add_earth_engine_accuracy_metrics(
    cur: Any,
    report: QualityReport,
    *,
    table: str,
) -> None:
    """Add shared range and coordinate checks for Earth Engine tables."""
    if not _table_exists(cur, table):
        return

    total = _scalar(cur, f"SELECT COUNT(*) FROM {table}") or 0  # noqa: S608
    if total <= 0:
        return

    bad_coords = _scalar(cur, f"""
        SELECT COUNT(*) FROM {table}
        WHERE lat < -90 OR lat > 90 OR lon < -180 OR lon > 180
    """) or 0
    report.add(MetricResult(
        table,
        "accuracy",
        "coords_in_wgs84_pct",
        _pct(total - bad_coords, total),
        status="INFO",
        detail=f"{bad_coords} outside WGS 84 bounds",
    ))

    outside_nl = _scalar(cur, f"""
        SELECT COUNT(*) FROM {table}
        WHERE lat < %s OR lat > %s OR lon < %s OR lon > %s
    """, (NL_LAT_MIN, NL_LAT_MAX, NL_LON_MIN, NL_LON_MAX)) or 0
    report.add(MetricResult(
        table,
        "accuracy",
        "coords_in_netherlands_pct",
        _pct(total - outside_nl, total),
        status="INFO",
        detail=f"{outside_nl} outside NL bbox",
    ))

    if table == "google_earth_engine_grid_tbl":
        bbox_oob = _scalar(cur, """
            SELECT COUNT(*) FROM google_earth_engine_grid_tbl
            WHERE bbox_min_lat < -90 OR bbox_min_lat > 90
               OR bbox_max_lat < -90 OR bbox_max_lat > 90
               OR bbox_min_lon < -180 OR bbox_min_lon > 180
               OR bbox_max_lon < -180 OR bbox_max_lon > 180
        """) or 0
        report.add(MetricResult(
            table,
            "accuracy",
            "bbox_coords_in_wgs84_pct",
            _pct(total - bbox_oob, total),
            status="INFO",
            detail=f"{bbox_oob} bbox corners outside WGS 84 bounds",
        ))

    range_checks = (
        ("ndvi", "ndvi", -1, 1, 95.0, "NDVI outside [-1, 1]"),
        ("temperature_2m_c", "temperature", -60, 60, 98.0, "outside (-60, 60) °C"),
        ("evi", "evi", -1, 1, 95.0, "EVI outside [-1, 1]"),
        ("total_precipitation_mm", "precipitation", 0, None, 99.0, "negative precipitation values"),
        ("cloud_cover_pct", "cloud_cover", 0, 100, 98.0, "outside [0, 100] %"),
        ("wind_speed_10m_ms", "wind_speed", 0, None, 99.0, "negative wind speed values"),
        ("soil_moisture", "soil_moisture", 0, 1, 95.0, "outside [0, 1] m³/m³"),
        ("no2_mol_m2", "no2", 0, None, 99.0, "negative NO₂ values"),
        ("forest_cover_pct", "forest_cover", 0, 100, 98.0, "outside [0, 100] %"),
        ("surface_water_pct", "surface_water", 0, 100, 98.0, "outside [0, 100] %"),
    )
    for column, metric_name, low, high, threshold, detail in range_checks:
        comparator = f"({column} < %s OR {column} > %s)" if high is not None else f"{column} < %s"
        params = (low, high) if high is not None else (low,)
        oob = _scalar(cur, f"""
            SELECT COUNT(*) FROM {table}
            WHERE {column} IS NOT NULL AND {comparator}
        """, params) or 0
        not_null = _scalar(cur, f"SELECT COUNT({column}) FROM {table}") or 1  # noqa: S608
        report.add(MetricResult(
            table,
            "accuracy",
            f"{metric_name}_in_range_pct" if high is not None else f"{metric_name}_non_negative_pct",
            _pct(not_null - oob, not_null),
            threshold,
            "PASS" if oob == 0 else "WARN",
            f"{oob} {detail}",
        ))


def _add_earth_engine_uniqueness_metrics(
    cur: Any,
    report: QualityReport,
    *,
    table: str,
) -> None:
    """Add natural-key uniqueness metrics for Earth Engine tables."""
    if not _table_exists(cur, table):
        return

    total = _scalar(cur, f"SELECT COUNT(*) FROM {table}") or 0  # noqa: S608
    if total <= 0:
        return

    entity_key = _EARTH_ENGINE_TABLE_CONFIGS[table]["entity_key"]
    distinct = _scalar(cur, f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT {entity_key}, observation_date
            FROM {table}
        ) sub
    """) or 0
    report.add(MetricResult(
        table,
        "uniqueness",
        "natural_key_uniqueness_pct",
        _pct(distinct, total),
        status="INFO",
        detail=f"{total - distinct} duplicate keys (PK enforced)",
    ))


def _add_earth_engine_validity_metrics(
    cur: Any,
    report: QualityReport,
    *,
    table: str,
) -> None:
    """Add shared validity checks for Earth Engine tables."""
    if not _table_exists(cur, table):
        return

    total = _scalar(cur, f"SELECT COUNT(*) FROM {table}") or 0  # noqa: S608
    if total <= 0:
        return

    valid_mode = _scalar(cur, f"""
        SELECT COUNT(*) FROM {table}
        WHERE profiler_mode IS NULL
           OR profiler_mode IN ('parallel', 'sequential')
    """) or 0
    report.add(MetricResult(
        table,
        "validity",
        "valid_profiler_mode_pct",
        _pct(valid_mode, total),
        99.0,
        "PASS" if valid_mode == total else "WARN",
        f"{total - valid_mode} unknown profiler modes",
    ))

    if table == "google_earth_engine_grid_tbl":
        valid_bbox = _scalar(cur, """
            SELECT COUNT(*) FROM google_earth_engine_grid_tbl
            WHERE bbox_min_lat <= bbox_max_lat
              AND bbox_min_lon <= bbox_max_lon
              AND grid_size_km > 0
        """) or 0
        report.add(MetricResult(
            table,
            "validity",
            "valid_grid_geometry_pct",
            _pct(valid_bbox, total),
            status="INFO",
            detail=f"{total - valid_bbox} rows with invalid bbox ordering or grid size",
        ))


# ---------------------------------------------------------------------------
# 1. COMPLETENESS
# ---------------------------------------------------------------------------

def _assess_completeness(cur: Any, report: QualityReport) -> None:
    """Non-null percentages, temporal coverage, and species coverage."""

    # ── birdweather_tbl ────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM birdweather_tbl") or 0
    report.add(MetricResult("birdweather_tbl", "completeness",
                            "total_rows", total, status="INFO"))

    if total > 0:
        for col in ("confidence", "score", "probability", "species_id",
                    "station_id", "audio_url"):
            nn = _scalar(cur,
                         f"SELECT COUNT({col}) FROM birdweather_tbl")  # noqa: S608
            report.add(MetricResult(
                "birdweather_tbl", "completeness",
                f"non_null_pct_{col}", _pct(nn, total), 95.0,
                "PASS" if _pct(nn, total) >= 95 else "WARN",
            ))

        # Temporal completeness: % of days with ≥ 1 detection
        date_range = _rows(cur, """
            SELECT MIN(observation_date), MAX(observation_date),
                   COUNT(DISTINCT observation_date)
            FROM birdweather_tbl
        """)
        if date_range and date_range[0][0] and date_range[0][1]:
            mn, mx, distinct_days = date_range[0]
            expected_days = (mx - mn).days + 1
            pct = _pct(distinct_days, expected_days)
            report.add(MetricResult(
                "birdweather_tbl", "completeness",
                "temporal_day_coverage_pct", pct, 80.0,
                "PASS" if pct >= 80 else "WARN",
                f"{distinct_days}/{expected_days} days",
            ))

        # Target-species coverage. Names live on species_tbl now; we count
        # distinct *detected* species_ids whose canonical common_name is in
        # the target list.
        found = _scalar(cur, """
            SELECT COUNT(DISTINCT b.species_id)
            FROM birdweather_tbl b
            JOIN species_tbl sp ON sp.species_id = b.species_id
            WHERE sp.common_name = ANY(%s)
        """, (TARGET_SPECIES,)) or 0
        report.add(MetricResult(
            "birdweather_tbl", "completeness",
            "target_species_coverage", _pct(found, len(TARGET_SPECIES)),
            status="INFO",
            detail=f"{found}/{len(TARGET_SPECIES)} species",
        ))

        # Longest consecutive-day gap
        gap = _scalar(cur, """
            WITH days AS (
                SELECT DISTINCT observation_date AS d
                FROM birdweather_tbl ORDER BY d
            ),
            gaps AS (
                SELECT d, (LEAD(d) OVER (ORDER BY d) - d) AS gap_days
                FROM days
            )
            SELECT MAX(gap_days) FROM gaps
        """)
        gap_days = int(gap) if gap else 0
        report.add(MetricResult(
            "birdweather_tbl", "completeness",
            "max_consecutive_gap_days", gap_days, status="INFO",
            detail=f"longest gap between detection days: {gap_days}d",
        ))

        # Volume trend: compare last-7-day avg to preceding-30-day avg
        trend = _rows(cur, """
            WITH recent AS (
                SELECT COUNT(*)::float / NULLIF(COUNT(DISTINCT observation_date), 0)
                FROM birdweather_tbl
                WHERE observation_date >= CURRENT_DATE - 7
            ),
            baseline AS (
                SELECT COUNT(*)::float / NULLIF(COUNT(DISTINCT observation_date), 0)
                FROM birdweather_tbl
                WHERE observation_date >= CURRENT_DATE - 37
                  AND observation_date < CURRENT_DATE - 7
            )
            SELECT (SELECT * FROM recent), (SELECT * FROM baseline)
        """)
        if trend and trend[0][0] and trend[0][1] and trend[0][1] > 0:
            ratio = round(100.0 * trend[0][0] / trend[0][1], 1)
            report.add(MetricResult(
                "birdweather_tbl", "completeness",
                "volume_trend_pct", ratio,
                status="INFO",
                detail=f"last-7d avg / prev-30d avg = {ratio}%",
            ))

        # Species diversity index (Shannon H'). Keys on species_id (canonical
        # FK) — only the per-species count matters for the diversity formula.
        sp_counts = _rows(cur, """
            SELECT species_id, COUNT(*) FROM birdweather_tbl
            GROUP BY species_id
        """)
        if sp_counts:
            h = _shannon_diversity([int(r[1]) for r in sp_counts])
            report.add(MetricResult(
                "birdweather_tbl", "completeness",
                "species_diversity_shannon", h, status="INFO",
                detail=f"H'={h} across {len(sp_counts)} species",
            ))

    # ── knmi_tbl ─────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM knmi_tbl") or 0
    report.add(MetricResult("knmi_tbl", "completeness",
                            "total_rows", total, status="INFO"))

    if total > 0:
        for col in ("temperature_c", "relative_humidity_pct", "wind_speed_ms",
                    "wind_direction_deg", "pressure_hpa", "precipitation_mm",
                    "solar_radiation_wm2", "cloud_cover_okta"):
            nn = _scalar(cur,
                         f"SELECT COUNT({col}) FROM knmi_tbl")  # noqa: S608
            report.add(MetricResult(
                "knmi_tbl", "completeness",
                f"non_null_pct_{col}", _pct(nn, total), 80.0,
                "PASS" if _pct(nn, total) >= 80 else "WARN",
            ))

        # Station coverage
        active_stations = _scalar(cur, """
            SELECT COUNT(DISTINCT station_id) FROM knmi_tbl
        """) or 0
        report.add(MetricResult(
            "knmi_tbl", "completeness",
            "active_station_count", active_stations, status="INFO",
        ))

    # ── sovon_tbl ──────────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM sovon_tbl") or 0
    report.add(MetricResult("sovon_tbl", "completeness",
                            "total_rows", total, status="INFO"))
    if total > 0:
        species_count = _scalar(cur, """
            SELECT COUNT(DISTINCT species_english) FROM sovon_tbl
        """) or 0
        report.add(MetricResult(
            "sovon_tbl", "completeness",
            "species_count", species_count, status="INFO",
        ))

        yr_range = _rows(cur, """
            SELECT MIN(year), MAX(year),
                   COUNT(DISTINCT year)
            FROM sovon_tbl
        """)
        if yr_range and yr_range[0][0]:
            mn, mx, distinct_yrs = yr_range[0]
            expected_yrs = mx - mn + 1
            report.add(MetricResult(
                "sovon_tbl", "completeness",
                "year_coverage_pct", _pct(distinct_yrs, expected_yrs), 80.0,
                "PASS" if _pct(distinct_yrs, expected_yrs) >= 80 else "WARN",
                f"{distinct_yrs}/{expected_yrs} years ({mn}–{mx})",
            ))

        for col in ("dead_bird_count", "n_total"):
            nn = _scalar(cur, f"SELECT COUNT({col}) FROM sovon_tbl")  # noqa: S608
            report.add(MetricResult(
                "sovon_tbl", "completeness",
                f"non_null_pct_{col}", _pct(nn, total), 90.0,
                "PASS" if _pct(nn, total) >= 90 else "WARN",
            ))

        # Target-species coverage in Sovon
        sovon_sp = _scalar(cur, """
            SELECT COUNT(DISTINCT species_english) FROM sovon_tbl
            WHERE species_english = ANY(%s)
        """, (TARGET_SPECIES,)) or 0
        report.add(MetricResult(
            "sovon_tbl", "completeness",
            "target_species_coverage", _pct(
                sovon_sp, len(TARGET_SPECIES)),
            status="INFO",
            detail=f"{sovon_sp}/{len(TARGET_SPECIES)} target species have mortality data",
        ))

        # Month coverage per species (12 months expected)
        avg_month_cov = _scalar(cur, """
            SELECT AVG(mcnt) FROM (
                SELECT species_english, COUNT(DISTINCT month) AS mcnt
                FROM sovon_tbl
                GROUP BY species_english
            ) sub
        """) or 0
        report.add(MetricResult(
            "sovon_tbl", "completeness",
            "avg_month_coverage_per_species", round(float(avg_month_cov), 1),
            status="INFO",
            detail=f"avg distinct months per species: {avg_month_cov:.1f}/12",
        ))

    # ── ebird_tbl ────────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM ebird_tbl") or 0
    report.add(MetricResult("ebird_tbl", "completeness",
                            "total_rows", total, status="INFO"))
    if total > 0:
        # Non-null checks on key columns
        for col in ("common_name", "lat", "lng", "loc_id", "species_code"):
            nn = _scalar(cur, f"SELECT COUNT({col}) FROM ebird_tbl")  # noqa: S608
            report.add(MetricResult(
                "ebird_tbl", "completeness",
                f"non_null_pct_{col}", _pct(nn, total), 95.0,
                "PASS" if _pct(nn, total) >= 95 else "WARN",
            ))

        # Temporal coverage
        date_range = _rows(cur, """
            SELECT MIN(observation_date), MAX(observation_date),
                   COUNT(DISTINCT observation_date)
            FROM ebird_tbl
        """)
        if date_range and date_range[0][0]:
            mn, mx, distinct_days = date_range[0]
            expected_days = (mx - mn).days + 1
            report.add(MetricResult(
                "ebird_tbl", "completeness",
                "temporal_coverage_pct",
                _pct(distinct_days, expected_days), 70.0,
                "PASS" if _pct(distinct_days, expected_days) >= 70 else "WARN",
                f"{distinct_days}/{expected_days} days ({mn} – {mx})",
            ))

        # Species diversity
        species_count = _scalar(cur, """
            SELECT COUNT(DISTINCT species_code) FROM ebird_tbl
        """) or 0
        report.add(MetricResult(
            "ebird_tbl", "completeness",
            "species_count", species_count, status="INFO",
        ))

        # Target-species coverage
        ebird_target = _scalar(cur, """
            SELECT COUNT(DISTINCT common_name) FROM ebird_tbl
            WHERE common_name = ANY(%s)
        """, (TARGET_SPECIES,)) or 0
        report.add(MetricResult(
            "ebird_tbl", "completeness",
            "target_species_coverage",
            _pct(ebird_target, len(TARGET_SPECIES)),
            status="INFO",
            detail=f"{ebird_target}/{len(TARGET_SPECIES)} target species found",
        ))

        # Location count
        loc_count = _scalar(cur, """
            SELECT COUNT(DISTINCT loc_id) FROM ebird_tbl
        """) or 0
        report.add(MetricResult(
            "ebird_tbl", "completeness",
            "location_count", loc_count, status="INFO",
        ))

    # ── movebank_tbl ───────────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM movebank_tbl") or 0
    report.add(MetricResult("movebank_tbl", "completeness",
                            "total_rows", total, status="INFO"))
    if total > 0:
        # Non-null checks
        for col in ("location_lat", "location_long", "individual_id",
                    "individual_taxon_canonical_name"):
            nn = _scalar(cur, f"SELECT COUNT({col}) FROM movebank_tbl")  # noqa: S608
            report.add(MetricResult(
                "movebank_tbl", "completeness",
                f"non_null_pct_{col}", _pct(nn, total), 80.0,
                "PASS" if _pct(nn, total) >= 80 else "WARN",
            ))

        # Study and individual counts
        study_count = _scalar(cur, """
            SELECT COUNT(DISTINCT study_id) FROM movebank_tbl
        """) or 0
        report.add(MetricResult(
            "movebank_tbl", "completeness",
            "study_count", study_count, status="INFO",
        ))
        individual_count = _scalar(cur, """
            SELECT COUNT(DISTINCT individual_id) FROM movebank_tbl
        """) or 0
        report.add(MetricResult(
            "movebank_tbl", "completeness",
            "individual_count", individual_count, status="INFO",
        ))

        # Temporal coverage
        date_range = _rows(cur, """
            SELECT MIN(timestamp::date), MAX(timestamp::date),
                   COUNT(DISTINCT timestamp::date)
            FROM movebank_tbl
        """)
        if date_range and date_range[0][0]:
            mn, mx, distinct_days = date_range[0]
            expected_days = (mx - mn).days + 1
            report.add(MetricResult(
                "movebank_tbl", "completeness",
                "temporal_coverage_pct",
                _pct(distinct_days, expected_days), 50.0,
                "PASS" if _pct(distinct_days, expected_days) >= 50 else "WARN",
                f"{distinct_days}/{expected_days} days ({mn} – {mx})",
            ))

        # GPS location coverage (non-null lat AND long)
        gps_ok = _scalar(cur, """
            SELECT COUNT(*) FROM movebank_tbl
            WHERE location_lat IS NOT NULL AND location_long IS NOT NULL
        """) or 0
        report.add(MetricResult(
            "movebank_tbl", "completeness",
            "gps_location_pct", _pct(gps_ok, total), 70.0,
            "PASS" if _pct(gps_ok, total) >= 70 else "WARN",
            f"{gps_ok}/{total} events have GPS coordinates",
        ))

    # ── Earth Engine tables ──────────────────────────────────────────────
    for table in _EARTH_ENGINE_TABLE_CONFIGS:
        _add_earth_engine_completeness_metrics(cur, report, table=table)

    # ── seismic_tbl ────────────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM seismic_tbl") or 0
    report.add(MetricResult("seismic_tbl", "completeness",
                            "total_rows", total, status="INFO"))
    if total > 0:
        for col in ("latitude", "longitude", "magnitude", "region"):
            nn = _scalar(cur,
                         f"SELECT COUNT({col}) FROM seismic_tbl")  # noqa: S608
            report.add(MetricResult(
                "seismic_tbl", "completeness",
                f"non_null_pct_{col}", _pct(nn, total),
                80.0 if col in ("magnitude", "region") else None,
                "PASS" if _pct(nn, total) >= 80 else (
                    "WARN" if col in ("magnitude", "region") else "INFO"),
            ))

        # Temporal coverage
        date_range = _rows(cur, """
            SELECT MIN(event_time::date), MAX(event_time::date),
                   COUNT(DISTINCT event_time::date)
            FROM seismic_tbl
        """)
        if date_range and date_range[0][0]:
            mn, mx, distinct_days = date_range[0]
            expected_days = (mx - mn).days + 1
            report.add(MetricResult(
                "seismic_tbl", "completeness",
                "temporal_coverage_pct",
                _pct(distinct_days, expected_days), status="INFO",
                detail=f"{distinct_days}/{expected_days} days ({mn} – {mx})",
            ))


# ---------------------------------------------------------------------------
# 2. TIMELINESS
# ---------------------------------------------------------------------------

def _assess_timeliness(cur: Any, report: QualityReport) -> None:
    """Data freshness based on each table's domain timestamp."""
    now = datetime.now(timezone.utc)

    # Per-table freshness: hours since most recent record
    freshness_queries = {
        "birdweather_tbl": "SELECT MAX(detected_at) FROM birdweather_tbl",
        "knmi_tbl": "SELECT MAX(observed_at) FROM knmi_tbl",
        "sovon_tbl": "SELECT MAX(observation_date) FROM sovon_tbl",
        "ebird_tbl": "SELECT MAX(obs_dt) FROM ebird_tbl",
        "movebank_tbl": "SELECT MAX(timestamp) FROM movebank_tbl",
        "google_earth_engine_grid_tbl": (
            "SELECT MAX(observation_date) FROM google_earth_engine_grid_tbl"
        ),
        "seismic_tbl": "SELECT MAX(event_time) FROM seismic_tbl",
    }

    for table, sql in freshness_queries.items():
        if not _table_exists(cur, table):
            report.add(MetricResult(
                table,
                "timeliness",
                "table_exists",
                0,
                status="FAIL",
                detail="Table is missing from public schema",
            ))
            continue

        row = _rows(cur, sql)
        if not row or not row[0][0]:
            report.add(MetricResult(
                table, "timeliness", "data_freshness_hours",
                0, status="FAIL", detail="No data found",
            ))
            continue

        latest_event = row[0][0]
        # DATE columns (e.g. google_earth_engine_grid_tbl.observation_date) return
        # a Python date, which lacks .tzinfo.  Promote to midnight UTC.
        if not isinstance(latest_event, datetime):
            latest_event = datetime(
                latest_event.year, latest_event.month, latest_event.day,
                tzinfo=timezone.utc,
            )
        elif latest_event.tzinfo is None:
            latest_event = latest_event.replace(tzinfo=timezone.utc)

        staleness_h = round((now - latest_event).total_seconds() / 3600, 1)
        # Different tables have different acceptable staleness
        threshold_map = {
            "birdweather_tbl": 48,
            "knmi_tbl": 24,
            "sovon_tbl": 730,   # monthly refresh, ~30 days tolerable
            "ebird_tbl": 48,
            "movebank_tbl": 168,    # GPS data may lag several days
            # Earth Engine is ingested bi-weekly (1st/15th anchors) and upstream
            # rasters publish with multi-week latency, so tolerate ~60 days of
            # staleness by default.  Override via EARTH_ENGINE_FRESHNESS_HOURS.
            "google_earth_engine_grid_tbl": _env_int(
                "EARTH_ENGINE_FRESHNESS_HOURS", 1440, minimum=1,
            ),
            "seismic_tbl": 168,   # earthquake catalog may be updated weekly
        }
        thresh = threshold_map.get(table, 168)
        report.add(MetricResult(
            table, "timeliness", "data_freshness_hours",
            staleness_h, status="INFO",
            detail=f"Last event: {latest_event:%Y-%m-%d %H:%M} UTC",
        ))
        report.add(MetricResult(
            table, "timeliness", "freshness_score",
            100.0 if staleness_h <= thresh else max(
                0, 100 - (staleness_h - thresh) / thresh * 100),
            90.0,
            "PASS" if staleness_h <= thresh else "WARN",
            f"threshold={thresh}h, actual={staleness_h}h",
        ))

    # Per-station freshness: flag stations with no data in the last 7 days
    bw_total_stations = _scalar(cur, """
        SELECT COUNT(DISTINCT station_id) FROM birdweather_tbl
    """) or 0
    if bw_total_stations > 0:
        stale_stations = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT station_id, MAX(observation_date) AS last_date
                FROM birdweather_tbl
                GROUP BY station_id
                HAVING MAX(observation_date) < CURRENT_DATE - 7
            ) sub
        """) or 0
        report.add(MetricResult(
            "birdweather_tbl", "timeliness",
            "active_station_pct_7d",
            _pct(bw_total_stations - stale_stations, bw_total_stations), 60.0,
            "PASS" if stale_stations < bw_total_stations * 0.4 else "WARN",
            f"{bw_total_stations - stale_stations}/{bw_total_stations} stations active in last 7d",
        ))

    # KNMI 10-minute observation regularity (expected interval = 600s)
    knmi_total = _scalar(
        cur, "SELECT COUNT(*) FROM knmi_tbl") or 0
    if knmi_total > 1:
        irregular = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT observed_at,
                       EXTRACT(EPOCH FROM (
                           observed_at - LAG(observed_at) OVER (
                               PARTITION BY station_id ORDER BY observed_at
                           )
                       )) AS gap_s
                FROM knmi_tbl
            ) sub
            WHERE gap_s IS NOT NULL AND gap_s != 600
        """) or 0
        regular_total = knmi_total - _scalar(cur, """
            SELECT COUNT(DISTINCT station_id) FROM knmi_tbl
        """)  # subtract first-per-station (no LAG)
        regular_total = max(regular_total, 1)
        report.add(MetricResult(
            "knmi_tbl", "timeliness",
            "observation_regularity_pct",
            _pct(regular_total - irregular, regular_total), 90.0,
            "PASS" if _pct(regular_total - irregular,
                           regular_total) >= 90 else "WARN",
            f"{irregular} non-10-min intervals",
        ))

    # Per-station freshness for KNMI: flag stations with no data in last 2 days
    knmi_total_stations = _scalar(cur, """
        SELECT COUNT(DISTINCT station_id) FROM knmi_tbl
    """) or 0
    if knmi_total_stations > 0:
        knmi_stale = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT station_id, MAX(observed_at) AS last_obs
                FROM knmi_tbl
                GROUP BY station_id
                HAVING MAX(observed_at) < NOW() - INTERVAL '2 days'
            ) sub
        """) or 0
        report.add(MetricResult(
            "knmi_tbl", "timeliness",
            "active_station_pct_2d",
            _pct(knmi_total_stations - knmi_stale, knmi_total_stations), 60.0,
            "PASS" if knmi_stale < knmi_total_stations * 0.4 else "WARN",
            f"{knmi_total_stations - knmi_stale}/{knmi_total_stations} stations active in last 2d",
        ))


# ---------------------------------------------------------------------------
# 3. ACCURACY
# ---------------------------------------------------------------------------

def _assess_accuracy(cur: Any, report: QualityReport) -> None:
    """Range checks and coordinate validation."""

    # ── birdweather_tbl ────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM birdweather_tbl") or 0
    if total > 0:
        # Confidence in [0, 1] — enforced by CHECK constraint; INFO only
        oob = _scalar(cur, """
            SELECT COUNT(*) FROM birdweather_tbl
            WHERE confidence < 0 OR confidence > 1
        """) or 0
        report.add(MetricResult(
            "birdweather_tbl", "accuracy",
            "confidence_in_range_pct", _pct(total - oob, total),
            status="INFO", detail=f"{oob} out-of-range rows",
        ))

        # Coordinates inside NL bounding box — geographic relevance, not
        # data quality; a station in Belgium with perfect data is still valid.
        # Coordinates are sourced from the joined station row (per-detection
        # lat/lon were dropped because they would only differ for mobile
        # stations excluded from the credible-tier path).
        outside_nl = _scalar(cur, """
            SELECT COUNT(*)
            FROM birdweather_tbl b
            JOIN birdweather_stations_tbl s ON s.station_id = b.station_id
            WHERE s.lat IS NOT NULL AND s.lon IS NOT NULL
              AND (s.lat < %s OR s.lat > %s
                   OR s.lon < %s OR s.lon > %s)
        """, (NL_LAT_MIN, NL_LAT_MAX, NL_LON_MIN, NL_LON_MAX)) or 0
        report.add(MetricResult(
            "birdweather_tbl", "accuracy",
            "coords_in_netherlands_pct", _pct(total - outside_nl, total),
            status="INFO", detail=f"{outside_nl} outside NL bbox",
        ))

        # Confidence distribution (descriptive statistics)
        stats = _rows(cur, """
            SELECT ROUND(AVG(confidence)::numeric, 4),
                   ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY confidence)::numeric, 4),
                   ROUND(PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY confidence)::numeric, 4),
                   ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY confidence)::numeric, 4)
            FROM birdweather_tbl WHERE confidence IS NOT NULL
        """)
        if stats and stats[0][0]:
            mean, med, p5, p95 = [float(v) for v in stats[0]]
            report.add(MetricResult(
                "birdweather_tbl", "accuracy",
                "confidence_mean", mean, status="INFO",
                detail=f"median={med} p5={p5} p95={p95}",
            ))

        # NOTE: ``detection_station_coord_agreement_pct`` was removed — it
        # only existed because the old per-detection ``detection_lat`` /
        # ``detection_lon`` columns could drift from the station record.
        # Those columns have been dropped (a detection is now defined to
        # sit at its station), so the metric is a tautology.

        # Statistical outliers: confidence z-score > 3 (uses stddev_samp)
        outlier_rows = _rows(cur, """
            WITH stats AS (
                SELECT AVG(confidence) AS mu, STDDEV_SAMP(confidence) AS sigma
                FROM birdweather_tbl WHERE confidence IS NOT NULL
            )
            SELECT COUNT(*) FILTER (WHERE ABS(confidence - mu) > 3 * sigma),
                   COUNT(*)
            FROM birdweather_tbl, stats
            WHERE confidence IS NOT NULL AND sigma > 0
        """)
        if outlier_rows and outlier_rows[0][1]:
            n_outlier, n_total = int(
                outlier_rows[0][0]), int(outlier_rows[0][1])
            report.add(MetricResult(
                "birdweather_tbl", "accuracy",
                "confidence_outlier_pct", _pct(n_outlier, n_total), status="INFO",
                detail=f"{n_outlier}/{n_total} with |z|>3",
            ))

    # ── knmi_tbl ─────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM knmi_tbl") or 0
    if total > 0:
        range_checks = {
            "temperature_c": (-50, 50),
            "relative_humidity_pct": (0, 100),
            "wind_speed_ms": (0, 75),
            "wind_direction_deg": (0, 360),
            "pressure_hpa": (870, 1085),
            "precipitation_mm": (0, 200),
            "solar_radiation_wm2": (0, 2000),
            "cloud_cover_okta": (0, 9),
        }
        # Columns whose SQL CHECK fully covers the Python range → INFO only
        _knmi_check_enforced = {
            "relative_humidity_pct",   # CHECK (0–100)
            "wind_direction_deg",      # CHECK (0–360)
            "cloud_cover_okta",        # CHECK (0–9)
        }
        for col, (lo, hi) in range_checks.items():
            oob = _scalar(cur, f"""
                SELECT COUNT(*) FROM knmi_tbl
                WHERE {col} IS NOT NULL AND ({col} < %s OR {col} > %s)
            """, (lo, hi)) or 0
            not_null = _scalar(cur,
                               f"SELECT COUNT({col}) FROM knmi_tbl") or 1
            if col in _knmi_check_enforced:
                report.add(MetricResult(
                    "knmi_tbl", "accuracy",
                    f"{col}_in_range_pct", _pct(not_null - oob, not_null),
                    status="INFO",
                    detail=f"{oob} out-of-range ({lo}–{hi}) (CHECK constraint enforced)",
                ))
            else:
                report.add(MetricResult(
                    "knmi_tbl", "accuracy",
                    f"{col}_in_range_pct", _pct(
                        not_null - oob, not_null), 98.0,
                    "PASS" if oob == 0 else "WARN",
                    f"{oob} out-of-range ({lo}–{hi})",
                ))

        # Coordinates inside NL — geographic relevance, not data quality
        outside_nl = _scalar(cur, """
            SELECT COUNT(*) FROM knmi_tbl
            WHERE station_lat IS NOT NULL
              AND (station_lat < %s OR station_lat > %s
                   OR station_lon < %s OR station_lon > %s)
        """, (NL_LAT_MIN, NL_LAT_MAX, NL_LON_MIN, NL_LON_MAX)) or 0
        report.add(MetricResult(
            "knmi_tbl", "accuracy",
            "coords_in_netherlands_pct", _pct(total - outside_nl, total),
            status="INFO", detail=f"{outside_nl} outside NL bbox",
        ))

        # Temperature statistical outliers (z-score > 3)
        temp_outliers = _rows(cur, """
            WITH stats AS (
                SELECT AVG(temperature_c) AS mu, STDDEV_SAMP(temperature_c) AS sigma
                FROM knmi_tbl WHERE temperature_c IS NOT NULL
            )
            SELECT COUNT(*) FILTER (WHERE ABS(temperature_c - mu) > 3 * sigma),
                   COUNT(*)
            FROM knmi_tbl, stats
            WHERE temperature_c IS NOT NULL AND sigma > 0
        """)
        if temp_outliers and temp_outliers[0][1]:
            n_out, n_tot = int(temp_outliers[0][0]), int(temp_outliers[0][1])
            report.add(MetricResult(
                "knmi_tbl", "accuracy",
                "temperature_outlier_pct", _pct(n_out, n_tot), status="INFO",
                detail=f"{n_out}/{n_tot} with |z|>3",
            ))

    # ── sovon_tbl ──────────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM sovon_tbl") or 0
    if total > 0:
        neg_counts = _scalar(cur, """
            SELECT COUNT(*) FROM sovon_tbl WHERE dead_bird_count < 0
        """) or 0
        report.add(MetricResult(
            "sovon_tbl", "accuracy",
            "non_negative_count_pct", _pct(total - neg_counts, total),
            status="INFO",
            detail=f"{neg_counts} negative mortality counts (CHECK constraint enforced)",
        ))

        bad_year = _scalar(cur, """
            SELECT COUNT(*) FROM sovon_tbl
            WHERE year < 2000 OR year > EXTRACT(YEAR FROM NOW())
        """) or 0
        report.add(MetricResult(
            "sovon_tbl", "accuracy",
            "year_in_range_pct", _pct(total - bad_year, total), 99.0,
            "PASS" if bad_year == 0 else "WARN",
            f"{bad_year} out-of-range years",
        ))

    # ── ebird_tbl ────────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM ebird_tbl") or 0
    if total > 0:
        # Coordinates inside NL bbox
        outside_nl = _scalar(cur, """
            SELECT COUNT(*) FROM ebird_tbl
            WHERE lat < %s OR lat > %s OR lng < %s OR lng > %s
        """, (NL_LAT_MIN, NL_LAT_MAX, NL_LON_MIN, NL_LON_MAX)) or 0
        report.add(MetricResult(
            "ebird_tbl", "accuracy",
            "coords_in_netherlands_pct", _pct(total - outside_nl, total),
            status="INFO", detail=f"{outside_nl} outside NL bbox",
        ))

        # how_many >= 0 (CHECK constraint; INFO only)
        neg_count = _scalar(cur, """
            SELECT COUNT(*) FROM ebird_tbl
            WHERE how_many IS NOT NULL AND how_many < 0
        """) or 0
        report.add(MetricResult(
            "ebird_tbl", "accuracy",
            "non_negative_count_pct",
            _pct(total - neg_count, total),
            status="INFO",
            detail=f"{neg_count} negative how_many values (CHECK constraint enforced)",
        ))

        # Lat/lng range validation (should be within WGS 84 bounds)
        bad_coords = _scalar(cur, """
            SELECT COUNT(*) FROM ebird_tbl
            WHERE lat < -90 OR lat > 90 OR lng < -180 OR lng > 180
        """) or 0
        report.add(MetricResult(
            "ebird_tbl", "accuracy",
            "coords_in_wgs84_pct", _pct(total - bad_coords, total),
            status="INFO", detail=f"{bad_coords} outside WGS 84 bounds",
        ))

    # ── movebank_tbl ───────────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM movebank_tbl") or 0
    if total > 0:
        # Location range checks
        has_loc = _scalar(cur, """
            SELECT COUNT(*) FROM movebank_tbl
            WHERE location_lat IS NOT NULL AND location_long IS NOT NULL
        """) or 0
        if has_loc > 0:
            bad_loc = _scalar(cur, """
                SELECT COUNT(*) FROM movebank_tbl
                WHERE location_lat IS NOT NULL
                  AND (location_lat < -90 OR location_lat > 90
                       OR location_long < -180 OR location_long > 180)
            """) or 0
            report.add(MetricResult(
                "movebank_tbl", "accuracy",
                "coords_in_wgs84_pct", _pct(has_loc - bad_loc, has_loc),
                status="INFO", detail=f"{bad_loc} outside WGS 84 bounds",
            ))

            # Coords in NL bbox
            outside_nl = _scalar(cur, """
                SELECT COUNT(*) FROM movebank_tbl
                WHERE location_lat IS NOT NULL
                  AND (location_lat < %s OR location_lat > %s
                       OR location_long < %s OR location_long > %s)
            """, (NL_LAT_MIN, NL_LAT_MAX, NL_LON_MIN, NL_LON_MAX)) or 0
            report.add(MetricResult(
                "movebank_tbl", "accuracy",
                "coords_in_netherlands_pct", _pct(
                    has_loc - outside_nl, has_loc),
                status="INFO", detail=f"{outside_nl} outside NL bbox",
            ))

        # Ground speed range — CHECK constraint enforced (ground_speed >= 0); INFO only
        bad_speed = _scalar(cur, """
            SELECT COUNT(*) FROM movebank_tbl
            WHERE ground_speed IS NOT NULL AND ground_speed < 0
        """) or 0
        not_null_speed = _scalar(
            cur, "SELECT COUNT(ground_speed) FROM movebank_tbl") or 1
        report.add(MetricResult(
            "movebank_tbl", "accuracy",
            "ground_speed_non_negative_pct",
            _pct(not_null_speed - bad_speed, not_null_speed),
            status="INFO",
            detail=f"{bad_speed} negative ground_speed (CHECK constraint enforced)",
        ))

        # Heading range 0–360 — CHECK constraint enforced; INFO only
        bad_heading = _scalar(cur, """
            SELECT COUNT(*) FROM movebank_tbl
            WHERE heading IS NOT NULL AND (heading < 0 OR heading > 360)
        """) or 0
        not_null_heading = _scalar(
            cur, "SELECT COUNT(heading) FROM movebank_tbl") or 1
        report.add(MetricResult(
            "movebank_tbl", "accuracy",
            "heading_in_range_pct",
            _pct(not_null_heading - bad_heading, not_null_heading),
            status="INFO",
            detail=f"{bad_heading} out-of-range (0–360) (CHECK constraint enforced)",
        ))

    for table in _EARTH_ENGINE_TABLE_CONFIGS:
        _add_earth_engine_accuracy_metrics(cur, report, table=table)

    # ── seismic_tbl ────────────────────────────────────────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM seismic_tbl") or 0
    if total > 0:
        # Coordinate range — CHECK constraint enforced; INFO only
        bad_coords = _scalar(cur, """
            SELECT COUNT(*) FROM seismic_tbl
            WHERE latitude < -90 OR latitude > 90
               OR longitude < -180 OR longitude > 180
        """) or 0
        report.add(MetricResult(
            "seismic_tbl", "accuracy",
            "coords_in_wgs84_pct", _pct(total - bad_coords, total),
            status="INFO",
            detail=f"{bad_coords} outside WGS 84 bounds (CHECK constraint enforced)",
        ))

        # Coords in NL bbox (geographic relevance — INFO)
        outside_nl = _scalar(cur, """
            SELECT COUNT(*) FROM seismic_tbl
            WHERE latitude < %s OR latitude > %s
               OR longitude < %s OR longitude > %s
        """, (NL_LAT_MIN, NL_LAT_MAX, NL_LON_MIN, NL_LON_MAX)) or 0
        report.add(MetricResult(
            "seismic_tbl", "accuracy",
            "coords_in_netherlands_pct", _pct(total - outside_nl, total),
            status="INFO", detail=f"{outside_nl} outside NL bbox",
        ))

        # Magnitude range: typically -2 to 10 on Richter scale
        has_mag = _scalar(
            cur, "SELECT COUNT(magnitude) FROM seismic_tbl") or 0
        if has_mag > 0:
            bad_mag = _scalar(cur, """
                SELECT COUNT(*) FROM seismic_tbl
                WHERE magnitude IS NOT NULL
                  AND (magnitude < -2 OR magnitude > 10)
            """) or 0
            report.add(MetricResult(
                "seismic_tbl", "accuracy",
                "magnitude_in_range_pct",
                _pct(has_mag - bad_mag, has_mag), 99.0,
                "PASS" if bad_mag == 0 else "WARN",
                f"{bad_mag} outside [-2, 10] range",
            ))


# ---------------------------------------------------------------------------
# 4. CONSISTENCY
# ---------------------------------------------------------------------------

def _assess_consistency(cur: Any, report: QualityReport) -> None:
    """Cross-field logical checks and cross-table agreement."""

    total_bwd = _scalar(cur, "SELECT COUNT(*) FROM birdweather_tbl") or 0
    if total_bwd > 0:
        valid_station_fk = _scalar(cur, """
            SELECT COUNT(*)
            FROM birdweather_tbl b
            JOIN birdweather_stations_tbl s ON s.station_id = b.station_id
        """) or 0
        station_fk_pct = _pct(valid_station_fk, total_bwd)
        report.add(MetricResult(
            "birdweather_tbl", "consistency",
            "detections_with_valid_station_fk_pct",
            station_fk_pct,
            99.0,
            "PASS" if station_fk_pct >= 99 else "WARN",
            f"{valid_station_fk}/{total_bwd} detections resolve to station rows",
        ))

        valid_species_fk = _scalar(cur, """
            SELECT COUNT(*)
            FROM birdweather_tbl b
            JOIN species_tbl sp ON sp.species_id = b.species_id
        """) or 0
        species_fk_pct = _pct(valid_species_fk, total_bwd)
        report.add(MetricResult(
            "birdweather_tbl", "consistency",
            "detections_with_valid_species_fk_pct",
            species_fk_pct,
            99.0,
            "PASS" if species_fk_pct >= 99 else "WARN",
            f"{valid_species_fk}/{total_bwd} detections resolve to species rows",
        ))

    # ── birdweather: audio_start < audio_end ─────────────────────────
    total = _scalar(cur, """
        SELECT COUNT(*) FROM birdweather_tbl
        WHERE audio_start_time_s IS NOT NULL
          AND audio_end_time_s IS NOT NULL
    """) or 0
    if total > 0:
        bad = _scalar(cur, """
            SELECT COUNT(*) FROM birdweather_tbl
            WHERE audio_start_time_s IS NOT NULL
              AND audio_end_time_s IS NOT NULL
                            AND audio_start_time_s >= audio_end_time_s
        """) or 0
        report.add(MetricResult(
            "birdweather_tbl", "consistency",
            "audio_start_before_end_pct", _pct(total - bad, total), 99.0,
            "PASS" if bad == 0 else "WARN",
                        f"{bad} rows with start >= end",
        ))

    # ── birdweather: detected_at matches observation_date ────────────
    if total_bwd > 0:
        mismatch = _scalar(cur, """
            SELECT COUNT(*) FROM birdweather_tbl
            WHERE observation_date != (detected_at AT TIME ZONE 'UTC')::date
        """) or 0
        report.add(MetricResult(
            "birdweather_tbl", "consistency",
            "date_timestamp_match_pct", _pct(
                total_bwd - mismatch, total_bwd), 95.0,
            "PASS" if _pct(total_bwd - mismatch, total_bwd) >= 95 else "WARN",
            f"{mismatch} date/timestamp mismatches",
        ))

    # ── Cross-table: temporal overlap BW ↔ KNMI ──────────────────────
    bw_days = _scalar(cur, """
        SELECT COUNT(DISTINCT observation_date) FROM birdweather_tbl
    """) or 0
    if bw_days > 0:
        overlap = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT observation_date FROM birdweather_tbl
                INTERSECT
                SELECT DISTINCT observation_date FROM knmi_tbl
            ) sub
        """) or 0
        report.add(MetricResult(
            "knmi_tbl", "consistency",
            "bw_knmi_date_overlap_pct", _pct(overlap, bw_days),
            status="INFO",
            detail=f"{overlap}/{bw_days} BW detection days have KNMI data",
        ))

    # ── Cross-table: species referential integrity BW ↔ Sovon ────────
    # Sovon keys species by English common name. BW carries only species_id,
    # so we resolve through species_tbl (the canonical reference) to recover
    # the common_name set for the species *actually observed* by BirdWeather.
    bw_sp = set(r[0] for r in _rows(cur, """
        SELECT DISTINCT sp.common_name
        FROM birdweather_tbl b
        JOIN species_tbl sp ON sp.species_id = b.species_id
        WHERE sp.common_name IS NOT NULL
    """))
    sovon_sp = set(r[0] for r in _rows(cur, """
        SELECT DISTINCT species_english FROM sovon_tbl
        WHERE species_english IS NOT NULL
    """))
    if bw_sp and sovon_sp:
        overlap = bw_sp & sovon_sp
        report.add(MetricResult(
            "sovon_tbl", "consistency",
            "bw_sovon_species_overlap", len(overlap), status="INFO",
            detail=f"{len(overlap)} species shared between BW ({len(bw_sp)}) and Sovon ({len(sovon_sp)})",
        ))
        # % of Sovon species that exist in BW (referential coverage)
        ref_pct = _pct(len(overlap), len(sovon_sp))
        report.add(MetricResult(
            "sovon_tbl", "consistency",
            "sovon_species_in_bw_pct", ref_pct,
            status="INFO",
            detail=f"{len(overlap)}/{len(sovon_sp)} Sovon species found in BW",
        ))

    # ── ebird: obs_dt date matches observation_date ──────────────────
    ebird_total = _scalar(cur, "SELECT COUNT(*) FROM ebird_tbl") or 0
    if ebird_total > 0:
        mismatch = _scalar(cur, """
            SELECT COUNT(*) FROM ebird_tbl
            WHERE observation_date != (obs_dt AT TIME ZONE 'UTC')::date
        """) or 0
        report.add(MetricResult(
            "ebird_tbl", "consistency",
            "date_timestamp_match_pct",
            _pct(ebird_total - mismatch, ebird_total), 95.0,
            "PASS" if _pct(ebird_total - mismatch,
                           ebird_total) >= 95 else "WARN",
            f"{mismatch} date/timestamp mismatches",
        ))

        # Cross-table: eBird ↔ BW species overlap
        ebird_sp = set(r[0] for r in _rows(cur, """
            SELECT DISTINCT common_name FROM ebird_tbl
            WHERE common_name IS NOT NULL
        """))
        if bw_sp and ebird_sp:
            overlap_eb = bw_sp & ebird_sp
            report.add(MetricResult(
                "ebird_tbl", "consistency",
                "ebird_bw_species_overlap", len(overlap_eb), status="INFO",
                detail=f"{len(overlap_eb)} species shared between eBird ({len(ebird_sp)}) and BW ({len(bw_sp)})",
            ))

        # Cross-table: temporal overlap eBird ↔ BW
        if bw_days > 0:
            ebird_bw_overlap = _scalar(cur, """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT observation_date FROM ebird_tbl
                    INTERSECT
                    SELECT DISTINCT observation_date FROM birdweather_tbl
                ) sub
            """) or 0
            ebird_days = _scalar(cur, """
                SELECT COUNT(DISTINCT observation_date) FROM ebird_tbl
            """) or 1
            report.add(MetricResult(
                "ebird_tbl", "consistency",
                "ebird_bw_date_overlap_pct",
                _pct(ebird_bw_overlap, ebird_days),
                status="INFO",
                detail=f"{ebird_bw_overlap}/{ebird_days} eBird days have BW data",
            ))

    # ── movebank: cross-table temporal overlap with BW ───────────────
    mb_total = _scalar(cur, "SELECT COUNT(*) FROM movebank_tbl") or 0
    if mb_total > 0 and bw_days > 0:
        mb_bw_overlap = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT timestamp::date AS d FROM movebank_tbl
                INTERSECT
                SELECT DISTINCT observation_date FROM birdweather_tbl
            ) sub
        """) or 0
        mb_days = _scalar(cur, """
            SELECT COUNT(DISTINCT timestamp::date) FROM movebank_tbl
        """) or 1
        report.add(MetricResult(
            "movebank_tbl", "consistency",
            "mb_bw_date_overlap_pct",
            _pct(mb_bw_overlap, mb_days),
            status="INFO",
            detail=f"{mb_bw_overlap}/{mb_days} Movebank days have BW data",
        ))

    # ── earth_engine_grid: date overlap with BirdWeather detections ───
    ee_grid_total = 0
    if _table_exists(cur, "google_earth_engine_grid_tbl"):
        ee_grid_total = _scalar(
            cur,
            "SELECT COUNT(*) FROM google_earth_engine_grid_tbl",
        ) or 0
    if ee_grid_total > 0:
        ee_grid_days = _scalar(cur, """
            SELECT COUNT(DISTINCT observation_date)
            FROM google_earth_engine_grid_tbl
        """) or 0
        ee_date_overlap = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT observation_date FROM google_earth_engine_grid_tbl
                INTERSECT
                SELECT DISTINCT observation_date FROM birdweather_tbl
            ) sub
        """) or 0
        if ee_grid_days > 0:
            pct = _pct(ee_date_overlap, ee_grid_days)
            report.add(MetricResult(
                "google_earth_engine_grid_tbl",
                "consistency",
                "date_overlap_with_bw_pct",
                pct,
                status="INFO",
                detail=f"{ee_date_overlap}/{ee_grid_days} grid dates also in BirdWeather",
            ))


# ---------------------------------------------------------------------------
# 5. UNIQUENESS
# ---------------------------------------------------------------------------

def _assess_uniqueness(cur: Any, report: QualityReport) -> None:
    """Duplicate detection on natural keys."""

    # birdweather: detection_id duplicates — enforced by PK; INFO only
    total = _scalar(cur, "SELECT COUNT(*) FROM birdweather_tbl") or 0
    if total > 0:
        distinct = _scalar(cur, """
            SELECT COUNT(DISTINCT detection_id) FROM birdweather_tbl
        """) or 0
        report.add(MetricResult(
            "birdweather_tbl", "uniqueness",
            "detection_id_uniqueness_pct", _pct(distinct, total),
            status="INFO", detail=f"{total - distinct} duplicate detection_ids",
        ))

        # Near-duplicates: same station + species within 3 seconds (sampled for perf)
        near_dup = _scalar(cur, """
            WITH recent AS (
                SELECT station_id, species_id, detected_at
                FROM birdweather_tbl
                WHERE observation_date >= CURRENT_DATE - 14
            )
            SELECT COUNT(*) FROM (
                SELECT detected_at,
                       LAG(detected_at) OVER (
                           PARTITION BY station_id, species_id
                           ORDER BY detected_at
                       ) AS prev_at
                FROM recent
            ) sub
            WHERE prev_at IS NOT NULL
              AND detected_at - prev_at < INTERVAL '3 seconds'
        """) or 0
        recent_total = _scalar(cur, """
            SELECT COUNT(*) FROM birdweather_tbl
            WHERE observation_date >= CURRENT_DATE - 14
        """) or 1
        report.add(MetricResult(
            "birdweather_tbl", "uniqueness",
            "near_duplicate_pct_14d", _pct(near_dup, recent_total), status="INFO",
            detail=f"{near_dup}/{recent_total} detections within 3s (last 14d sample)",
        ))

    # knmi_tbl: natural-key uniqueness — enforced by PK; INFO only
    total = _scalar(cur, "SELECT COUNT(*) FROM knmi_tbl") or 0
    if total > 0:
        distinct = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT station_id, observed_at
                FROM knmi_tbl
            ) sub
        """) or 0
        report.add(MetricResult(
            "knmi_tbl", "uniqueness",
            "natural_key_uniqueness_pct", _pct(distinct, total),
            status="INFO",
            detail=f"{total - distinct} duplicate keys (PK enforced)",
        ))

    # sovon_tbl: species+year+month uniqueness — enforced by UNIQUE; INFO only
    total = _scalar(cur, "SELECT COUNT(*) FROM sovon_tbl") or 0
    if total > 0:
        distinct = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT species_english, year, month
                FROM sovon_tbl
            ) sub
        """) or 0
        report.add(MetricResult(
            "sovon_tbl", "uniqueness",
            "natural_key_uniqueness_pct", _pct(distinct, total),
            status="INFO",
            detail=f"{total - distinct} duplicate keys (UNIQUE enforced)",
        ))

    # ebird_tbl: (sub_id, species_code, obs_dt) PK uniqueness — enforced by PK; INFO only
    total = _scalar(cur, "SELECT COUNT(*) FROM ebird_tbl") or 0
    if total > 0:
        distinct = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT sub_id, species_code, obs_dt
                FROM ebird_tbl
            ) sub
        """) or 0
        report.add(MetricResult(
            "ebird_tbl", "uniqueness",
            "natural_key_uniqueness_pct", _pct(distinct, total),
            status="INFO",
            detail=f"{total - distinct} duplicate keys (PK enforced)",
        ))

    # movebank_tbl: (event_id, study_id, timestamp) PK uniqueness — enforced by PK; INFO only
    total = _scalar(cur, "SELECT COUNT(*) FROM movebank_tbl") or 0
    if total > 0:
        distinct = _scalar(cur, """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT event_id, study_id, timestamp
                FROM movebank_tbl
            ) sub
        """) or 0
        report.add(MetricResult(
            "movebank_tbl", "uniqueness",
            "natural_key_uniqueness_pct", _pct(distinct, total),
            status="INFO",
            detail=f"{total - distinct} duplicate keys (PK enforced)",
        ))

    for table in _EARTH_ENGINE_TABLE_CONFIGS:
        _add_earth_engine_uniqueness_metrics(cur, report, table=table)

    # seismic_tbl: event_id PK uniqueness — enforced by PK; INFO only
    total = _scalar(cur, "SELECT COUNT(*) FROM seismic_tbl") or 0
    if total > 0:
        distinct = _scalar(cur, """
            SELECT COUNT(DISTINCT event_id) FROM seismic_tbl
        """) or 0
        report.add(MetricResult(
            "seismic_tbl", "uniqueness",
            "event_id_uniqueness_pct", _pct(distinct, total),
            status="INFO",
            detail=f"{total - distinct} duplicate event_ids (PK enforced)",
        ))


# ---------------------------------------------------------------------------
# 6. VALIDITY
# ---------------------------------------------------------------------------

def _assess_validity(cur: Any, report: QualityReport) -> None:
    """Domain and constraint conformance checks."""

    # ── sovon: month should stay within the calendar range ───────────
    total = _scalar(cur, "SELECT COUNT(*) FROM sovon_tbl") or 0
    if total > 0:
        # month CHECK 1-12 — enforced by CHECK constraint; INFO only
        bad_month = _scalar(cur, """
            SELECT COUNT(*) FROM sovon_tbl
            WHERE month < 1 OR month > 12
        """) or 0
        report.add(MetricResult(
            "sovon_tbl", "validity",
            "valid_month_pct", _pct(total - bad_month, total),
            status="INFO", detail=f"{bad_month} invalid months",
        ))

    # ── birdweather: station_country distribution — geographic relevance,
    # not data quality; INFO only. station_country lives on
    # birdweather_stations_tbl now; the per-detection table only carries
    # the FK, so we count detections joined to their station's country.
    total = _scalar(cur, """
        SELECT COUNT(*) FROM birdweather_tbl b
        JOIN birdweather_stations_tbl s ON s.station_id = b.station_id
        WHERE s.station_country IS NOT NULL
    """) or 0
    if total > 0:
        nl_ct = _scalar(cur, """
            SELECT COUNT(*) FROM birdweather_tbl b
            JOIN birdweather_stations_tbl s ON s.station_id = b.station_id
            WHERE s.station_country IS NOT NULL
              AND s.station_country IN ('Netherlands', 'NL', 'The Netherlands')
        """) or 0
        report.add(MetricResult(
            "birdweather_tbl", "validity",
            "station_country_netherlands_pct", _pct(nl_ct, total),
            status="INFO", detail=f"{nl_ct}/{total} with NL country",
        ))

    # ── birdweather: eclipse is boolean (not null where present) ─────
    total_eclipse = _scalar(cur, """
        SELECT COUNT(*) FROM birdweather_tbl WHERE eclipse IS NOT NULL
    """) or 0
    if total_eclipse > 0:
        report.add(MetricResult(
            "birdweather_tbl", "validity",
            "eclipse_populated_pct",
            _pct(total_eclipse,
                 _scalar(cur, "SELECT COUNT(*) FROM birdweather_tbl") or 1),
            status="INFO",
            detail=f"{total_eclipse} rows with eclipse flag",
        ))

    # ── knmi: observation_date matches observed_at date ──────────────
    total = _scalar(cur, "SELECT COUNT(*) FROM knmi_tbl") or 0
    if total > 0:
        mismatch = _scalar(cur, """
            SELECT COUNT(*) FROM knmi_tbl
            WHERE observation_date != (observed_at AT TIME ZONE 'UTC')::date
        """) or 0
        report.add(MetricResult(
            "knmi_tbl", "validity",
            "date_timestamp_match_pct", _pct(total - mismatch, total), 95.0,
            "PASS" if _pct(total - mismatch, total) >= 95 else "WARN",
            f"{mismatch} date/timestamp mismatches",
        ))

    # ── ebird: obs_valid distribution ────────────────────────────────
    ebird_total = _scalar(cur, "SELECT COUNT(*) FROM ebird_tbl") or 0
    if ebird_total > 0:
        valid_ct = _scalar(cur, """
            SELECT COUNT(*) FROM ebird_tbl WHERE obs_valid = TRUE
        """) or 0
        report.add(MetricResult(
            "ebird_tbl", "validity",
            "obs_valid_pct", _pct(valid_ct, ebird_total), 80.0,
            "PASS" if _pct(valid_ct, ebird_total) >= 80 else "WARN",
            f"{valid_ct}/{ebird_total} marked valid",
        ))

        # country_code = 'NL' or similar for geographic focus
        nl_ct = _scalar(cur, """
            SELECT COUNT(*) FROM ebird_tbl
            WHERE country_code IN ('NL', 'NLD')
        """) or 0
        report.add(MetricResult(
            "ebird_tbl", "validity",
            "country_code_nl_pct", _pct(nl_ct, ebird_total),
            status="INFO", detail=f"{nl_ct}/{ebird_total} with NL country code",
        ))

    # ── movebank: visible flag distribution ──────────────────────────
    mb_total = _scalar(cur, "SELECT COUNT(*) FROM movebank_tbl") or 0
    if mb_total > 0:
        visible_ct = _scalar(cur, """
            SELECT COUNT(*) FROM movebank_tbl WHERE visible = TRUE
        """) or 0
        report.add(MetricResult(
            "movebank_tbl", "validity",
            "visible_event_pct", _pct(visible_ct, mb_total),
            status="INFO",
            detail=f"{visible_ct}/{mb_total} visible events",
        ))

    for table in _EARTH_ENGINE_TABLE_CONFIGS:
        _add_earth_engine_validity_metrics(cur, report, table=table)

    # ── seismic_tbl: event_time not in future, depth non-negative ────
    se_total = _scalar(cur, "SELECT COUNT(*) FROM seismic_tbl") or 0
    if se_total > 0:
        future_events = _scalar(cur, """
            SELECT COUNT(*) FROM seismic_tbl
            WHERE event_time > NOW() + INTERVAL '1 hour'
        """) or 0
        report.add(MetricResult(
            "seismic_tbl", "validity",
            "event_time_not_future_pct",
            _pct(se_total - future_events, se_total), 99.0,
            "PASS" if future_events == 0 else "FAIL",
            f"{future_events} events with future timestamps",
        ))

        has_depth = _scalar(
            cur, "SELECT COUNT(depth_km) FROM seismic_tbl") or 0
        if has_depth > 0:
            neg_depth = _scalar(cur, """
                SELECT COUNT(*) FROM seismic_tbl
                WHERE depth_km IS NOT NULL AND depth_km < 0
            """) or 0
            report.add(MetricResult(
                "seismic_tbl", "validity",
                "depth_non_negative_pct",
                _pct(has_depth - neg_depth, has_depth), 99.0,
                "PASS" if neg_depth == 0 else "WARN",
                f"{neg_depth} negative depth values",
            ))

    # ── Schema drift: verify all expected columns exist ───────────────
    for table, expected_cols in _EXPECTED_COLUMNS.items():
        actual_cols_rows = _rows(cur, """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
        """, (table,))
        actual_cols = {r[0] for r in actual_cols_rows}
        missing = expected_cols - actual_cols
        extra = actual_cols - expected_cols
        pct = _pct(len(expected_cols) - len(missing), len(expected_cols))
        report.add(MetricResult(
            table, "validity",
            "required_schema_columns_present_pct", pct, 100.0,
            "PASS" if not missing else "FAIL",
            f"missing={sorted(missing)}" if missing else "all required columns present",
        ))
        report.add(MetricResult(
            table, "validity",
            "unexpected_schema_columns_count", len(extra),
            status="INFO",
            detail=f"extra={sorted(extra)}" if extra else "no unexpected columns",
        ))


# ---------------------------------------------------------------------------
# Per-station quality assessment
# ---------------------------------------------------------------------------

@dataclass
class StationMetrics:
    """Quality metrics for a single BirdWeather or KNMI station."""
    source: str                # "birdweather" or "knmi"
    station_id: str
    station_name: str
    lat: float | None = None
    lon: float | None = None
    total_detections: int = 0
    first_date: str = ""
    last_date: str = ""
    days_active: int = 0
    days_span: int = 0
    temporal_coverage_pct: float = 0.0
    species_count: int = 0
    species_diversity: float = 0.0   # Shannon H'
    mean_confidence: float | None = None
    median_confidence: float | None = None
    is_within_nl_bbox: bool | None = None
    audio_metadata_valid_pct: float = 0.0
    processed_clip_pct: float = 0.0
    near_dup_pct: float = 0.0
    days_since_last: int = 0
    # KNMI-specific
    completeness_pct: float = 0.0    # avg non-null rate across key columns
    observation_regularity_pct: float = 0.0
    freshness_status: str = ""       # "active" / "stale" / "dormant"


def _assess_station_quality(cur: Any) -> list[StationMetrics]:
    """Compute per-station quality metrics for BirdWeather and KNMI stations."""
    stations: list[StationMetrics] = []

    # ── BirdWeather stations ─────────────────────────────────────────
    # Reference attributes (station_name, lat, lon) live on
    # birdweather_stations_tbl; we join on the logical FK rather than aggregating
    # them off per-detection rows. species_count is computed from species_id
    # (the canonical FK), not the legacy denormalised common_name.
    bw_rows = _rows(cur, """
        SELECT
            b.station_id,
            COALESCE(s.station_name, '')                AS station_name,
            s.lat                                        AS lat,
            s.lon                                        AS lon,
            COUNT(*)                                     AS total,
            MIN(b.observation_date)                      AS first_date,
            MAX(b.observation_date)                      AS last_date,
            COUNT(DISTINCT b.observation_date)           AS days_active,
            COUNT(DISTINCT b.species_id)                 AS species_count,
            ROUND(AVG(b.confidence)::numeric, 4)         AS mean_conf,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY b.confidence)::numeric, 4) AS med_conf,
            CASE
                WHEN s.lat IS NULL OR s.lon IS NULL THEN NULL
                WHEN s.lat >= %s AND s.lat <= %s
                 AND s.lon >= %s AND s.lon <= %s THEN TRUE
                ELSE FALSE
            END                                          AS is_within_nl_bbox,
            COUNT(*) FILTER (WHERE
                b.audio_url IS NOT NULL
                AND b.audio_start_time_s IS NOT NULL
                AND b.audio_end_time_s IS NOT NULL
                AND b.audio_end_time_s > b.audio_start_time_s
                AND (b.audio_filesize_bytes IS NULL OR b.audio_filesize_bytes > 0)
            )                                            AS audio_metadata_ok,
            COUNT(*) FILTER (WHERE b.has_processed_clip) AS processed_clip_count,
            (CURRENT_DATE - MAX(b.observation_date))     AS days_stale
        FROM birdweather_tbl b
        LEFT JOIN birdweather_stations_tbl s ON s.station_id = b.station_id
        WHERE b.station_id IS NOT NULL
        GROUP BY b.station_id, s.station_name, s.lat, s.lon
        ORDER BY total DESC
    """, (NL_LAT_MIN, NL_LAT_MAX, NL_LON_MIN, NL_LON_MAX))

    # Pre-fetch near-dup counts per station (sampled last 30d for perf)
    dup_map: dict[str, tuple[int, int]] = {}
    dup_rows = _rows(cur, """
        WITH recent AS (
            SELECT station_id, species_id, detected_at
            FROM birdweather_tbl
            WHERE observation_date >= CURRENT_DATE - 30
        ),
        lagged AS (
            SELECT station_id, detected_at,
                   LAG(detected_at) OVER (
                       PARTITION BY station_id, species_id ORDER BY detected_at
                   ) AS prev_at
            FROM recent
        )
        SELECT station_id,
               COUNT(*) FILTER (WHERE prev_at IS NOT NULL
                   AND detected_at - prev_at < INTERVAL '3 seconds') AS n_dup,
               COUNT(*) AS n_total
        FROM lagged
        GROUP BY station_id
    """)
    for r in dup_rows:
        dup_map[r[0]] = (int(r[1]), int(r[2]))

    # Pre-fetch per-station species counts for Shannon diversity. Keys on
    # species_id (canonical FK) — the diversity calculation only needs
    # per-bin counts, not species names.
    sp_counts_rows = _rows(cur, """
        SELECT station_id, species_id, COUNT(*) AS cnt
        FROM birdweather_tbl
        WHERE station_id IS NOT NULL
        GROUP BY station_id, species_id
    """)
    sp_by_station: dict[str, list[int]] = {}
    for r in sp_counts_rows:
        sp_by_station.setdefault(r[0], []).append(int(r[2]))

    for r in bw_rows:
        sid = r[0]
        total = int(r[4])
        days_span = max((r[6] - r[5]).days + 1, 1) if r[5] and r[6] else 1
        stale_days = int(r[14]) if r[14] is not None else 999

        dup_n, dup_total = dup_map.get(sid, (0, 0))
        sp_list = sp_by_station.get(sid, [])

        sm = StationMetrics(
            source="birdweather",
            station_id=sid,
            station_name=r[1] or "",
            lat=float(r[2]) if r[2] else None,
            lon=float(r[3]) if r[3] else None,
            total_detections=total,
            first_date=str(r[5]) if r[5] else "",
            last_date=str(r[6]) if r[6] else "",
            days_active=int(r[7]),
            days_span=days_span,
            temporal_coverage_pct=round(100.0 * int(r[7]) / days_span, 1),
            species_count=int(r[8]),
            species_diversity=_shannon_diversity(sp_list),
            mean_confidence=float(r[9]) if r[9] is not None else None,
            median_confidence=float(r[10]) if r[10] is not None else None,
            is_within_nl_bbox=bool(r[11]) if r[11] is not None else None,
            audio_metadata_valid_pct=_pct(int(r[12]), total),
            processed_clip_pct=_pct(int(r[13]), total),
            near_dup_pct=_pct(dup_n, dup_total) if dup_total else 0.0,
            days_since_last=stale_days,
            freshness_status=(
                "active" if stale_days <= 3
                else ("stale" if stale_days <= 14 else "dormant")
            ),
        )
        stations.append(sm)

    # ── KNMI stations ────────────────────────────────────────────────
    knmi_rows = _rows(cur, """
        SELECT
            station_id,
            MAX(station_name)                           AS station_name,
            MAX(station_lat)                            AS lat,
            MAX(station_lon)                            AS lon,
            COUNT(*)                                    AS total,
            MIN(observation_date)                       AS first_date,
            MAX(observation_date)                       AS last_date,
            COUNT(DISTINCT observation_date)             AS days_active,
            ROUND((100.0 * (
                COUNT(temperature_c)::float +
                COUNT(relative_humidity_pct)::float +
                COUNT(wind_speed_ms)::float +
                COUNT(pressure_hpa)::float
            ) / (COUNT(*) * 4.0))::numeric, 1)          AS comp_pct,
            (CURRENT_DATE - MAX(observation_date))       AS days_stale
        FROM knmi_tbl
        WHERE station_id IS NOT NULL
        GROUP BY station_id
        ORDER BY total DESC
    """)

    for r in knmi_rows:
        total = int(r[4])
        days_span = max((r[6] - r[5]).days + 1, 1) if r[5] and r[6] else 1
        stale_days = int(r[9]) if r[9] is not None else 999

        sm = StationMetrics(
            source="knmi",
            station_id=r[0],
            station_name=r[1] or "",
            lat=float(r[2]) if r[2] else None,
            lon=float(r[3]) if r[3] else None,
            total_detections=total,
            first_date=str(r[5]) if r[5] else "",
            last_date=str(r[6]) if r[6] else "",
            days_active=int(r[7]),
            days_span=days_span,
            temporal_coverage_pct=round(100.0 * int(r[7]) / days_span, 1),
            completeness_pct=float(r[8]) if r[8] else 0.0,
            days_since_last=stale_days,
            freshness_status=(
                "active" if stale_days <= 1
                else ("stale" if stale_days <= 7 else "dormant")
            ),
        )
        stations.append(sm)

    return stations


def _export_station_csv(
    station_metrics: list[StationMetrics], output_dir: Path,
) -> Path:
    """Write per-station quality CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    path = output_dir / "data_quality_stations.csv"

    fields = [
        "generated_at", "source", "station_id", "station_name",
        "lat", "lon", "total_detections", "first_date", "last_date",
        "days_active", "days_span", "temporal_coverage_pct",
        "species_count", "species_diversity", "mean_confidence",
        "median_confidence", "is_within_nl_bbox",
        "audio_metadata_valid_pct", "processed_clip_pct", "near_dup_pct",
        "days_since_last",
        "completeness_pct", "freshness_status",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for sm in station_metrics:
            row = {
                "generated_at": now_str,
                "source": sm.source,
                "station_id": sm.station_id,
                "station_name": sm.station_name,
                "lat": sm.lat if sm.lat is not None else "",
                "lon": sm.lon if sm.lon is not None else "",
                "total_detections": sm.total_detections,
                "first_date": sm.first_date,
                "last_date": sm.last_date,
                "days_active": sm.days_active,
                "days_span": sm.days_span,
                "temporal_coverage_pct": sm.temporal_coverage_pct,
                "species_count": sm.species_count,
                "species_diversity": sm.species_diversity,
                "mean_confidence": sm.mean_confidence if sm.mean_confidence is not None else "",
                "median_confidence": sm.median_confidence if sm.median_confidence is not None else "",
                "is_within_nl_bbox": (
                    sm.is_within_nl_bbox if sm.is_within_nl_bbox is not None else ""
                ),
                "audio_metadata_valid_pct": sm.audio_metadata_valid_pct,
                "processed_clip_pct": sm.processed_clip_pct,
                "near_dup_pct": sm.near_dup_pct,
                "days_since_last": sm.days_since_last,
                "completeness_pct": sm.completeness_pct,
                "freshness_status": sm.freshness_status,
            }
            w.writerow(row)

    logger.info("Stations → %s (%d stations)", path, len(station_metrics))
    return path


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def _export_csvs(report: QualityReport, output_dir: Path) -> tuple[Path, Path, Path]:
    """Write summary, detail, and issue CSVs."""
    output_dir.mkdir(parents=True, exist_ok=True)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    dimensions = ["completeness", "timeliness", "accuracy",
                  "consistency", "uniqueness", "validity"]

    # ── Summary CSV ──────────────────────────────────────────────────
    summary_path = output_dir / "data_quality_summary.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["generated_at", "table"] +
                   dimensions + ["composite_score"])
        for table in TABLES:
            row = [now_str, table]
            for dim in dimensions:
                row.append(report.dimension_score(table, dim))
            row.append(report.composite_score(table))
            w.writerow(row)
    logger.info("Summary  → %s", summary_path)

    # ── Details CSV ──────────────────────────────────────────────────
    details_path = output_dir / "data_quality_details.csv"
    with open(details_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["generated_at", "table", "dimension", "metric",
                    "value", "threshold", "status", "detail"])
        for m in report.metrics:
            w.writerow([
                now_str, m.table, m.dimension, m.metric,
                m.value,
                m.threshold if m.threshold is not None else "",
                m.status, m.detail,
            ])
    logger.info("Details  → %s", details_path)

    # ── Issues CSV (FAIL + WARN only) ────────────────────────────────
    issues_path = output_dir / "data_quality_issues.csv"
    issues = [m for m in report.metrics if m.status in ("FAIL", "WARN")]
    with open(issues_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["generated_at", "severity", "table", "dimension",
                    "metric", "value", "threshold", "detail"])
        for m in issues:
            w.writerow([
                now_str, m.status, m.table, m.dimension, m.metric,
                m.value,
                m.threshold if m.threshold is not None else "",
                m.detail,
            ])
    logger.info("Issues   → %s (%d issues)", issues_path, len(issues))

    return summary_path, details_path, issues_path


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------

def _print_report(report: QualityReport, tables: list[str] | None = None) -> None:
    """Render a compact terminal summary."""
    dims = ["completeness", "timeliness", "accuracy",
            "consistency", "uniqueness", "validity"]
    show_tables = tables or TABLES
    w = 100  # line width

    print("\n" + "━" * w)
    print("  DATA QUALITY REPORT")
    print("━" * w)

    header = f"  {'Table':<28}" + \
        "".join(f"{'  ' + d[:4].upper():>8}" for d in dims) + "  SCORE"
    print(header)
    print("  " + "─" * (w - 2))

    for table in show_tables:
        cells = f"  {table:<28}"
        for dim in dims:
            s = report.dimension_score(table, dim)
            indicator = "✓" if s >= 90 else (
                "~" if s >= 70 else ("✗" if s > 0 else "-"))
            cells += f"  {s:5.1f}{indicator} "
        cells += f"  {report.composite_score(table):5.1f}"
        print(cells)

    print("  " + "─" * (w - 2))

    # Highlight failures and warnings
    issues = [m for m in report.metrics
              if m.status in ("FAIL", "WARN") and m.table in show_tables]
    if issues:
        print(f"\n  Issues ({len(issues)}):")
        for m in issues:
            icon = "✗" if m.status == "FAIL" else "~"
            detail = f"  ({m.detail})" if m.detail else ""
            print(f"    {icon} [{m.table}] {m.metric}: {m.value}{detail}")
    else:
        print("\n  No issues detected.")

    # Total metric counts
    all_metrics = [m for m in report.metrics if m.table in show_tables]
    n_pass = sum(1 for m in all_metrics if m.status == "PASS")
    n_warn = sum(1 for m in all_metrics if m.status == "WARN")
    n_fail = sum(1 for m in all_metrics if m.status == "FAIL")
    n_info = sum(1 for m in all_metrics if m.status == "INFO")
    print(f"\n  Totals: {len(all_metrics)} metrics — "
          f"✓ {n_pass} PASS  ~ {n_warn} WARN  ✗ {n_fail} FAIL  ℹ {n_info} INFO")

    print("━" * w + "\n")


# ---------------------------------------------------------------------------
# Programmatic entry points (used by Dagster flow and CLI)
# ---------------------------------------------------------------------------

def run_audit(
    pool: ConnectionPool,
    species_file: Path | None = None,
) -> tuple[QualityReport, list[StationMetrics]]:
    """Execute the full quality audit against the live database.

    Args:
        pool: An open ``ConnectionPool`` instance.
        species_file: Optional JSON config with target species list.

    Returns:
        Tuple of ``(report, station_metrics)``.
    """
    global TARGET_SPECIES  # noqa: PLW0603
    reg = load_registry(species_file)
    TARGET_SPECIES = reg.english_names

    report = QualityReport()
    station_metrics: list[StationMetrics] = []

    assessors = [
        ("Completeness", _assess_completeness),
        ("Timeliness",   _assess_timeliness),
        ("Accuracy",     _assess_accuracy),
        ("Consistency",  _assess_consistency),
        ("Uniqueness",   _assess_uniqueness),
        ("Validity",     _assess_validity),
    ]

    with pool.cursor() as cur:
        for name, fn in assessors:
            logger.info("Assessing %s …", name)
            try:
                fn(cur, report)
            except Exception:
                logger.exception("Error during %s assessment", name)
                cur.connection.rollback()

        logger.info("Assessing per-station quality …")
        try:
            station_metrics = _assess_station_quality(cur)
        except Exception:
            logger.exception("Error during station quality assessment")
            cur.connection.rollback()

    return report, station_metrics


def store_results(
    pool: ConnectionPool,
    report: QualityReport,
    station_metrics: list[StationMetrics],
) -> int:
    """Persist audit results to ``data_quality_tbl``.

    Writes two kinds of rows:
      - ``summary``: one row with run-level metadata (composite score, counts)
      - ``metric``:  one row per individual quality check

    Per-station quality is computed by ``refresh_station_summary()`` in SQL
    and stored directly in ``birdweather_stations_tbl``.

    Args:
        pool: An open ``ConnectionPool`` instance.
        report: Completed quality report.
        station_metrics: Per-station metrics (used for summary counts only).

    Returns:
        The ``run_id`` of the newly created audit run.
    """
    from psycopg2.extras import execute_values

    started_at = datetime.now(timezone.utc)

    fail_count = sum(1 for m in report.metrics if m.status == "FAIL")
    warn_count = sum(1 for m in report.metrics if m.status == "WARN")

    table_scores = [
        report.composite_score(t) for t in TABLES
        if report.composite_score(t) > 0
    ]
    composite = (
        round(sum(table_scores) / len(table_scores), 2)
        if table_scores else 0.0
    )

    config = {
        "target_species": TARGET_SPECIES,
        "dimension_weights": DIMENSION_WEIGHTS,
        "tables": TABLES,
    }

    with pool.cursor() as cur:
        # ── Allocate a run_id from the shared sequence ──────────────
        cur.execute("SELECT nextval('data_quality_run_id_seq')")
        run_id: int = cur.fetchone()[0]

        # ── Summary row ─────────────────────────────────────────────
        cur.execute("""
            INSERT INTO data_quality_tbl
                (run_id, kind, status, score, detail)
            VALUES (%s, 'summary', 'completed', %s, %s)
        """, (
            run_id,
            composite,
            json.dumps({
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "config_json": config,
                "metric_count": len(report.metrics),
                "station_count": len(station_metrics),
                "fail_count": fail_count,
                "warn_count": warn_count,
            }),
        ))

        # ── Metric rows (bulk) ──────────────────────────────────────
        if report.metrics:
            execute_values(cur, """
                INSERT INTO data_quality_tbl
                    (run_id, kind, status, table_name, dimension,
                     metric, value, score, detail)
                VALUES %s
            """, [
                (run_id, "metric", m.status, m.table, m.dimension,
                 m.metric, m.value, m.score(),
                 json.dumps({"threshold": m.threshold, "detail": m.detail}))
                for m in report.metrics
            ])

        # Station quality is now computed by refresh_station_summary()
        # and stored in birdweather_stations_tbl — no separate DB rows needed.

        cur.connection.commit()

    logger.info(
        "Stored quality run #%d (%d metrics, %d stations, score=%.1f)",
        run_id, len(report.metrics), len(station_metrics), composite,
    )
    return run_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Run full data-quality audit, store to DB, and export CSVs.

    Returns:
        Exit code: 0 on success, 1 on quality-gate failure or FAIL-level issues.
    """
    parser = argparse.ArgumentParser(description="Data-quality audit")
    parser.add_argument(
        "-o", "--output", default="data/quality",
        help="Output directory for CSV reports (default: data/quality)",
    )
    parser.add_argument(
        "-t", "--table", action="append", dest="tables",
        choices=TABLES,
        help="Limit display and quality gate to specific table(s). Can be repeated.",
    )
    parser.add_argument(
        "--fail-under", type=float, default=0.0, metavar="SCORE",
        help="Exit non-zero if any table's composite score < SCORE (0–100).",
    )
    parser.add_argument(
        "--species-file", type=Path, default=None, metavar="PATH",
        help="Path to a JSON file with target species list. "
             "Defaults to config/target_species.json.",
    )
    parser.add_argument(
        "--no-store", action="store_true",
        help="Skip writing results to the data_quality_* tables.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    load_runtime_env()
    output_dir = Path(args.output)

    # ── Connect ──────────────────────────────────────────────────────
    logger.info("Connecting to TimescaleDB …")
    try:
        pool = ConnectionPool(min_conn=1, max_conn=3)
    except Exception as exc:
        logger.error("Cannot connect to database: %s", exc)
        return 1

    # ── Audit ────────────────────────────────────────────────────────
    try:
        report, station_metrics = run_audit(pool, args.species_file)

        # ── Store to DB (unless --no-store) ──────────────────────────
        if not args.no_store:
            try:
                run_id = store_results(pool, report, station_metrics)
                logger.info("Results stored in database (run_id=%d)", run_id)
            except Exception:
                logger.exception(
                    "Failed to store results — continuing with CSV export")

        # ── Export CSVs ──────────────────────────────────────────────
        _export_csvs(report, output_dir)
        if station_metrics:
            _export_station_csv(station_metrics, output_dir)
    finally:
        pool.close()

    # ── Console output ───────────────────────────────────────────────
    _print_report(report, tables=args.tables)
    print(f"  CSV reports saved to: {output_dir.resolve()}\n")

    # ── Quality gate ─────────────────────────────────────────────────
    if args.fail_under > 0:
        check_tables = args.tables or TABLES
        for table in check_tables:
            score = report.composite_score(table)
            if score < args.fail_under:
                logger.warning(
                    "Quality gate FAILED: %s score %.1f < %.1f",
                    table, score, args.fail_under,
                )
                return 1

    # Exit non-zero if any FAIL-level issues exist
    if any(m.status == "FAIL" for m in report.metrics):
        return 1

    return 0
