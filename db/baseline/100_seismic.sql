-- =============================================================================
-- Domain baseline DDL for KNMI earthquake catalog.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: seismic_tbl (Regular table)
-- =========================================================================
-- Purpose: Earthquake catalogue events.
-- Grain  : One row per earthquake event.
-- PK     : event_id
-- Source : KNMI FDSN Event API.
-- Model  : SeismicEvent (db/models/knmi.py)
-- Loader : upsert_seismic_events (db/loaders/knmi.py)
--
-- Notes:
--   * Value-range validation deferred to the data_quality_tbl pipeline.
-- =========================================================================

CREATE TABLE IF NOT EXISTS seismic_tbl (

    -- Identity & Time
    event_id TEXT PRIMARY KEY,
    event_time TIMESTAMPTZ NOT NULL,

    -- Location
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    region TEXT,

    -- Seismic attributes 
    depth_km DOUBLE PRECISION,
    magnitude DOUBLE PRECISION,
    magnitude_type TEXT,
    description TEXT,

    attributes JSONB NOT NULL DEFAULT '{}'
);

-- =========================================================================
-- Indexes
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Time-axis scans (recent events, time-bound joins)
CREATE INDEX IF NOT EXISTS idx_set_event_time
    ON seismic_tbl (event_time);

COMMENT ON TABLE seismic_tbl IS
    'KNMI FDSN earthquake catalogue events with coordinates and raw attributes.';

COMMIT;
