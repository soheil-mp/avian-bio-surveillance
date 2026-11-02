-- =========================================================================
-- FUNCTION: refresh_species_dim()
-- =========================================================================
-- Refreshes the activity-envelope columns on ``species_tbl``:
--   first_seen, last_seen, total_detections, station_count
--
-- Reference attributes (common_name, scientific_name, alpha_code,
-- alpha6_code, ebird_code, species_color, species_image_url,
-- species_thumbnail_url, birdweather_url, ebird_url, ml_url,
-- wikipedia_url, wikipedia_summary, prediction_area, range_map) are
-- seeded directly from the BirdWeather public API by the Python loader
-- ``upsert_birdweather_species`` (db/loaders/birdweather.py); this
-- routine never overwrites them.
--
-- Historically this routine also recovered species names from
-- ``birdweather_tbl`` via ``DISTINCT ON (species_id)``. After the
-- normalisation step that removed the redundant species columns from
-- ``birdweather_tbl`` (see ``db/baseline/020_birdweather.sql``), names
-- live exclusively on ``species_tbl`` and are no longer derivable from
-- detections. The Dagster orchestration calls ``upsert_birdweather_species``
-- before ``upsert_birdweather_detections`` so every detection's
-- ``species_id`` already has a row in ``species_tbl``.
--
-- For species that have aged out of the raw retention window we still
-- carry the row forward — the LEFT JOIN preserves it, and the activity
-- envelope simply stops moving forward.
--
-- Source : ``birdweather_daily_cagg_tbl`` (cheap aggregate scan).
-- Output : Updates one row per species_id; never inserts new rows
--          (species are inserted only by the API loader).
-- Depends: birdweather_daily_cagg_tbl, species_tbl
-- Load order: must follow tables + birdweather_daily_cagg_tbl + species_tbl.
-- =========================================================================

BEGIN;

CREATE OR REPLACE FUNCTION refresh_species_dim()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE species_tbl s
    SET    first_seen        = a.first_seen,
           last_seen         = a.last_seen,
           total_detections  = COALESCE(a.total_detections, 0),
           station_count     = COALESCE(a.station_count, 0)
    FROM (
        SELECT
            species_id,
            MIN(bucket)                                 AS first_seen,
            MAX(bucket)                                 AS last_seen,
            SUM(detection_count)::bigint                AS total_detections,
            COUNT(DISTINCT station_id)::int             AS station_count
        FROM birdweather_daily_cagg_tbl
        GROUP BY species_id
    ) a
    WHERE s.species_id = a.species_id;
END;
$$;

COMMENT ON FUNCTION refresh_species_dim() IS
    'Refreshes activity-envelope columns (first_seen, last_seen, '
    'total_detections, station_count) on species_tbl from '
    'birdweather_daily_cagg_tbl. Reference attributes (names, codes, '
    'colours, URLs) are seeded by upsert_birdweather_species and never '
    'touched here. Called by Dagster after each BirdWeather ingest.';

COMMIT;
