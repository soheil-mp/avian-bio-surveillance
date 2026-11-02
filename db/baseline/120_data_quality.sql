-- =============================================================================
-- Domain baseline DDL for data quality audits.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: data_quality_tbl (Regular table)
-- =========================================================================
-- Purpose: Audit log for dataset-level quality checks.
-- Grain  : One row per audit run (summary) or individual check (metric).
-- PK     : id
--
-- Notes:
--   * kind = 'summary': Audit run metadata (counts, composite score, config).
--   * kind = 'metric': Individual check results (value, threshold, status).
--   * Per-station quality is maintained separately in birdweather_stations_tbl.
--   * detail JSONB captures variable-schema fields per 'kind' discriminator.
-- =========================================================================

CREATE SEQUENCE IF NOT EXISTS data_quality_run_id_seq;

CREATE TABLE IF NOT EXISTS data_quality_tbl (

    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    run_id BIGINT NOT NULL DEFAULT nextval('data_quality_run_id_seq'),
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',

    table_name TEXT,                   -- metric: source table
    dimension TEXT,                   -- metric: quality dimension
    metric TEXT,                   -- metric: metric name
    value DOUBLE PRECISION,       -- metric: measured value  -- noqa: RF04
    score DOUBLE PRECISION,       -- metric / summary: score or composite score

    detail JSONB NOT NULL DEFAULT '{}',

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE data_quality_tbl IS
    'Run-level and metric-level quality audit records. Station quality lives in birdweather_stations_tbl.';

-- =========================================================================
-- Indexes
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Covers run_id filters via leading prefix.

CREATE INDEX IF NOT EXISTS idx_dqt_run_kind
    ON data_quality_tbl (run_id, kind);

CREATE INDEX IF NOT EXISTS idx_dqt_summary_status
    ON data_quality_tbl (status, run_id DESC)
    WHERE kind = 'summary';

CREATE INDEX IF NOT EXISTS idx_dqt_created_at_desc
    ON data_quality_tbl (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_dqt_metric_table_dim
    ON data_quality_tbl (table_name, dimension)
    WHERE kind = 'metric';

CREATE INDEX IF NOT EXISTS idx_dqt_metric_status
    ON data_quality_tbl (status)
    WHERE kind = 'metric' AND status IN ('FAIL', 'WARN');

COMMIT;
