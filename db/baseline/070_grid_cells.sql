-- =============================================================================
-- Domain baseline DDL for canonical grid cells.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: grid_cells_tbl (Regular table)
-- =========================================================================
-- Purpose: Authoritative registry of canonical grid cells.
-- Grain  : One row per canonical grid cell.
-- PK     : grid_cell_id
--
-- Notes:
--   * Stores centroid, bounding box, and polygon GeoJSON for downstream consumers.
--   * Value-range validation deferred to the data_quality_tbl pipeline.
--   * grid_row and grid_col are NULL for grids that do not encode row/col coordinates.
-- =========================================================================

CREATE TABLE IF NOT EXISTS grid_cells_tbl (

    -- Identity
    grid_cell_id TEXT PRIMARY KEY,

    -- Geometry
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    bbox_min_lat DOUBLE PRECISION NOT NULL,
    bbox_min_lon DOUBLE PRECISION NOT NULL,
    bbox_max_lat DOUBLE PRECISION NOT NULL,
    bbox_max_lon DOUBLE PRECISION NOT NULL,
    grid_size_km DOUBLE PRECISION NOT NULL,
    cell_geojson JSONB NOT NULL,

    -- Grid System Metadata
    grid_system TEXT NOT NULL,
    source TEXT,

    -- Row/col coordinates for indexed grid systems (enables hierarchical roll-ups)
    grid_row INTEGER,
    grid_col INTEGER
);

-- =========================================================================
-- Indexes
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Grid-system filter
CREATE INDEX IF NOT EXISTS idx_gct_grid_system
    ON grid_cells_tbl (grid_system);

-- Hierarchical row/col roll-ups
CREATE INDEX IF NOT EXISTS idx_gct_system_row_col
    ON grid_cells_tbl (grid_system, grid_row, grid_col)
    WHERE grid_row IS NOT null AND grid_col IS NOT null;

COMMENT ON TABLE grid_cells_tbl IS
    'Canonical grid-cell registry: centroid, bounding box, and GeoJSON geometry.';
COMMENT ON COLUMN grid_cells_tbl.grid_row IS
    'Row index for row/col-style grids (e.g., nl_geodesic_5km). NULL otherwise.';
COMMENT ON COLUMN grid_cells_tbl.grid_col IS
    'Column index for row/col-style grids. NULL otherwise.';

COMMIT;
