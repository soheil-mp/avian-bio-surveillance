-- =============================================================================
-- Routines for the per-species calling-climate weather envelope.
-- Applied lexicographically by bootstrap/init_database.sh (after baseline).
-- =============================================================================

-- =========================================================================
-- FUNCTION: refresh_weather_samples()
-- =========================================================================
-- Incremental top-up of weather_samples_tbl: extracts the six weather floats
-- from birdweather_tbl.station_weather->'weather' for detections newer than
-- whatever is already staged.
--
-- WARNING: do NOT use this for the initial historical fill. On an empty
-- weather_samples_tbl it degrades to a single full scan of the station_weather
-- JSONB across every chunk, which is pathologically slow on a spinning disk.
-- Run scripts/dev/backfill_weather_samples.sh (chunked per month) once first;
-- this function is for cheap ongoing top-ups thereafter.
-- =========================================================================
CREATE OR REPLACE FUNCTION refresh_weather_samples()
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    since    timestamptz;
    inserted bigint;
BEGIN
    SELECT COALESCE(max(detected_at), 'epoch'::timestamptz)
      INTO since
      FROM weather_samples_tbl;

    INSERT INTO weather_samples_tbl
        (species_id, detected_at, temperature, rain, humidity, wind, pressure, cloud)
    SELECT
        b.species_id,
        b.detected_at,
        (b.station_weather ->> 'temp')::float8 - 273.15,
        COALESCE((b.station_weather ->> 'rain1h')::float8, 0),
        (b.station_weather ->> 'humidity')::float8,
        (b.station_weather ->> 'windSpeed')::float8,
        (b.station_weather ->> 'pressure')::float8,
        (b.station_weather ->> 'cloudiness')::float8
    FROM birdweather_tbl b
    WHERE b.detected_at > since
      AND b.station_weather IS NOT NULL;

    GET DIAGNOSTICS inserted = ROW_COUNT;
    RETURN inserted;
END;
$$;

-- =========================================================================
-- FUNCTION: refresh_species_weather_envelope()
-- =========================================================================
-- Rebuilds species_weather_envelope_tbl from the slim weather_samples_tbl.
-- Computes the five-point IQR envelope (p5/q1/median/q3/p95) per species, per
-- window, per variable in long form. Windows are trailing ranges anchored to
-- the latest staged sample. Cheap: only touches the slim table, no JSONB.
-- =========================================================================
CREATE OR REPLACE FUNCTION refresh_species_weather_envelope()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    latest timestamptz;
BEGIN
    SELECT max(detected_at) INTO latest FROM weather_samples_tbl;
    IF latest IS NULL THEN
        TRUNCATE species_weather_envelope_tbl;
        RETURN;
    END IF;

    TRUNCATE species_weather_envelope_tbl;

    INSERT INTO species_weather_envelope_tbl
        (species_id, window_key, var_key, p5, q1, median, q3, p95, sample_count, updated_at)
    SELECT
        g.species_id,
        w.window_key,
        v.var_key,
        v.arr[1], v.arr[2], v.arr[3], v.arr[4], v.arr[5],
        v.n,
        now()
    FROM (VALUES
        ('24h',     1),
        ('week',    7),
        ('month',  31),
        ('season', 92),
        ('year',  366),
        ('total', 100000)
    ) AS w(window_key, days)
    CROSS JOIN LATERAL (
        SELECT
            s.species_id,
            percentile_cont(ARRAY[0.05,0.25,0.5,0.75,0.95]) WITHIN GROUP (ORDER BY s.temperature) FILTER (WHERE s.temperature IS NOT NULL) AS temp_arr,  count(s.temperature) AS temp_n,
            percentile_cont(ARRAY[0.05,0.25,0.5,0.75,0.95]) WITHIN GROUP (ORDER BY s.rain)        FILTER (WHERE s.rain        IS NOT NULL) AS rain_arr,  count(s.rain)        AS rain_n,
            percentile_cont(ARRAY[0.05,0.25,0.5,0.75,0.95]) WITHIN GROUP (ORDER BY s.humidity)    FILTER (WHERE s.humidity    IS NOT NULL) AS hum_arr,   count(s.humidity)    AS hum_n,
            percentile_cont(ARRAY[0.05,0.25,0.5,0.75,0.95]) WITHIN GROUP (ORDER BY s.wind)        FILTER (WHERE s.wind        IS NOT NULL) AS wind_arr,  count(s.wind)        AS wind_n,
            percentile_cont(ARRAY[0.05,0.25,0.5,0.75,0.95]) WITHIN GROUP (ORDER BY s.pressure)    FILTER (WHERE s.pressure    IS NOT NULL) AS pres_arr,  count(s.pressure)    AS pres_n,
            percentile_cont(ARRAY[0.05,0.25,0.5,0.75,0.95]) WITHIN GROUP (ORDER BY s.cloud)       FILTER (WHERE s.cloud       IS NOT NULL) AS cloud_arr, count(s.cloud)       AS cloud_n
        FROM weather_samples_tbl s
        WHERE s.detected_at > latest - make_interval(days => w.days)
        GROUP BY s.species_id
    ) AS g
    CROSS JOIN LATERAL (VALUES
        ('temperature', g.temp_arr,  g.temp_n),
        ('rain',        g.rain_arr,  g.rain_n),
        ('humidity',    g.hum_arr,   g.hum_n),
        ('wind',        g.wind_arr,  g.wind_n),
        ('pressure',    g.pres_arr,  g.pres_n),
        ('cloud',       g.cloud_arr, g.cloud_n)
    ) AS v(var_key, arr, n)
    WHERE v.n > 0;
END;
$$;
