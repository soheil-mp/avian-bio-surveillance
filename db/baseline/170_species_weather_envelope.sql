-- =============================================================================
-- Domain baseline DDL for per-species "calling-climate" weather envelopes.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: weather_samples_tbl (slim, persistent extract)
-- =========================================================================
-- Purpose: One row per weather-bearing BirdWeather detection, with the six
--          weather variables pulled out of birdweather_tbl.station_weather
--          ->'weather' (OpenWeather-style, metric units). This exists so the
--          envelope aggregation never has to scan the large station_weather
--          JSONB on the hypertable more than once.
-- Grain  : (species_id, detected_at) for rows where station_weather ? 'weather'.
-- Fill   : Initial historical backfill via scripts/dev/backfill_weather_samples.sh
--          (chunked per month). Ongoing top-ups via refresh_weather_samples().
-- =========================================================================
CREATE TABLE IF NOT EXISTS weather_samples_tbl (
    species_id   TEXT        NOT NULL,
    detected_at  TIMESTAMPTZ NOT NULL,
    temperature  DOUBLE PRECISION,   -- weather.temp        (degrees C)
    rain         DOUBLE PRECISION,   -- weather.rain1h       (mm, dry = 0)
    humidity     DOUBLE PRECISION,   -- weather.humidity     (percent)
    wind         DOUBLE PRECISION,   -- weather.windSpeed    (m/s)
    pressure     DOUBLE PRECISION,   -- weather.pressure     (hPa)
    cloud        DOUBLE PRECISION    -- weather.cloudiness   (percent)
);

CREATE INDEX IF NOT EXISTS idx_weather_samples_species_time
    ON weather_samples_tbl (species_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_weather_samples_time
    ON weather_samples_tbl (detected_at DESC);

-- =========================================================================
-- TABLE: species_weather_envelope_tbl (aggregated percentiles)
-- =========================================================================
-- Purpose: Frontend-facing precomputed IQR envelope per species, window and
--          variable. The /api/species-tracker/climate route reads this with a
--          primary-key lookup -- no hypertable / JSONB access at request time.
-- Grain  : (species_id, window_key, var_key).
--          window_key in (24h, week, month, season, year, total)
--          var_key    in (temperature, rain, humidity, wind, pressure, cloud)
-- Refresh: refresh_species_weather_envelope()
-- =========================================================================
CREATE TABLE IF NOT EXISTS species_weather_envelope_tbl (
    species_id   TEXT NOT NULL,
    window_key   TEXT NOT NULL,
    var_key      TEXT NOT NULL,
    p5           DOUBLE PRECISION,
    q1           DOUBLE PRECISION,
    median       DOUBLE PRECISION,
    q3           DOUBLE PRECISION,
    p95          DOUBLE PRECISION,
    sample_count INTEGER NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (species_id, window_key, var_key)
);

COMMIT;
