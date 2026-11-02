-- =========================================================================
-- VIEW: cds_drought_v
-- =========================================================================
-- Enriches ``cds_drought_tbl`` rows with the canonical cell polygon from
-- ``grid_cells_tbl``. Convenience view for map rendering / export.
-- =========================================================================

BEGIN;

CREATE OR REPLACE VIEW cds_drought_v AS
-- noqa: disable=AM04
SELECT
    f.*,
    g.cell_geojson
FROM cds_drought_tbl AS f
INNER JOIN grid_cells_tbl AS g ON f.grid_cell_id = g.grid_cell_id;
-- noqa: enable=AM04

COMMENT ON VIEW cds_drought_v IS
    'cds_drought_tbl enriched with grid-cell polygon from grid_cells_tbl.';

-- The current local/dev stack uses one owning login role. Add non-owner grants
-- in ``grants/010_role_grants.sql`` if a future deployment needs them.

COMMIT;
