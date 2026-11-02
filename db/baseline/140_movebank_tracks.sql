-- =============================================================================
-- Domain baseline DDL for Movebank migration tracks.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: movebank_tracks_tbl (Derived regular table)
-- =========================================================================
-- Purpose: Pre-decimated per-individual GPS tracks for fast API retrieval.
-- Grain  : One row per (study_id, individual_key).
-- PK     : (study_id, individual_key)
-- Refresh: refresh_movebank_tracks()
--
-- Notes:
--   * Stores ordered track_points JSON for each decimated fix.
--   * Keeps raw extent and endpoint summaries alongside the decimated track.
--   * Eliminates heavy windowed queries to movebank_tbl on /api/migration-tracks.
-- =========================================================================

CREATE TABLE IF NOT EXISTS movebank_tracks_tbl (

    study_id BIGINT NOT NULL,
    species TEXT,
    individual_key TEXT NOT NULL,

    -- Pre-decimated trajectory (<= 500 points)
    raw_point_count INTEGER NOT NULL,
    point_count INTEGER NOT NULL,
    bbox JSONB NOT NULL,
    start_ts TIMESTAMPTZ NOT NULL,
    start_point JSONB NOT NULL,
    end_ts TIMESTAMPTZ NOT NULL,
    end_point JSONB NOT NULL,
    track_points JSONB NOT NULL,

    PRIMARY KEY (study_id, individual_key)
);

-- =========================================================================
-- Indexes
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Support for species filtering
CREATE INDEX IF NOT EXISTS idx_movebank_tracks_species
    ON movebank_tracks_tbl (species)
    WHERE species IS NOT null;

COMMENT ON TABLE movebank_tracks_tbl IS
    'Pre-decimated per-individual GPS tracks with raw counts, bbox, and '
    'start/end summaries. '
    'Rebuilt by refresh_movebank_tracks().';

-- =========================================================================

COMMIT;
