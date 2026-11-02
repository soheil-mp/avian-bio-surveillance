# Database Layer (`avian_biosurveillance.db`)

Python interface to TimescaleDB and MinIO for the Avian Biosurveillance platform.

## Prerequisites

```bash
# Start infrastructure containers (from repo root)
bash infrastructure/docker/compose.sh up -d timescaledb minio

# Verify containers are running
docker ps --filter name=timescaledb --filter name=minio
```

Required secrets in `.env`; ports and endpoints are sourced from `config/runtime.env`:

```dotenv
MINIO_ROOT_USER=avian-minio
MINIO_ROOT_PASSWORD=<password>
POSTGRES_PASSWORD=<password>
```

## Health Check

```bash
# Run the smoke-test script (checks TimescaleDB + MinIO)
poetry run python scripts/ops/db_healthcheck.py
```

Verifies: TimescaleDB extension, required runtime relations exist, hypertable
config, continuous aggregates are discoverable, and MinIO buckets exist.

## Seed Database (Backfill Local Data + BirdWeather Reference Metadata)

```bash
# Seed all sources plus BirdWeather species / station metadata
poetry run python scripts/ops/seed_db.py

# Seed BirdWeather metadata + detections
poetry run python scripts/ops/seed_db.py --birdweather

# Seed only BirdWeather species / station metadata
poetry run python scripts/ops/seed_db.py --birdweather-reference

# Seed specific sources
poetry run python scripts/ops/seed_db.py --sovon

# Dry-run: count records without writing
poetry run python scripts/ops/seed_db.py --dry-run
```

## Python API

### Connection Pool

```python
from avian_biosurveillance.db import ConnectionPool, get_pool, healthcheck

# Singleton pool (reads DATABASE_URL from env)
pool = get_pool()

# Or create a custom pool
pool = ConnectionPool(min_conn=2, max_conn=10)

# Health check
result = healthcheck(pool)
print(result)  # {'ok': True, 'tables_found': {...}, 'tables_missing': set(), ...}
```

### Bulk Upsert Loaders

```python
from avian_biosurveillance.db import (
    upsert_birdweather_detections,
    upsert_knmi_observations,
    upsert_sovon_mortality,
)
from avian_biosurveillance.db.models import BirdWeatherDetection

# Upsert BirdWeather detections
records = [BirdWeatherDetection.from_jsonl(det) for det in detections]
n = upsert_birdweather_detections(records)
print(f"Upserted {n} rows")

# Upsert Sovon mortality (accepts list of dicts)
rows = [{
    "species_english": "Eurasian Blackbird",
    "year": 2024,
    "month": 1,
    "dead_bird_count": 5,
}]
n = upsert_sovon_mortality(rows)
```

All upsert functions use `ON CONFLICT DO UPDATE` with chunk size 5000.

Note: `upsert_knmi_daily_summaries` was removed in v6. Daily summaries are now
derived automatically via the `knmi_daily_cagg_tbl` TimescaleDB continuous
aggregate — no separate ingestion path is needed.

### MinIO Audio Store

```python
from avian_biosurveillance.db import AudioStore

store = AudioStore()

# Upload audio bytes
store.upload_audio("2024-01-15/station_123/det_456.mp3", audio_bytes)

# Download audio to a local file
store.download_to_file(
    "2024-01-15/station_123/det_456.mp3",
    "data/processed/tmp/det_456.mp3",
)

# Check whether an object exists
store.exists("2024-01-15/station_123/det_456.mp3")

# Health check
result = store.healthcheck()  # {'ok': True, 'buckets_found': [...], ...}
```

Buckets: `raw-audio`, `processed-audio`, `features`.

## Schema

The canonical schema is defined under [`db/`](../../../db/) and bootstrapped
from `db/bootstrap/init_database.sh`.

This package depends on both source tables and derived relations, including:

- `birdweather_tbl`, `knmi_tbl`, `sovon_tbl`, `ebird_tbl`, `movebank_tbl`
- `birdweather_daily_cagg_tbl`, `knmi_daily_cagg_tbl`
- `birdweather_stations_tbl`, `species_tbl`, `movebank_tracks_tbl`
- `google_earth_engine_tbl`, `google_earth_engine_grid_tbl`, `grid_cells_tbl`
- `cds_drought_tbl`, `seismic_tbl`, `data_quality_tbl`

See [db/README.md](../../../db/README.md) for the full schema, bootstrap
order, and naming conventions.

## Tests

```bash
poetry run pytest tests/db/ -v
```
