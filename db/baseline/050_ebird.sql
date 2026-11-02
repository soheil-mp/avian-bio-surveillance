-- =============================================================================
-- Domain baseline DDL for eBird observations.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: ebird_tbl (TimescaleDB hypertable, 1-day chunks)
-- =========================================================================
-- Purpose: Citizen-science bird sightings sourced from eBird checklists.
-- Grain  : One row per species per checklist submission per observation time.
-- PK     : (sub_id, species_code, obs_dt)
-- Source : eBird API 2.0 (/data/obs endpoints)
-- Model  : EBirdObservation (db/models/ebird.py)
-- Loader : upsert_ebird_observations (db/loaders/ebird.py)
--
-- Notes:
--   * Hotspot attributes are denormalized inline; no separate ebird_hotspot table.
--   * Value-range validation deferred to the data_quality_tbl pipeline.
--   * No compression or retention policies defined during dev phase.
-- =========================================================================

CREATE TABLE IF NOT EXISTS ebird_tbl (

    -- Identity
    sub_id TEXT NOT NULL,
    observation_date DATE NOT NULL,
    obs_dt TIMESTAMPTZ NOT NULL,

    -- Species
    species_code TEXT NOT NULL,
    common_name TEXT NOT NULL,
    scientific_name TEXT,

    -- Count
    how_many INTEGER,
    presence_noted BOOLEAN NOT NULL DEFAULT false,

    -- Location (WGS 84)
    loc_id TEXT NOT NULL,
    loc_name TEXT,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    location_private BOOLEAN NOT NULL DEFAULT false,

    -- Review status
    obs_valid BOOLEAN NOT NULL DEFAULT true,
    obs_reviewed BOOLEAN NOT NULL DEFAULT false,

    -- Region hierarchy
    subnational2_code TEXT,
    subnational2_name TEXT,
    subnational1_code TEXT,
    subnational1_name TEXT,
    country_code TEXT,
    country_name TEXT,

    -- Observer
    user_display_name TEXT,

    -- Checklist metadata
    checklist_id TEXT,
    has_comments BOOLEAN,
    has_rich_media BOOLEAN,

    -- Hotspot reference (merged inline — replaces ebird_hotspot table)
    num_species_all_time INTEGER,
    is_hotspot BOOLEAN,

    PRIMARY KEY (sub_id, species_code, obs_dt)
);

SELECT create_hypertable(
    'ebird_tbl', 'obs_dt',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => true
);

-- =========================================================================
-- Indexes
-- Time-range scans depend on TimescaleDB chunk pruning on obs_dt.
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Per-species timeseries (species tracker, comparison views)
CREATE INDEX IF NOT EXISTS idx_ebt_species_code
    ON ebird_tbl (species_code, obs_dt DESC);

-- Per-location timeseries (hotspot drill-downs)
CREATE INDEX IF NOT EXISTS idx_ebt_loc_id
    ON ebird_tbl (loc_id, obs_dt DESC);

-- Dashboard freshness and coverage scans across all checklists
CREATE INDEX IF NOT EXISTS idx_ebt_obs_dt_desc
    ON ebird_tbl (obs_dt DESC);

-- Country filter (NL-only views, cross-source overlap checks)
CREATE INDEX IF NOT EXISTS idx_ebt_country_code
    ON ebird_tbl (country_code)
    WHERE country_code IS NOT null;

COMMENT ON TABLE ebird_tbl IS
    'eBird checklist observations at species-by-checklist granularity, '
    'with hotspot metadata denormalized inline.';

COMMIT;
