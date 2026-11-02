-- =========================================================================
-- FUNCTION: refresh_station_gee_enrichment()
-- =========================================================================
-- Precomputes the per-station Google Earth Engine enrichment that the
-- /api/stations dashboard route previously derived live on every cache miss
-- (a 765-station fn_point_to_cell pass + two GEE aggregations joined per
-- request). The station -> grid-cell mapping is static and the GEE features
-- are slow-moving, so materialising them onto birdweather_stations_tbl lets
-- the route become a single-table read.
--
-- The aggregation below is byte-identical to the former route query:
--   * grid_cell_id        : fn_point_to_cell(lat, lon, 'nl_geodesic_5km')
--   * gee_ndvi_90d_mean   : ROUND(AVG(ndvi), 3) over the trailing 90 days,
--                           anchored on MAX(observation_date) across the table
--   * gee_elevation_m_mean: ROUND(AVG(elevation_m), 0) over the same window
--   * gee_land_cover_label: latest non-null esa_worldcover_label per cell
--
-- Refresh: invoked by refresh_station_summary_task() (Dagster
-- recording_station_summary asset) so it tracks the station-summary cadence.
-- =========================================================================

BEGIN;

CREATE OR REPLACE FUNCTION refresh_station_gee_enrichment()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    WITH station_cells AS (
        SELECT rs.station_id,
               fn_point_to_cell(rs.lat, rs.lon, 'nl_geodesic_5km') AS grid_cell_id
        FROM birdweather_stations_tbl rs
        WHERE rs.lat IS NOT NULL AND rs.lon IS NOT NULL
    ),
    cells AS (
        SELECT DISTINCT grid_cell_id
        FROM station_cells
        WHERE grid_cell_id IS NOT NULL
    ),
    gee_agg AS (
        SELECT gee.grid_cell_id,
               ROUND(AVG(gee.ndvi)::numeric, 3)::double precision        AS ndvi_mean,
               ROUND(AVG(gee.elevation_m)::numeric, 0)::double precision AS elevation_mean
        FROM google_earth_engine_tbl gee
        JOIN cells ON cells.grid_cell_id = gee.grid_cell_id
        WHERE gee.observation_date
                  >= (SELECT MAX(observation_date) FROM google_earth_engine_tbl)
                     - INTERVAL '90 days'
        GROUP BY gee.grid_cell_id
    ),
    gee_latest AS (
        SELECT DISTINCT ON (gee.grid_cell_id)
               gee.grid_cell_id,
               gee.esa_worldcover_label
        FROM google_earth_engine_tbl gee
        JOIN cells ON cells.grid_cell_id = gee.grid_cell_id
        WHERE gee.esa_worldcover_label IS NOT NULL
        ORDER BY gee.grid_cell_id, gee.observation_date DESC
    ),
    enriched AS (
        SELECT sc.station_id,
               sc.grid_cell_id,
               ga.ndvi_mean,
               ga.elevation_mean,
               gl.esa_worldcover_label
        FROM station_cells sc
        LEFT JOIN gee_agg    ga ON ga.grid_cell_id = sc.grid_cell_id
        LEFT JOIN gee_latest gl ON gl.grid_cell_id = sc.grid_cell_id
    )
    UPDATE birdweather_stations_tbl rs
    SET grid_cell_id         = e.grid_cell_id,
        gee_ndvi_90d_mean    = e.ndvi_mean,
        gee_elevation_m_mean = e.elevation_mean,
        gee_land_cover_label = e.esa_worldcover_label
    FROM enriched e
    WHERE rs.station_id = e.station_id;
END;
$$;

COMMENT ON FUNCTION refresh_station_gee_enrichment() IS
    'Materialises the static station -> grid-cell mapping and per-cell GEE '
    'features (90-day NDVI mean, elevation mean, latest ESA WorldCover label) '
    'onto birdweather_stations_tbl so /api/stations is a single-table read. '
    'Aggregation matches the former live route query exactly.';

COMMIT;
