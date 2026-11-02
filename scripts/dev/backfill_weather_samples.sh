#!/usr/bin/env bash
# Initial historical backfill of weather_samples_tbl from birdweather_tbl.
# Iterates NEWEST month first so the most useful (recent) weather lands first;
# still covers the whole range. Each month is an atomic DELETE+INSERT, so the
# big station_weather JSONB read is broken into bounded, committed chunks that
# can't wedge the whole table and can be safely resumed (staged months skip).
#
# station_weather is a FLAT OpenWeather object (keys temp, humidity, windSpeed,
# pressure, cloudiness, rain1h, ...). temp/tempMin/tempMax are in KELVIN.
#
# Usage:
#   scripts/dev/backfill_weather_samples.sh [START_MONTH] [END_MONTH]
#   START_MONTH / END_MONTH are YYYY-MM-01 (END exclusive). Defaults cover the
#   current data range with margin.
set -euo pipefail

CONTAINER="ab-timescaledb"
DB="avian_biosurveillance"
USER_="root"
START_MONTH="${1:-2024-01-01}"
END_MONTH="${2:-2026-07-01}"
CHUNK_TIMEOUT_MS="600000"   # 10 min/month safety cap

psql() { docker exec "$CONTAINER" psql -U "$USER_" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "[$(date '+%H:%M:%S')] backfill start (newest-first): $START_MONTH .. $END_MONTH (exclusive)"
cur="$(date -d "$END_MONTH -1 month" +%Y-%m-%d)"
while [ "$(date -d "$cur" +%Y%m%d)" -ge "$(date -d "$START_MONTH" +%Y%m%d)" ]; do
    next="$(date -d "$cur +1 month" +%Y-%m-%d)"

    have="$(psql -tAc "SELECT 1 FROM weather_samples_tbl WHERE detected_at >= '$cur' AND detected_at < '$next' LIMIT 1;")"
    if [ "$have" = "1" ]; then
        echo "[$(date '+%H:%M:%S')] $cur  skip (already staged)"
        cur="$(date -d "$cur -1 month" +%Y-%m-%d)"; continue
    fi

    t0=$(date +%s)
    out="$(psql -c "
        SET statement_timeout='$CHUNK_TIMEOUT_MS';
        DELETE FROM weather_samples_tbl WHERE detected_at >= '$cur' AND detected_at < '$next';
        INSERT INTO weather_samples_tbl
            (species_id, detected_at, temperature, rain, humidity, wind, pressure, cloud)
        SELECT b.species_id, b.detected_at,
            (b.station_weather ->> 'temp')::float8 - 273.15,
            COALESCE((b.station_weather ->> 'rain1h')::float8, 0),
            (b.station_weather ->> 'humidity')::float8,
            (b.station_weather ->> 'windSpeed')::float8,
            (b.station_weather ->> 'pressure')::float8,
            (b.station_weather ->> 'cloudiness')::float8
        FROM birdweather_tbl b
        WHERE b.detected_at >= '$cur' AND b.detected_at < '$next'
          AND b.station_weather IS NOT NULL;
    ")"
    n="$(printf '%s\n' "$out" | grep -oE 'INSERT 0 [0-9]+' | awk '{print $3}')"
    echo "[$(date '+%H:%M:%S')] $cur  inserted ${n:-0} rows  ($(( $(date +%s) - t0 ))s)"
    cur="$(date -d "$cur -1 month" +%Y-%m-%d)"
done

total="$(psql -tAc 'SELECT count(*) FROM weather_samples_tbl;')"
echo "[$(date '+%H:%M:%S')] backfill done. weather_samples_tbl total rows: $total"

# Rebuild the per-species weather envelope from the freshly staged samples.
# Without this, species_weather_envelope_tbl stays empty and the Species
# Tracker "Climate" tab shows no data even though weather_samples_tbl is full.
# work_mem is raised and parallelism disabled so the percentile sorts stay in
# RAM (the default 1.3MB spills to temp files and is pathologically slow on the
# shared spinning disk) while staying under the container's 2GB cap.
echo "[$(date '+%H:%M:%S')] rebuilding species_weather_envelope_tbl from weather_samples_tbl..."
psql -c "SET work_mem='128MB'; SET max_parallel_workers_per_gather=0; SET statement_timeout='2700000'; SELECT refresh_species_weather_envelope();"
env_rows="$(psql -tAc 'SELECT count(*) FROM species_weather_envelope_tbl;')"
echo "[$(date '+%H:%M:%S')] envelope rebuild done. species_weather_envelope_tbl rows: $env_rows"
