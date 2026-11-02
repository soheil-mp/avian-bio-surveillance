-- =============================================================================
-- Domain baseline DDL for canonical species reference.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: species_tbl (Derived regular table)
-- =========================================================================
-- Purpose: Authoritative canonical species reference attributes.
-- Grain  : One row per species (BirdWeather species_id).
-- PK     : species_id
-- Source : BirdWeather API, PhyloPic, and internal aggregations.
-- Refresh: Metadata seeded from BirdWeather API; detection envelope
--          updated by refresh_species_dim().
-- Notes  : Request-shaped dashboard caches intentionally live outside the
--          canonical species dimension. The frontend now derives tracker
--          payloads directly from source tables / analytical tables instead
--          of storing raw API payload blobs on species rows.
-- =========================================================================

CREATE TABLE IF NOT EXISTS species_tbl (

    species_id TEXT PRIMARY KEY,

    -- Names
    common_name TEXT NOT NULL,
    scientific_name TEXT,
    alpha_code TEXT,
    alpha6_code TEXT,
    ebird_code TEXT,

    -- Display attributes
    species_color TEXT,
    species_image_url TEXT,
    species_thumbnail_url TEXT,
    birdweather_url TEXT,
    ebird_url TEXT,
    ml_url TEXT,
    wikipedia_url TEXT,
    wikipedia_summary TEXT,
    prediction_area JSONB,
    range_map JSONB,

    -- Activity envelope
    first_seen TIMESTAMPTZ,
    last_seen TIMESTAMPTZ,
    total_detections BIGINT NOT NULL DEFAULT 0,
    station_count INTEGER NOT NULL DEFAULT 0,

    -- Cached PhyloPic silhouette lookup
    phylopic_svg_url TEXT,
    phylopic_thumb_url TEXT,
    phylopic_attribution TEXT NOT NULL DEFAULT '',
    phylopic_uuid TEXT NOT NULL DEFAULT '',
    phylopic_fetched_at TIMESTAMPTZ
);

-- =========================================================================
-- Indexes
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Support for case-insensitive search interfaces
CREATE INDEX IF NOT EXISTS idx_species_tbl_common_name_ci
    ON species_tbl ((lower(common_name)));

CREATE INDEX IF NOT EXISTS idx_species_tbl_scientific_name_ci
    ON species_tbl ((lower(scientific_name)))
    WHERE scientific_name IS NOT null;

COMMENT ON TABLE species_tbl IS
    'Canonical species dimension: API metadata, activity envelope, and cached PhyloPic assets.';

COMMIT;
