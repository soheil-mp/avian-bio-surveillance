-- =============================================================================
-- Domain baseline DDL for KNMI weather observations.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: knmi_tbl (TimescaleDB hypertable, 1-day chunks)
-- =========================================================================
-- Purpose: KNMI station observations used as 10-minute weather covariates.
-- Grain  : One row per KNMI station per 10-minute observation.
-- PK     : (station_id, observed_at)
-- Model  : KNMIWeatherObservation (db/models/knmi.py)
-- Loader : upsert_knmi_observations (db/loaders/knmi.py)
--
-- Notes:
--   * Derived daily rollups in knmi_daily_cagg_tbl.
--   * Value-range validation deferred to the data_quality_tbl pipeline.
--   * No compression or retention policies defined during dev phase.
-- =========================================================================

CREATE TABLE IF NOT EXISTS knmi_tbl (

    -- Identity & Timestamp
    observed_at TIMESTAMPTZ NOT NULL,
    observation_date DATE NOT NULL,

    -- Station Metadata
    station_id TEXT NOT NULL,
    station_name TEXT,
    station_lat DOUBLE PRECISION,
    station_lon DOUBLE PRECISION,
    station_elevation_m REAL,

    -- Measurements (KNMI parameter codes)
    temperature_c REAL,                           -- ta (°C)
    relative_humidity_pct REAL,                           -- rh (%)
    wind_speed_ms REAL,                           -- ff (m/s)
    wind_direction_deg REAL,                           -- dd (°)
    pressure_hpa REAL,                           -- pp (hPa)
    precipitation_mm REAL,                           -- R1H (mm)
    solar_radiation_wm2 REAL,                           -- qg (W/m²)
    cloud_cover_okta REAL,                           -- n (okta; 9 = obscured)

    PRIMARY KEY (station_id, observed_at)
);

SELECT create_hypertable(
    'knmi_tbl',
    'observed_at',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => true
);

-- =========================================================================
-- Indexes
-- The PK (station_id, observed_at) supports per-station queries.
-- Dashboard freshness and coverage scans also need a leading timestamp index.
-- =========================================================================

CREATE INDEX IF NOT EXISTS idx_knt_observed_at_desc
    ON knmi_tbl (observed_at DESC);

COMMENT ON TABLE knmi_tbl IS
    'KNMI 10-minute weather observations keyed by station and timestamp.';

COMMIT;
