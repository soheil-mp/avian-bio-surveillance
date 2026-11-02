-- =========================================================================
-- FUNCTION: fn_point_to_cell(lat, lon, grid_system)
-- =========================================================================
-- Bins an arbitrary (lat, lon) to the containing grid_cell_id for any
-- registered grid. Uses the bbox index on ``grid_cells_tbl`` (O(log N)).
-- =========================================================================

CREATE OR REPLACE FUNCTION fn_point_to_cell(
    p_lat DOUBLE PRECISION,
    p_lon DOUBLE PRECISION,
    p_grid_system TEXT
) RETURNS TEXT
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT grid_cell_id
    FROM   grid_cells_tbl
    WHERE  grid_system = p_grid_system
      AND  p_lat BETWEEN bbox_min_lat AND bbox_max_lat
      AND  p_lon BETWEEN bbox_min_lon AND bbox_max_lon
    LIMIT 1;
$$;

COMMENT ON FUNCTION fn_point_to_cell IS
    'Return the grid_cell_id a point falls into for the named grid_system. '
    'Uses the bbox index on grid_cells_tbl (O(log N)).';
