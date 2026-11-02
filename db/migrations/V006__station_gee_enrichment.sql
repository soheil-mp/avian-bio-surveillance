-- Materialise the per-station Google Earth Engine enrichment that
-- /api/stations previously computed live on every cache miss (static
-- station -> grid-cell mapping + two GEE aggregations joined per request,
-- ~0.5s cold and prone to thundering-herd amplification under concurrency).
--
-- The baseline DDL in db/baseline/110_birdweather_stations.sql now carries the
-- same column list for fresh installs; this migration mirrors it for any
-- existing database created before the baseline change landed. The matching
-- routine lives in db/routines/060_refresh_station_gee.sql and is (re)applied
-- on bootstrap; population happens via refresh_station_gee_enrichment(),
-- invoked by refresh_station_summary_task().
--
-- All adds are idempotent (ADD COLUMN IF NOT EXISTS) and nullable.

ALTER TABLE birdweather_stations_tbl
    ADD COLUMN IF NOT EXISTS grid_cell_id         TEXT,
    ADD COLUMN IF NOT EXISTS gee_ndvi_90d_mean    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS gee_elevation_m_mean DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS gee_land_cover_label TEXT;
