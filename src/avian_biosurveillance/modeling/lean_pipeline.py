"""Lean, bioacoustic-primary anomaly scoring (the delivered/simplified path).

See docs/refactor/lean_bioacoustic_migration.md. Computes ONLY the bioacoustic
embedding-drift signal and writes the single canonical species_week_anomaly_tbl:
no IsolationForest fusion, no population baseline, no embedding clusters, no
scene control, no run_id.

It reuses ``pipeline._compute_embedding_stage_streamed`` verbatim -- the exact
drift computation the old pipeline used -- so for any given source/output window
the per-(species, cell, week) drift values are identical to the old pipeline's
``embedding_drift_score``. The only new logic here is the column mapping and the
bioacoustic alert rule, which the parity check validates.

Alert rule (interim; see migration doc): drift_score is near-binary/saturated,
so alerts threshold on outlier_share (the graded magnitude) with a minimum clip
guardrail -- amber >= 0.50, red >= 0.90, only when clip_count >= 5.

CLI:
    .venv/bin/python -m avian_biosurveillance.modeling.lean_pipeline \
        --source-start 2024-01-01 --source-end 2025-12-31 \
        --output-start 2024-01-01 --output-end 2025-12-31
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter

import pandas as pd
import psycopg2.extras
import psycopg2.sql

from avian_biosurveillance.config.species import registry as species_registry
from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.modeling.embedding_drift import EmbeddingDriftConfig
from avian_biosurveillance.modeling.pipeline import (
    _coerce_date,
    _compute_embedding_stage_streamed,
    _effective_target_species_ids,
)
from avian_biosurveillance.modeling.settings import SETTINGS

logger = logging.getLogger(__name__)

MIN_ALERT_CLIPS = 5
AMBER_SHARE = 0.50
RED_SHARE = 0.90

_PAYLOAD_KEYS = (
    "embedding_baseline_size",
    "embedding_baseline_level",
    "embedding_backend_clip_counts",
    "embedding_backend_scored_clip_counts",
    "embedding_primary_backend",
    "top_audio_artifact_paths",
    "background_noise_summary",
    "audio_quality_flag",
)

_INSERT_COLS = (
    "species_id", "species_english", "location_type", "location_id",
    "grid_cell_id", "period_start", "period_end", "drift_score", "outlier_share",
    "clip_count", "alert_level", "top_detection_ids", "payload",
)


@dataclass
class LeanScoringStats:
    rows_scored: int
    alerts_amber: int
    alerts_red: int
    alerts_green: int
    species: int


def _alert(outlier_share: float | None, clip_count: int | None) -> str:
    if clip_count is None or clip_count < MIN_ALERT_CLIPS or outlier_share is None:
        return "green"
    if outlier_share >= RED_SHARE:
        return "red"
    if outlier_share >= AMBER_SHARE:
        return "amber"
    return "green"


def _slim_payload(row: dict) -> dict:
    return {k: row[k] for k in _PAYLOAD_KEYS if row.get(k) is not None}


def _drift_config() -> EmbeddingDriftConfig:
    return EmbeddingDriftConfig(
        seasonal_window_weeks=SETTINGS.feature_set["seasonal_window_weeks"],
        min_baseline_clips=SETTINGS.embedding_drift["min_baseline_clips"],
        min_week_clips=SETTINGS.embedding_drift["min_week_clips"],
        k_neighbors=SETTINGS.embedding_drift["k_neighbors"],
        min_confidence=SETTINGS.embedding_drift["min_confidence"],
    )


def run_bioacoustic_anomaly_scoring(
    *,
    source_start_date,
    source_end_date,
    output_period_start,
    output_period_end,
    species_ids=None,
    target_species_only: bool = True,
    table: str = "species_week_anomaly_tbl",
    truncate: bool = False,
    pool: ConnectionPool | None = None,
) -> LeanScoringStats:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise ValueError(f"unsafe table name: {table!r}")
    own_pool = pool is None
    pool = pool or ConnectionPool(min_conn=1, max_conn=3)
    started = perf_counter()
    src_s, src_e = _coerce_date(source_start_date), _coerce_date(source_end_date)
    out_s, out_e = _coerce_date(output_period_start), _coerce_date(output_period_end)
    species = _effective_target_species_ids(
        target_species_only=target_species_only, species_ids=species_ids
    )
    try:
        embedding_weekly, _, _, _ = _compute_embedding_stage_streamed(
            pool,
            selected_species_ids=species,
            config=_drift_config(),
            embedding_min_confidence=SETTINGS.embedding_drift["min_confidence"],
            include_embedding_clusters=False,
            include_embedding_control=False,
            embedding_min_control_species=1,
            source_start=src_s,
            source_end=src_e,
            output_start=out_s,
            output_end=out_e,
        )
        if embedding_weekly.empty:
            logger.warning("lean scoring produced no drift rows.")
            return LeanScoringStats(0, 0, 0, 0, 0)

        english = species_registry.english_by_birdweather_id
        counts = {"green": 0, "amber": 0, "red": 0}
        values: list[tuple] = []
        for r in embedding_weekly.to_dict("records"):
            loc = r.get("location_id")
            if not loc or r.get("species_id") is None or r.get("period_start") is None:
                continue
            ps = pd.to_datetime(r["period_start"]).date()
            level = _alert(r.get("embedding_outlier_share"),
                           r.get("embedding_clip_count_scored"))
            counts[level] += 1
            top = r.get("top_detection_ids") or []
            if not isinstance(top, list):
                top = list(top)
            values.append((
                str(r["species_id"]), english.get(str(r["species_id"])),
                r.get("location_type") or "grid", str(loc), str(loc),
                ps, ps + timedelta(days=6),
                r.get("embedding_drift_score"), r.get("embedding_outlier_share"),
                r.get("embedding_clip_count_scored"), level, top,
                psycopg2.extras.Json(_slim_payload(r)),
            ))

        insert_sql = psycopg2.sql.SQL(
            "INSERT INTO {tbl} ({cols}) VALUES %s "
            "ON CONFLICT (species_id, location_id, period_start) DO UPDATE SET "
            "species_english=EXCLUDED.species_english, grid_cell_id=EXCLUDED.grid_cell_id, "
            "period_end=EXCLUDED.period_end, drift_score=EXCLUDED.drift_score, "
            "outlier_share=EXCLUDED.outlier_share, clip_count=EXCLUDED.clip_count, "
            "alert_level=EXCLUDED.alert_level, top_detection_ids=EXCLUDED.top_detection_ids, "
            "payload=EXCLUDED.payload, computed_at=NOW()"
        ).format(
            tbl=psycopg2.sql.Identifier(table),
            cols=psycopg2.sql.SQL(", ").join(map(psycopg2.sql.Identifier, _INSERT_COLS)),
        )
        with pool.connection() as conn, conn.cursor() as cur:
            if truncate:
                cur.execute(psycopg2.sql.SQL("TRUNCATE {}").format(psycopg2.sql.Identifier(table)))
            psycopg2.extras.execute_values(cur, insert_sql, values, page_size=2000)
            conn.commit()

        stats = LeanScoringStats(
            rows_scored=len(values), alerts_amber=counts["amber"],
            alerts_red=counts["red"], alerts_green=counts["green"],
            species=int(embedding_weekly["species_id"].nunique()),
        )
        logger.info(
            "lean bioacoustic scoring: %d rows (%d species) into %s in %.0fs "
            "alerts(a/r/g)=%d/%d/%d",
            stats.rows_scored, stats.species, table, perf_counter() - started,
            stats.alerts_amber, stats.alerts_red, stats.alerts_green,
        )
        return stats
    finally:
        if own_pool:
            pool.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="Lean bioacoustic-primary scoring")
    ap.add_argument("--source-start", required=True)
    ap.add_argument("--source-end", required=True)
    ap.add_argument("--output-start", required=True)
    ap.add_argument("--output-end", required=True)
    ap.add_argument("--species", nargs="*", default=None,
                    help="birdweather ids; default = all target species")
    ap.add_argument("--table", default="species_week_anomaly_tbl")
    ap.add_argument("--truncate", action="store_true")
    args = ap.parse_args()
    stats = run_bioacoustic_anomaly_scoring(
        source_start_date=args.source_start, source_end_date=args.source_end,
        output_period_start=args.output_start, output_period_end=args.output_end,
        species_ids=args.species, target_species_only=args.species is None,
        table=args.table, truncate=args.truncate,
    )
    print(f"DONE rows={stats.rows_scored} species={stats.species} "
          f"alerts(a/r/g)={stats.alerts_amber}/{stats.alerts_red}/{stats.alerts_green}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
