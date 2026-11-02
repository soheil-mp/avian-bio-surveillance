# Ingestion

Most ingestion modules support `--persist-to-db` (upsert to TimescaleDB) and `--no-save-local` (skip local disk save). `BirdWeather` is the main exception: it uses `--target {local,database}` plus `--no-enrich-weather`.

---

## 0. Run All

Orchestrator that runs all ingestion modules sequentially. Defaults to Netherlands and all target species from `config/target_species.json`.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--start-date` | str | — | Start date (`YYYY-MM-DD`) |
| `--end-date` | str | — | End date (`YYYY-MM-DD`) |
| `--hours` | int | 24 | Rolling look-back hours (ignored when dates given) |
| `--country` | str | `Netherlands` | Country filter |
| `--min-confidence` | float | 0.1 | Min confidence (BirdWeather) |
| `--workers` | int | 10 | Parallel download workers (BirdWeather) |
| `--persist-to-db` | flag | — | Upsert to TimescaleDB |
| `--no-save-local` | flag | — | Skip local disk save |
| `--classify-background` | flag | — | Run AST classification (BirdWeather) |
| `--bg-device` | str | `cpu` | Device for AST (`cpu`, `cuda`, `mps`) |
| `--bg-batch-size` | int | 8 | Batch size for AST |
| `--no-birdweather` | flag | — | Skip BirdWeather |
| `--no-ebird` | flag | — | Skip eBird |
| `--no-knmi` | flag | — | Skip KNMI |
| `--no-movebank` | flag | — | Skip Movebank |
| `--no-sovon` | flag | — | Skip Sovon |
| `--no-earth-engine` | flag | — | Skip Earth Engine |

```bash
poetry run python -m avian_biosurveillance.ingestion.run_all \
  --start-date 2025-01-01 --end-date 2025-01-31 \
  --classify-background --bg-device cuda --workers 20
```

---

## 1. BirdWeather

Collects bird detection data (metadata + audio) from the [BirdWeather API](https://app.birdweather.com/graphql). Supports date-range or rolling-window queries, parallel audio downloads, country/continent/species filtering, and integrated background sound classification.

`BirdWeather` uses storage targets instead of the older shared persistence flags:

- `--target local` (default): save detections locally under `data/raw/birdweather/...`
- `--target database`: upsert detections into TimescaleDB and upload downloaded audio to MinIO without keeping local detection metadata files
- weather enrichment is enabled by default and attaches station weather, air-pollution, and sensor snapshots to each detection; use `--no-enrich-weather` to skip that extra API work

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--hours` | int | 24 | Hours to look back |
| `--start-date` | str | — | Start date (`YYYY-MM-DD`) |
| `--end-date` | str | — | End date (`YYYY-MM-DD`) |
| `--continent` | str | — | Filter by continent (e.g. `Europe`) |
| `--country` | str | — | Filter by country (e.g. `Netherlands`) |
| `--species-id` | str | — | Species ID filter (repeatable) |
| `--min-confidence` | float | 0.9 | Minimum detection confidence |
| `--workers` | int | 10 | Parallel download workers |
| `--output-dir` | str | `data/raw/birdweather` | Output directory |
| `--no-audio` | flag | — | Skip audio download |
| `--max-detections` | int | — | Limit detections scanned from the API per collection window/day (useful for testing/resume runs) |
| `--retry` | str | — | Retry failed downloads for a date path |
| `--target` | str | `local` | Storage target: `local` or `database` |
| `--no-enrich-weather` | flag | — | Skip station weather, air-pollution, and sensor enrichment |
| `--classify-background` | flag | — | Run AST background classification after download |
| `--classify-only` | str | — | Run/resume background classification only for a date or directory |
| `--bg-device` | str | `cpu` | Device for classification (`cpu`, `cuda`) |
| `--bg-batch-size` | int | 8 | Batch size for classification |
| `--bg-force` | flag | — | Re-classify even if tags exist |
| `--legacy` | flag | — | Use legacy station-based mode |
| `--days` | int | 7 | Days to look back (legacy mode) |

```bash
# Collect locally (default) and include weather enrichment
poetry run python -m avian_biosurveillance.ingestion.birdweather --hours 24 --country Netherlands

# Persist directly to TimescaleDB/MinIO without local detection files
poetry run python -m avian_biosurveillance.ingestion.birdweather --hours 24 --target database --no-enrich-weather

# Collect all 11 NL species (2024-01-01 to 2025-12-31) 
poetry run python -m avian_biosurveillance.ingestion.birdweather --start-date 2025-01-01 --end-date 2025-12-31 --country Netherlands --species-id 35 --species-id 61 --species-id 6 --species-id 44 --species-id 5 --species-id 62 --species-id 81 --species-id 25 --species-id 76 --species-id 407 --species-id 66 --min-confidence 0.1 --workers 20
```

---

## 2. eBird

Fetches citizen-science bird observations from the [eBird API 2.0](https://documenter.getpostman.com/view/664302/S1ENwy59). Supports recent, historic, nearby, and notable observation queries plus hotspot metadata and taxonomy lookups.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--region` | str | `NL` | eBird region code (e.g. `NL`, `US-NY`) |
| `--hotspots` | str | — | Fetch hotspot metadata for a region |
| `--species` | str | — | eBird species code filter (repeatable) |
| `--target-species` | flag | — | Only fetch species from config |
| `--back` | int | 14 | Days back for recent observations (1–30) |
| `--date` | str | — | Single historic date (`YYYY-MM-DD`) |
| `--start-date` | str | — | Start of historic range (`YYYY-MM-DD`) |
| `--end-date` | str | — | End of historic range (`YYYY-MM-DD`) |
| `--lat` | float | — | Latitude for nearby search |
| `--lon` | float | — | Longitude for nearby search |
| `--dist` | int | 25 | Search radius in km (max 50) |
| `--notable` | flag | — | Fetch rare/unusual observations |
| `--hotspot-only` | flag | — | Restrict to hotspot observations |
| `--include-provisional` | flag | — | Include unreviewed observations |
| `--max-results` | int | — | Limit observations per request |
| `--output-dir` | str | `data/raw/ebird` | Output directory |
| `--persist-to-db` | flag | — | Upsert to TimescaleDB |
| `--no-save-local` | flag | — | Skip local disk save |
| `--no-skip-existing` | flag | — | Re-download existing dates |

```bash
poetry run python -m avian_biosurveillance.ingestion.ebird --start-date 2000-01-01 --end-date 2026-03-16 --region NL --target-species --target local
```

---

## 3. KNMI

Ingests meteorological and seismic data from all [KNMI](https://www.knmi.nl/) APIs: Open Data, EDR (Environmental Data Retrieval), WMS, MQTT notifications, and FDSN seismic services.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--ingest` | flag | — | Ingest meteorological observations |
| `--full` | flag | — | Run full ingestion (all KNMI sources) |
| `--observations` | flag | — | Download latest observation files |
| `--earthquakes` | flag | — | Get earthquake data (FDSN Event API) |
| `--weather` | flag | — | Query weather at a coordinate |
| `--notifications` | flag | — | Start real-time MQTT listener |
| `--start-date` / `-s` | str | — | Start date (`YYYY-MM-DD`) |
| `--end-date` / `-e` | str | today | End date (`YYYY-MM-DD`) |
| `--hours-back` | int | 2 | Hours of history for live mode |
| `--force` | flag | — | Re-download existing daily files |
| `--count` | int | 10 | Files to download (`--observations`) |
| `--days` | int | 30 | Days of history to retrieve |
| `--lat` | float | — | Latitude (`--weather`) |
| `--lon` | float | — | Longitude (`--weather`) |
| `--topic` | str | — | MQTT topic (`--notifications`) |
| `--data-dir` | str | `data/raw/knmi` | Output directory |
| `--persist-to-db` | flag | — | Upsert to TimescaleDB |
| `--no-save-local` | flag | — | Skip local disk save |

```bash
poetry run python -m avian_biosurveillance.ingestion.knmi --ingest --start-date 2000-01-01 --end-date 2026-03-16 --target local
```

---

## 4. Movebank

Downloads animal tracking data (GPS, accelerometer) from the [Movebank REST API](https://www.movebank.org/cms/movebank-content/service-requests). Supports study discovery, event/reference data, reduced profiles, and incremental sync.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--study-id` | int | — | Movebank study ID |
| `--list-studies` | flag | — | List accessible studies |
| `--search-taxon` | str | — | Search studies by taxon name |
| `--target-species` | flag | — | Download for all target species (default) |
| `--all-studies` | flag | — | Download from ALL accessible studies |
| `--individual` | str | — | Animal local identifier (repeatable) |
| `--start-date` | str | — | Start date (`YYYY-MM-DD`) |
| `--end-date` | str | — | End date (`YYYY-MM-DD`) |
| `--sensor-type` | int | 653 | Sensor type ID (default: GPS) |
| `--reduction-profile` | str | — | Data reduction profile |
| `--reference-data` | flag | — | Download metadata (individuals, tags, deployments) |
| `--gps-only` | flag | — | Skip non-GPS sensors |
| `--all-sensors` | flag | — | Collect from every available sensor type (not just first match) |
| `--output-dir` | str | `data/raw/movebank` | Output directory |
| `--persist-to-db` | flag | — | Upsert to TimescaleDB |
| `--no-save-local` | flag | — | Skip local save |
| `-v` / `--verbose` | flag | — | Enable verbose logging |

```bash
poetry run python -m avian_biosurveillance.ingestion.movebank --all-studies --start-date 2026-03-16 --end-date 2026-03-16 --target local --all-sensors
```

>> Expected Null Rates

The `EarthEngineCollector` queries 25+ GEE raster datasets per station per day. Many datasets have significant publication lag, so **null values in recent profiles are expected**:

| Dataset | Typical Lag | Fields Affected |
|---|---|---|
| ERA5-Land climate | 3–5 months | `climate.*` (16 fields) |
| MODIS LST / NDVI / EVI | 2–3 weeks | `surface_temperature.*`, `vegetation.ndvi/evi` |
| VIIRS nighttime lights | 1–2 months | `nighttime_lights.*` |
| MODIS ET | 2–3 months | `evapotranspiration.*` |
| Sentinel-5P NO₂ | 2–3 weeks | `air_quality.no2` |
| MODIS Land Cover (IGBP) | ~1 year (annual) | `land_cover.modis_igbp` (always null for current year) |
| WorldPop | deprecated | `population.worldpop_count` (always null; use `gpw_density`) |

Historical dates (e.g. 2025-01-01) typically show ~24% null fields. Recent dates show ~60% null — this is normal and the values fill in as GEE publishes datasets. Fields like elevation, soil, ESA WorldCover, and cloud properties are always available (0% null).


---

## 5. Sovon

Two-phase pipeline for dead bird mortality surveillance from [portal.sovon.nl](https://portal.sovon.nl/). Phase 1 downloads bar chart images; Phase 2 extracts numeric data using the Gemini Flash Vision API.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--download-only` | flag | — | Run phase 1 only (download charts) |
| `--extract-only` | flag | — | Run phase 2 only (extract from existing charts) |
| `--persist-to-db` | flag | — | Upsert extracted data to TimescaleDB |
| `--no-save-local` | flag | — | Skip saving to local disk |

```bash
poetry run python -m avian_biosurveillance.ingestion.sovon
```

---

## 7. Earth Engine

Profiles BirdWeather station locations with 16 categories of environmental data from [Google Earth Engine](https://earthengine.google.com/): elevation, land cover, vegetation indices, surface temperature, climate (ERA5), air quality, cloud cover, surface water, forest cover, nighttime lights, population density, human modification, fire activity, soil properties, evapotranspiration, and sea surface temperature. Stations are auto-discovered from the BirdWeather public API (no token required), database, or local files.

Requires a GEE service-account key set via `GEE_SERVICE_ACCOUNT_CREDENTIALS` in `.env` (paste the full JSON on one line), or Application Default Credentials.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--start-date` | str | — | Start date (`YYYY-MM-DD`) for batch collection |
| `--end-date` | str | — | End date (`YYYY-MM-DD`) for batch collection |
| `--date` | str | — | Single date for quick profiling (`YYYY-MM-DD`) |
| `--lat` | float | — | Latitude (single-point mode) |
| `--lon` | float | — | Longitude (single-point mode) |
| `--locations-file` | str | — | JSON/JSONL file with `[{id, lat, lon}, ...]` |
| `--project` | str | — | GEE project ID (falls back to `GEE_PROJECT` env) |
| `--high-volume` | flag | — | Use the GEE high-volume endpoint |
| `--parallel` | flag | ✓ | Profile sections in parallel |
| `--no-parallel` | flag | — | Disable parallel profiling |
| `--max-workers` | int | env/default | Parallel section workers per location |
| `--section` | str | — | Query a single section (e.g. `climate`) |
| `--grid` | flag | — | Profile the canonical Netherlands grid instead of stations |
| `--grid-size-km` | float | 5.0 | Grid cell size in kilometres for `--grid` |
| `--cell-workers` | int | env/default | Parallel grid cells per date for `--grid` |
| `--db-batch-size` | int | env/default | Rows per database upsert batch for `--grid` |
| `--target` | enum | `local` | Write to local files or the database |
| `--json` | flag | — | Print JSON to stdout |
| `--force` | flag | — | Re-run even if output already exists |

```bash
# Ingest the Netherlands grid into the database, skipping fully-covered dates
poetry run python -m avian_biosurveillance.ingestion.earth_engine \
  --grid --target database --high-volume \
  --start-date 2024-01-01 --end-date 2025-12-31 \
  --max-workers 4 --cell-workers 2 --db-batch-size 200

# Single point: quick profile for one location
poetry run python -m avian_biosurveillance.ingestion.earth_engine \
  --lat 52.0 --lon 4.7 --date 2025-06-15 --json
```

---

## 8. Background Sound Classification

Enriches BirdWeather audio with ambient sound tags using the [AST (Audio Spectrogram Transformer)](https://huggingface.co/MIT/ast-finetuned-audioset-10-10-0.4593) model. Classifies 527 AudioSet classes (rain, wind, traffic, speech, etc.). Can also be triggered inline via BirdWeather's `--classify-background` flag.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--input` / `-i` | str | **required** | Path to BirdWeather date dir or parent |
| `--device` / `-d` | str | auto | Device for inference (`cpu`, `cuda`, `mps`) |
| `--batch-size` / `-b` | int | 8 | Batch size |
| `--top-k` / `-k` | int | all | Max tags per detection |
| `--min-confidence` | float | 0.10 | Confidence threshold |
| `--force` | flag | — | Re-classify all detections |
| `--dry-run` | flag | — | Preview without writing |
| `-v` / `--verbose` | flag | — | Debug-level logging |

```bash
poetry run python -m avian_biosurveillance.ingestion.background_sound_classification \
  --input data/raw/birdweather --device cuda --batch-size 16
```
