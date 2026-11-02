-- V008: Lean bioacoustic-primary weekly anomaly table.
--
-- Replaces, for the delivered single-scoring system, the run_id-scoped
-- modeling_species_week_scores_tbl (+ features, clusters, and the
-- modeling_runs_tbl "latest run" machinery). One canonical scoring, one
-- table, bioacoustic embedding drift only. Fusion (IsolationForest) and the
-- population baseline are dropped: the 2-year retro_full_2024_2025_v3
-- evaluation showed fusion adds no lift and population is redundant with (and
-- weaker than) bioacoustic drift.
--
-- This migration is ADDITIVE. It does not touch the existing modeling tables;
-- those are dropped in a later phase once the pipeline and frontend have moved
-- over (see docs/refactor/lean_bioacoustic_migration.md).

CREATE TABLE IF NOT EXISTS species_week_anomaly_tbl (
    anomaly_row_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    species_id          TEXT NOT NULL,
    species_english     TEXT,
    location_type       TEXT NOT NULL DEFAULT 'grid',
    location_id         TEXT NOT NULL,
    grid_cell_id        TEXT,
    period_start        DATE NOT NULL,
    period_end          DATE,
    drift_score         DOUBLE PRECISION,        -- embedding drift; the primary signal
    outlier_share       DOUBLE PRECISION,        -- share of clips beyond the drift baseline
    clip_count          INTEGER,                 -- embedding clips scored in the cell-week
    alert_level         TEXT,                    -- per-species drift quantiles: amber>=p95, red>=p99
    top_detection_ids   TEXT [] NOT NULL DEFAULT '{}',
    payload             JSONB NOT NULL DEFAULT '{}',  -- slim per-row context (mortality/env)
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (species_id, location_id, period_start)   -- one canonical row per species-cell-week
);

CREATE INDEX IF NOT EXISTS idx_swa_species_period
    ON species_week_anomaly_tbl (species_id, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_swa_cell_period
    ON species_week_anomaly_tbl (grid_cell_id, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_swa_alert
    ON species_week_anomaly_tbl (alert_level, drift_score DESC);
CREATE INDEX IF NOT EXISTS idx_swa_payload_gin
    ON species_week_anomaly_tbl USING gin (payload jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_swa_top_detection_ids_gin
    ON species_week_anomaly_tbl USING gin (top_detection_ids);
