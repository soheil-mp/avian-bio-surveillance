-- =========================================================================
-- CONTINUOUS AGGREGATE: knmi_daily_cagg_tbl
-- =========================================================================
-- Daily weather summaries per station. Replaces the former
-- ``knmi_daily_summary`` table (removed in v6) and now carries full daily
-- station metadata plus min/max/avg rollups for every numeric source column
-- in ``knmi_tbl``.
--
-- IMPORTANT: created WITH NO DATA. After initial deployment or bulk backfill:
--
--   CALL refresh_continuous_aggregate(
--       'knmi_daily_cagg_tbl', NULL, NOW());
--
-- Timescale policy helper calls are intentionally left in autocommit mode for
-- compatibility across TimescaleDB versions.
-- =========================================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS knmi_daily_cagg_tbl
WITH (timescaledb.continuous) AS
SELECT
    station_id,
    time_bucket('1 day', observed_at) AS bucket,
    min(observation_date) AS observation_date,
    max(station_name) AS station_name,
    count(*) AS sample_count,
    min(observed_at) AS first_observed_at,
    max(observed_at) AS last_observed_at,

    min(station_lat) AS station_lat_min,
    avg(station_lat) AS station_lat_avg,
    max(station_lat) AS station_lat_max,

    min(station_lon) AS station_lon_min,
    avg(station_lon) AS station_lon_avg,
    max(station_lon) AS station_lon_max,

    min(station_elevation_m) AS station_elevation_m_min,
    avg(station_elevation_m) AS station_elevation_m_avg,
    max(station_elevation_m) AS station_elevation_m_max,

    avg(temperature_c) AS temperature_c_avg,
    min(temperature_c) AS temperature_c_min,
    max(temperature_c) AS temperature_c_max,

    avg(relative_humidity_pct) AS relative_humidity_pct_avg,
    min(relative_humidity_pct) AS relative_humidity_pct_min,
    max(relative_humidity_pct) AS relative_humidity_pct_max,

    avg(wind_speed_ms) AS wind_speed_ms_avg,
    min(wind_speed_ms) AS wind_speed_ms_min,
    max(wind_speed_ms) AS wind_speed_ms_max,

    avg(wind_direction_deg) AS wind_direction_deg_avg,
    min(wind_direction_deg) AS wind_direction_deg_min,
    max(wind_direction_deg) AS wind_direction_deg_max,
    CASE
        WHEN count(wind_direction_deg) FILTER (WHERE wind_direction_deg IS NOT NULL) = 0 THEN NULL
        ELSE MOD(
            (
                degrees(
                    atan2(
                        avg(sin(radians(wind_direction_deg::double precision))) FILTER (WHERE wind_direction_deg IS NOT NULL),
                        avg(cos(radians(wind_direction_deg::double precision))) FILTER (WHERE wind_direction_deg IS NOT NULL)
                    )
                ) + 360.0
            )::numeric,
            360.0
        )::double precision
    END AS wind_direction_deg_circular_avg,
    CASE
        WHEN count(wind_direction_deg) FILTER (WHERE wind_direction_deg IS NOT NULL) = 0 THEN NULL
        ELSE MOD(
            (
                degrees(
                    atan2(
                        avg(sin(radians((wind_direction_deg::double precision + 180.0)))) FILTER (WHERE wind_direction_deg IS NOT NULL),
                        avg(cos(radians((wind_direction_deg::double precision + 180.0)))) FILTER (WHERE wind_direction_deg IS NOT NULL)
                    )
                ) + 360.0
            )::numeric,
            360.0
        )::double precision
    END AS flow_direction_deg_circular_avg,

    avg(pressure_hpa) AS pressure_hpa_avg,
    min(pressure_hpa) AS pressure_hpa_min,
    max(pressure_hpa) AS pressure_hpa_max,

    sum(precipitation_mm) AS precipitation_mm_sum,
    avg(precipitation_mm) AS precipitation_mm_avg,
    min(precipitation_mm) AS precipitation_mm_min,
    max(precipitation_mm) AS precipitation_mm_max,

    avg(solar_radiation_wm2) AS solar_radiation_wm2_avg,
    min(solar_radiation_wm2) AS solar_radiation_wm2_min,
    max(solar_radiation_wm2) AS solar_radiation_wm2_max,

    avg(cloud_cover_okta) AS cloud_cover_okta_avg,
    min(cloud_cover_okta) AS cloud_cover_okta_min,
    max(cloud_cover_okta) AS cloud_cover_okta_max,

    -- Compatibility aliases retained for existing consumers.
    avg(temperature_c) AS daily_mean_temp_c,
    max(temperature_c) AS daily_max_temp_c,
    min(temperature_c) AS daily_min_temp_c,
    sum(precipitation_mm) AS daily_precip_mm,
    avg(relative_humidity_pct) AS daily_mean_rh_pct,
    max(wind_speed_ms) AS daily_max_wind_ms,
    avg(solar_radiation_wm2) AS daily_mean_solar_wm2
FROM knmi_tbl
GROUP BY bucket, station_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'knmi_daily_cagg_tbl',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => true
);

-- No retention policy in dev: data lifecycle is managed manually.
