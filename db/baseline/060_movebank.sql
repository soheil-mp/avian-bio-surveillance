-- =============================================================================
-- Domain baseline DDL for Movebank telemetry.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: movebank_tbl (TimescaleDB hypertable, 7-day chunks)
-- =========================================================================
-- Purpose: GPS telemetry fact table for tracked animals.
-- Grain  : One row per GPS fix (event) per individual per study.
-- PK     : (event_id, study_id, timestamp)
-- Source : Movebank REST API
-- Model  : MovebankEvent (db/models/movebank.py)
-- Loader : upsert_movebank_events (db/loaders/movebank.py)
--
-- Notes:
--   * Individual, tag, and deployment attributes are denormalized inline.
--   * Only fields populated by the public studies or used by live features
--     are kept in the dev-phase canonical schema.
--   * Value-range validation deferred to the data_quality_tbl pipeline.
--   * No compression or retention policies defined during dev phase.
-- =========================================================================

CREATE TABLE IF NOT EXISTS movebank_tbl (

    -- Identity & Time
    event_id BIGINT NOT NULL,
    study_id BIGINT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL, -- noqa: RF04

    -- Individual
    individual_id BIGINT,
    individual_local_identifier TEXT,
    individual_taxon_canonical_name TEXT,

    -- Tag / Deployment
    tag_id BIGINT,
    tag_local_identifier TEXT,
    deployment_id BIGINT,

    -- Sensor
    sensor_type_id BIGINT,

    -- Location (WGS 84)
    location_lat DOUBLE PRECISION,
    location_long DOUBLE PRECISION,

    -- Kinematics
    ground_speed DOUBLE PRECISION,
    heading DOUBLE PRECISION,
    height_above_ellipsoid DOUBLE PRECISION,

    -- Quality / Outlier flag
    visible BOOLEAN,

    -- Individual reference (merged from movebank_individual)
    individual_sex TEXT,
    individual_life_stage TEXT,

    -- Tag reference (merged from movebank_tag)
    tag_manufacturer_name TEXT,
    tag_model TEXT,
    tag_serial_number TEXT,

    -- Deployment reference (merged from movebank_deployment)
    deployment_local_identifier TEXT,
    deploy_on_timestamp TIMESTAMPTZ,
    deploy_off_timestamp TIMESTAMPTZ,
    deploy_on_latitude DOUBLE PRECISION,
    deploy_on_longitude DOUBLE PRECISION,
    deployment_comments TEXT,
    location_accuracy_comments TEXT,
    manipulation_type TEXT,
    study_site TEXT,
    tag_readout_method TEXT,
    alt_project_id TEXT,
    attachment_type TEXT,
    animal_mass_g DOUBLE PRECISION,

    -- Event-level update / quality flags
    update_ts TIMESTAMPTZ,
    import_marked_outlier BOOLEAN,
    event_comments TEXT,

    -- Individual (animal) reference
    individual_comments TEXT,
    individual_ring_id TEXT,
    individual_number_of_events BIGINT,
    individual_number_of_deployments INTEGER,
    individual_taxon_detail TEXT,

    -- Tag reference (extended)
    tag_comments TEXT,

    -- Extended altitude & barometry (sensor-agnostic)
    height_above_msl_m DOUBLE PRECISION,
    barometric_pressure_pa DOUBLE PRECISION,

    -- Environmental sensors on the tag
    external_temperature_c DOUBLE PRECISION,

    PRIMARY KEY (event_id, study_id, timestamp)
);

SELECT create_hypertable(
    'movebank_tbl', 'timestamp',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => true
);

-- =========================================================================
-- Indexes
-- Time-range scans depend on TimescaleDB chunk pruning on timestamp.
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Per-study queries
CREATE INDEX IF NOT EXISTS idx_mbt_study_id
    ON movebank_tbl (study_id, timestamp DESC);

-- Per-individual track reconstruction
CREATE INDEX IF NOT EXISTS idx_mbt_individual_id
    ON movebank_tbl (individual_id, timestamp DESC)
    WHERE individual_id IS NOT null;

-- Per-species filters
CREATE INDEX IF NOT EXISTS idx_mbt_species
    ON movebank_tbl (individual_taxon_canonical_name)
    WHERE individual_taxon_canonical_name IS NOT null;

-- Frontend overlay windows over located fixes
CREATE INDEX IF NOT EXISTS idx_mbt_located_timestamp_desc
    ON movebank_tbl (timestamp DESC)
    WHERE location_lat IS NOT null AND location_long IS NOT null;

CREATE INDEX IF NOT EXISTS idx_mbt_species_located_timestamp_desc
    ON movebank_tbl (individual_taxon_canonical_name, timestamp DESC)
    WHERE individual_taxon_canonical_name IS NOT null
      AND location_lat IS NOT null
      AND location_long IS NOT null;

CREATE INDEX IF NOT EXISTS idx_mbt_deployment_origin_timestamp_desc
    ON movebank_tbl (timestamp DESC)
    WHERE deploy_on_latitude IS NOT null
      AND deploy_on_longitude IS NOT null
      AND location_lat IS NOT null
      AND location_long IS NOT null;

COMMENT ON TABLE movebank_tbl IS
    'Movebank GPS telemetry with denormalized individual, tag, and deployment '
    'attributes for analytical joins.';

COMMIT;
