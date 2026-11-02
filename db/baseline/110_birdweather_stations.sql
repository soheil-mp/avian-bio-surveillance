-- =============================================================================
-- Domain baseline DDL for BirdWeather station references.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: birdweather_stations_tbl (Regular table)
-- =========================================================================
-- Purpose: Canonical BirdWeather station table (reference metadata + stats).
-- Grain  : One row per BirdWeather station.
-- PK     : station_id
-- Source : BirdWeather API + calculated metrics.
-- Refresh: Metadata via pipeline loader; stats via refresh_station_summary().
--
-- Notes:
--   * Logical FK target for birdweather_tbl.station_id.
--   * Uses INSERT ... ON CONFLICT to bridge meta/stat load timing.
--   * lifetime_availability_pct captures long-run station consistency:
--     100 * unique_days / (CURRENT_DATE - first_detection + 1).
--     Using today as the right boundary penalises stations that have gone
--     quiet and rewards those that have contributed consistently over time.
--   * availability_90d_pct captures recent active-day coverage across the
--     trailing 90-day window.
--   * days_since_last provides a coarse freshness signal for the UI.
-- =========================================================================

CREATE TABLE IF NOT EXISTS birdweather_stations_tbl (

    -- Identity
    station_id TEXT PRIMARY KEY,
    station_name TEXT,
    station_type TEXT,

    -- Location
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    station_continent TEXT,
    station_country TEXT,
    station_state TEXT,
    station_location TEXT,
    station_timezone TEXT,

    -- Analysis metrics
    total_detections INT NOT NULL DEFAULT 0,
    top_species JSONB NOT NULL DEFAULT '[]',
    unique_species INT NOT NULL DEFAULT 0,
    avg_confidence NUMERIC(5, 4),
    high_confidence_detections INT NOT NULL DEFAULT 0,
    first_detection DATE,
    last_detection DATE,
    days_since_last INT,
    unique_days INT NOT NULL DEFAULT 0,
    monthly_detections JSONB NOT NULL DEFAULT '{}',

    -- Quality metrics
    lifetime_availability_pct DOUBLE PRECISION, -- 100 * unique_days / inclusive span from first detection to today.
    availability_90d_pct DOUBLE PRECISION, -- 100 * active days in trailing 90-day window / 90, capped at 100.

    -- Precomputed Google Earth Engine enrichment (static cell mapping + slow-
    -- moving per-cell features) so /api/stations is a single-table read.
    -- Populated by refresh_station_gee_enrichment(); see db/routines/060.
    grid_cell_id TEXT,                    -- fn_point_to_cell(lat, lon, 'nl_geodesic_5km')
    gee_ndvi_90d_mean DOUBLE PRECISION,   -- ROUND(AVG(ndvi), 3) over trailing 90 days
    gee_elevation_m_mean DOUBLE PRECISION,-- ROUND(AVG(elevation_m), 0) over trailing 90 days
    gee_land_cover_label TEXT             -- latest non-null esa_worldcover_label for the cell
);

COMMENT ON TABLE birdweather_stations_tbl IS
    'Unified BirdWeather station table. Metadata seeded from public API; '
    'detection summaries, freshness, lifetime availability, and recent '
    '90-day availability updated by refresh_station_summary().';

-- =========================================================================

-- Frontend map and dashboard scans
CREATE INDEX IF NOT EXISTS idx_bwst_lat_lon
    ON birdweather_stations_tbl (lat, lon)
    WHERE lat IS NOT null AND lon IS NOT null;

CREATE INDEX IF NOT EXISTS idx_bwst_last_detection_desc
    ON birdweather_stations_tbl (last_detection DESC NULLS LAST);

COMMIT;
