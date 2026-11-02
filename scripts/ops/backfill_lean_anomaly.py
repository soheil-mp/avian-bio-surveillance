#!/usr/bin/env python
"""Backfill species_week_anomaly_tbl (lean, bioacoustic-primary) from a run.

Phase 1 of the lean refactor (docs/refactor/lean_bioacoustic_migration.md):
projects the bioacoustic signal out of the run_id-scoped
modeling_species_week_scores_tbl into the single canonical lean table.

ALERTING (interim; finalised in Phase 3). The 2-year data shows
embedding_drift_score is near-binary/saturated -- a mass at 0 and most non-zero
values pinned near 1.0 (median 0.965 among drift>0), so it cannot grade alerts.
outlier_share (fraction of clips beyond the drift baseline) is the graded
magnitude, so alerts threshold on it, with a minimum clip count so a single
anomalous clip in a sparse cell-week (outlier_share=1.0 noise) cannot fire.
Fixed thresholds (not per-species quantiles): a share is directly comparable
across species, and per-species quantiles also saturate (p99=1.0 everywhere).

Non-destructive w.r.t. the old tables; only writes species_week_anomaly_tbl.

Usage:
    .venv/bin/python scripts/ops/backfill_lean_anomaly.py --run-id retro_full_2024_2025_v3 --truncate
"""
from __future__ import annotations

import argparse

import psycopg2.extras

from avian_biosurveillance.db.database import ConnectionPool

MIN_ALERT_CLIPS = 5     # below this a cell-week's outlier_share is too noisy to trust
AMBER_SHARE = 0.50      # >= this fraction of clips beyond baseline -> amber
RED_SHARE = 0.90        # >= this -> red

SELECT_SQL = """
    SELECT species_id, species_english, location_type, location_id, grid_cell_id,
           period_start, period_end,
           embedding_drift_score, embedding_outlier_share, embedding_clip_count_scored,
           top_detection_ids, payload
    FROM modeling_species_week_scores_tbl
    WHERE run_id = %s
    ORDER BY species_id, location_id, period_start
"""

INSERT_SQL = """
    INSERT INTO species_week_anomaly_tbl
        (species_id, species_english, location_type, location_id, grid_cell_id,
         period_start, period_end, drift_score, outlier_share, clip_count,
         alert_level, top_detection_ids, payload)
    VALUES %s
    ON CONFLICT (species_id, location_id, period_start) DO UPDATE SET
        species_english = EXCLUDED.species_english,
        grid_cell_id    = EXCLUDED.grid_cell_id,
        period_end      = EXCLUDED.period_end,
        drift_score     = EXCLUDED.drift_score,
        outlier_share   = EXCLUDED.outlier_share,
        clip_count      = EXCLUDED.clip_count,
        alert_level     = EXCLUDED.alert_level,
        top_detection_ids = EXCLUDED.top_detection_ids,
        payload         = EXCLUDED.payload,
        computed_at     = NOW()
"""


def _alert(outlier_share: float | None, clip_count: int | None) -> str:
    if clip_count is None or clip_count < MIN_ALERT_CLIPS or outlier_share is None:
        return "green"
    if outlier_share >= RED_SHARE:
        return "red"
    if outlier_share >= AMBER_SHARE:
        return "amber"
    return "green"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--truncate", action="store_true",
                    help="empty species_week_anomaly_tbl before loading")
    args = ap.parse_args()

    pool = ConnectionPool(min_conn=1, max_conn=2)
    try:
        with pool.connection() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(SELECT_SQL, (args.run_id,))
            rows = cur.fetchall()
        print(f"loaded {len(rows)} source rows from run {args.run_id}")

        values = []
        counts = {"green": 0, "amber": 0, "red": 0}
        skipped = 0
        for r in rows:
            loc_id = r["location_id"] or r["grid_cell_id"]
            if not loc_id or r["species_id"] is None or r["period_start"] is None:
                skipped += 1
                continue
            level = _alert(r["embedding_outlier_share"], r["embedding_clip_count_scored"])
            counts[level] += 1
            values.append((
                r["species_id"], r["species_english"], r["location_type"] or "grid",
                loc_id, r["grid_cell_id"], r["period_start"], r["period_end"],
                r["embedding_drift_score"], r["embedding_outlier_share"],
                r["embedding_clip_count_scored"], level, r["top_detection_ids"],
                psycopg2.extras.Json(r["payload"] or {}),
            ))

        with pool.connection() as conn, conn.cursor() as cur:
            if args.truncate:
                cur.execute("TRUNCATE species_week_anomaly_tbl")
            psycopg2.extras.execute_values(cur, INSERT_SQL, values, page_size=2000)
            conn.commit()
        print(f"inserted/updated {len(values)} rows (skipped {skipped})  alerts={counts}")
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
