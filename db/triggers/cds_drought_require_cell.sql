-- =========================================================================
-- TRIGGER + FUNCTION: cds_drought_require_cell
-- =========================================================================
-- One self-contained deploy unit for the cds_drought integrity guard.
--
-- The trigger function enforces alignment between ``cds_drought_tbl`` and
-- ``grid_cells_tbl``. TimescaleDB refuses ADD FOREIGN KEY on a compressed
-- hypertable, so this trigger provides defence-in-depth around the
-- ingester's upsert flow.
--
-- Checks performed on every INSERT / UPDATE:
--   1.  grid_cell_id + grid_system exist in grid_cells_tbl.
--   2.  Denormalised geometry on the new row matches grid_cells_tbl
--       (lat / lon / bbox). Any drift raises before the row lands, so
--       ``cds_drought_tbl`` can never disagree with the canonical cell
--       registry.
--
-- Idempotent: safe to re-run on every container boot.
-- =========================================================================

BEGIN;

CREATE OR REPLACE FUNCTION trg_cds_drought_require_cell()
RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_lat          DOUBLE PRECISION;
    v_lon          DOUBLE PRECISION;
    v_bbox_min_lat DOUBLE PRECISION;
    v_bbox_min_lon DOUBLE PRECISION;
    v_bbox_max_lat DOUBLE PRECISION;
    v_bbox_max_lon DOUBLE PRECISION;
BEGIN
    SELECT lat, lon, bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon
      INTO v_lat, v_lon, v_bbox_min_lat, v_bbox_min_lon,
           v_bbox_max_lat, v_bbox_max_lon
      FROM grid_cells_tbl
     WHERE grid_cell_id = NEW.grid_cell_id
       AND grid_system  = NEW.grid_system;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'cds_drought_tbl: grid_cell_id=% missing from grid_cells_tbl for grid_system=%',
            NEW.grid_cell_id, NEW.grid_system;
    END IF;

    IF NEW.lat          IS DISTINCT FROM v_lat
       OR NEW.lon          IS DISTINCT FROM v_lon
       OR NEW.bbox_min_lat IS DISTINCT FROM v_bbox_min_lat
       OR NEW.bbox_min_lon IS DISTINCT FROM v_bbox_min_lon
       OR NEW.bbox_max_lat IS DISTINCT FROM v_bbox_max_lat
       OR NEW.bbox_max_lon IS DISTINCT FROM v_bbox_max_lon
    THEN
        RAISE EXCEPTION
            'cds_drought_tbl: geometry drift for grid_cell_id=% (grid_system=%). '
            'row=(lat=%, lon=%, bbox=[% % % %]) '
            'registry=(lat=%, lon=%, bbox=[% % % %])',
            NEW.grid_cell_id, NEW.grid_system,
            NEW.lat, NEW.lon,
            NEW.bbox_min_lat, NEW.bbox_min_lon,
            NEW.bbox_max_lat, NEW.bbox_max_lon,
            v_lat, v_lon,
            v_bbox_min_lat, v_bbox_min_lon,
            v_bbox_max_lat, v_bbox_max_lon;
    END IF;

    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_cdsd_require_cell ON cds_drought_tbl;
CREATE TRIGGER trg_cdsd_require_cell
    BEFORE INSERT OR UPDATE ON cds_drought_tbl
    FOR EACH ROW EXECUTE FUNCTION trg_cds_drought_require_cell();

COMMENT ON FUNCTION trg_cds_drought_require_cell() IS
    'Enforces grid_cell_id existence + denormalised-geometry consistency '
    'between cds_drought_tbl and grid_cells_tbl. '
    'Bound by trg_cdsd_require_cell.';

COMMIT;
