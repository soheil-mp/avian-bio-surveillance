-- =========================================================================
-- CONTINUOUS AGGREGATE: birdweather_daily_cagg_tbl
-- =========================================================================
-- Daily species detection counts per station.
-- Auto-refreshes hourly with a 3-day look-back window.
--
-- IMPORTANT: created WITH NO DATA. After initial deployment or bulk backfill,
-- materialise historical data manually:
--
--   CALL refresh_continuous_aggregate(
--       'birdweather_daily_cagg_tbl', NULL, NOW());
--
-- Timescale policy helper calls are intentionally left in autocommit mode for
-- compatibility across TimescaleDB versions.
-- =========================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS birdweather_daily_cagg_tbl
WITH (timescaledb.continuous) AS
SELECT
    species_id,
    station_id,
    time_bucket('1 day', detected_at) AS bucket,
    count(*) AS detection_count,
    sum(confidence) AS confidence_sum,
    avg(confidence) AS avg_confidence,
    min(confidence) AS min_confidence,
    max(confidence) AS max_confidence,
    count(*) FILTER (WHERE confidence >= 0.8)
                                        AS high_confidence_count
FROM birdweather_tbl
GROUP BY bucket, species_id, station_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'birdweather_daily_cagg_tbl',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => true
);

-- No retention policy in dev: data lifecycle is managed manually.
