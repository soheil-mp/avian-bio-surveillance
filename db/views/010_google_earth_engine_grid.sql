-- =========================================================================
-- VIEW: google_earth_engine_grid_tbl
-- =========================================================================
-- Backward-compatible wide view over ``google_earth_engine_tbl``
-- joined with ``grid_cells_tbl`` for geometry. Consumers that expect the old
-- shape (lat/lon/bbox/grid_size_km inline) read from this view. All writes
-- go to the facts table.
--
-- Uses ``SELECT f.*`` so any column added to the facts table automatically
-- flows through to this view. Because ``CREATE OR REPLACE VIEW`` cannot
-- change the column set (PG only allows it when the new column list extends
-- the old one in-place), we DROP + CREATE.
--
-- WARNING: ``DROP VIEW`` will fail if any *other* view, materialised view, or
-- stored procedure depends on this one. Today only application code (Python /
-- TypeScript) reads this view, so it is safe. If you ever add a
-- dependent SQL object on top of it, switch to ``CREATE OR REPLACE`` (and
-- accept the column-set restriction) or use ``DROP ... CASCADE`` knowingly.
-- Object grants should be re-applied from ``grants/010_role_grants.sql`` if a
-- future multi-role deployment adds non-owner readers.
-- =========================================================================

BEGIN;

DROP VIEW IF EXISTS google_earth_engine_grid_tbl;

CREATE VIEW google_earth_engine_grid_tbl AS
-- noqa: disable=AM04
SELECT
    f.*,
    g.grid_system,
    g.lat,
    g.lon,
    g.bbox_min_lat,
    g.bbox_min_lon,
    g.bbox_max_lat,
    g.bbox_max_lon,
    g.grid_size_km,
    g.cell_geojson
FROM google_earth_engine_tbl AS f
LEFT JOIN grid_cells_tbl AS g ON f.grid_cell_id = g.grid_cell_id;
-- noqa: enable=AM04

COMMENT ON VIEW google_earth_engine_grid_tbl IS
    'Backward-compatible view of google_earth_engine_tbl joined '
    'with grid_cells_tbl for geometry. All writes go to the facts table.';

-- The current local/dev stack uses one owning login role, so
-- ``grants/010_role_grants.sql`` is intentionally a no-op today.

COMMIT;
