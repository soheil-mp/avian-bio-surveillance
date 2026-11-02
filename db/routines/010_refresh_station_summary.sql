-- =========================================================================
-- FUNCTION: refresh_station_summary()
-- =========================================================================
-- Refreshes detection-derived station summaries on birdweather_stations_tbl.
-- Reference attributes are seeded by upsert_birdweather_stations() and are
-- preserved by this routine; missing station rows are inserted with summary
-- fields only so metadata/stat load order cannot hide detected stations.
--
-- Summary fields:
--   total_detections, unique_species, unique_days,
--   first_detection, last_detection, avg_confidence,
--   high_confidence_detections, monthly_detections, top_species
--
-- Availability fields:
--   days_since_last, lifetime_availability_pct, availability_90d_pct
-- =========================================================================

BEGIN;

CREATE OR REPLACE FUNCTION refresh_station_summary()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO birdweather_stations_tbl (
        station_id,
        total_detections,
        unique_species,
        unique_days,
        first_detection,
        last_detection,
        avg_confidence,
        high_confidence_detections,
        monthly_detections,
        top_species,
        days_since_last,
        lifetime_availability_pct,
        availability_90d_pct
    )
    WITH base AS (
        SELECT
            c.station_id,
            SUM(c.detection_count)::int                  AS total_detections,
            COUNT(DISTINCT c.species_id)::int            AS unique_species,
            COUNT(DISTINCT c.bucket::date)::int          AS unique_days,
            MIN(c.bucket::date)                          AS first_detection,
            MAX(c.bucket::date)                          AS last_detection,
            ROUND(
                (SUM(c.confidence_sum)::numeric
                    / NULLIF(SUM(c.detection_count), 0)),
                4
            )                                            AS avg_confidence,
            SUM(c.high_confidence_count)::int            AS high_confidence_detections
        FROM birdweather_daily_cagg_tbl c
        GROUP BY c.station_id
    ),
    monthly AS (
        SELECT station_id,
               jsonb_object_agg(month, cnt) AS monthly_detections
        FROM (
            SELECT station_id,
                   TO_CHAR(bucket, 'YYYY-MM') AS month,
                   SUM(detection_count)::int  AS cnt
            FROM birdweather_daily_cagg_tbl
            GROUP BY station_id, TO_CHAR(bucket, 'YYYY-MM')
        ) m
        GROUP BY station_id
    ),
    species_counts AS (
        SELECT
            c.station_id,
            c.species_id,
            COALESCE(sp.common_name, c.species_id) AS name,
            sp.species_thumbnail_url               AS thumbnail_url,
            SUM(c.detection_count)::int            AS cnt
        FROM birdweather_daily_cagg_tbl c
        LEFT JOIN species_tbl sp ON sp.species_id = c.species_id
        GROUP BY c.station_id, c.species_id, sp.common_name, sp.species_thumbnail_url
    ),
    top5 AS (
        SELECT station_id,
               jsonb_agg(
                   jsonb_build_object(
                       'name', name,
                       'count', cnt,
                       'thumbnail_url', thumbnail_url
                   ) ORDER BY cnt DESC
               ) AS top_species
        FROM (
            SELECT sc.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY sc.station_id
                       ORDER BY sc.cnt DESC
                   ) AS rn
            FROM species_counts sc
        ) ranked
        WHERE rn <= 5
        GROUP BY station_id
    ),
    recent_daily AS (
        SELECT
            station_id,
            bucket::date AS observation_date
        FROM birdweather_daily_cagg_tbl
        WHERE bucket >= CURRENT_DATE - INTERVAL '89 days'
        GROUP BY station_id, bucket::date
    ),
    recent_window AS (
        SELECT
            station_id,
            LEAST(
                100.0,
                ROUND((100.0 * COUNT(*) / 90.0)::numeric, 2)
            ) AS availability_90d_pct
        FROM recent_daily
        GROUP BY station_id
    ),
    features AS (
        SELECT
            b.station_id,
            b.total_detections,
            b.unique_species,
            b.unique_days,
            b.first_detection,
            b.last_detection,
            b.avg_confidence,
            b.high_confidence_detections,
            COALESCE(mo.monthly_detections, '{}'::jsonb) AS monthly_detections,
            COALESCE(t.top_species, '[]'::jsonb)         AS top_species,
            (CURRENT_DATE - b.last_detection)::int       AS days_since_last,
            ROUND(
                100.0 * b.unique_days
                / GREATEST(1, (CURRENT_DATE - b.first_detection)::int + 1),
                2
            )                                            AS lifetime_availability_pct,
            COALESCE(rw.availability_90d_pct, 0)         AS availability_90d_pct
        FROM base b
        LEFT JOIN monthly mo ON mo.station_id = b.station_id
        LEFT JOIN top5 t ON t.station_id = b.station_id
        LEFT JOIN recent_window rw ON rw.station_id = b.station_id
    )
    SELECT
        f.station_id,
        f.total_detections,
        f.unique_species,
        f.unique_days,
        f.first_detection,
        f.last_detection,
        f.avg_confidence,
        f.high_confidence_detections,
        f.monthly_detections,
        f.top_species,
        f.days_since_last,
        f.lifetime_availability_pct,
        f.availability_90d_pct
    FROM features f
    ON CONFLICT (station_id) DO UPDATE SET
        total_detections           = EXCLUDED.total_detections,
        unique_species             = EXCLUDED.unique_species,
        unique_days                = EXCLUDED.unique_days,
        first_detection            = EXCLUDED.first_detection,
        last_detection             = EXCLUDED.last_detection,
        avg_confidence             = EXCLUDED.avg_confidence,
        high_confidence_detections = EXCLUDED.high_confidence_detections,
        monthly_detections         = EXCLUDED.monthly_detections,
        top_species                = EXCLUDED.top_species,
        days_since_last            = EXCLUDED.days_since_last,
        lifetime_availability_pct  = EXCLUDED.lifetime_availability_pct,
        availability_90d_pct       = EXCLUDED.availability_90d_pct;
END;
$$;

COMMENT ON FUNCTION refresh_station_summary() IS
    'Upserts detection-derived summary, freshness, lifetime availability, '
    'and recent 90-day availability on birdweather_stations_tbl from '
    'birdweather_daily_cagg_tbl and species_tbl while preserving seeded '
    'station metadata.';

COMMIT;
