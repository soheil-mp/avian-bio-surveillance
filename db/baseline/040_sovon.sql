-- =============================================================================
-- Domain baseline DDL for Sovon mortality counts.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: sovon_tbl (Regular table)
-- =========================================================================
-- Purpose: Mortality baseline counts from Sovon dead-bird portal charts.
-- Grain  : One row per species x year x month.
-- Source : Sovon portal charts (counts extracted via Gemini Vision).
-- Loader : upsert_sovon_mortality (db/loaders/sovon.py)
--
-- Notes:
--   * Value-range validation deferred to the data_quality_tbl pipeline.
--   * Generated observation_date is the first day of the represented month.
-- =========================================================================

CREATE TABLE IF NOT EXISTS sovon_tbl (

    -- Identity & Timestamp
    mortality_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_date DATE GENERATED ALWAYS AS (
                            make_date(year, month, 1)
                            ) STORED,
    year INTEGER NOT NULL, -- noqa: RF04
    month INTEGER NOT NULL, -- noqa: RF04

    -- Species Metadata
    species_english TEXT NOT NULL,
    species_dutch TEXT,
    sovon_species_id INTEGER,

    -- Mortality & Source Data
    dead_bird_count INTEGER NOT NULL DEFAULT 0,
    n_total INTEGER,
    source_image TEXT,

    CONSTRAINT uq_sovon_species_year_month
        UNIQUE (species_english, year, month)
);

-- =========================================================================
-- Indexes
-- The unique constraint supports the per-species drill-down path.
-- An additional index on observation_date supports time-axis scans.
-- =========================================================================

CREATE INDEX IF NOT EXISTS idx_svt_observation_date
    ON sovon_tbl (observation_date);

COMMENT ON TABLE sovon_tbl IS
    'Monthly Sovon dead-bird mortality counts by species, '
    'used as an epidemiological baseline.';

COMMIT;
