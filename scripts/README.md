
# Scripts

This folder is organized by purpose so operational tools, repair scripts, dev
helpers, and release tooling stop tripping over each other:

- `scripts/ops/` — maintained operational entrypoints
- `scripts/repair/` — manual repair and backfill helpers. Ledger-backed SQL
  migrations belong in `db/migrations/`.
- `scripts/dev/` — demos and sample-data generators
- `scripts/tooling/` — code quality and image publishing helpers
- `scripts/encryption/` — `.env` encryption / decryption (see [encryption/README.md](encryption/README.md))

## Encryption

Encrypt and decrypt `.env` so an encrypted backup (`.env.enc`, gitignored) can be
copied to another machine.

```bash
# Encrypt .env → .env.enc (first time prompts for password)
poetry run python scripts/encryption/env_crypto.py encrypt

# Decrypt .env.enc → .env
poetry run python scripts/encryption/env_crypto.py decrypt

# Check status
poetry run python scripts/encryption/env_crypto.py status
```

## Docker Compose (with auto-decrypt)

Wrapper scripts that ensure `.env` exists before starting the stack:
the compose wrappers they call default `COMPOSE_BAKE=false` locally to avoid
Docker Desktop build/export hangs.

If `config/docker.local.env` exists, the Compose wrapper loads it after
`config/runtime.env` so local Docker-only overrides can persist across restarts
without changing secrets or committed runtime topology.

```bash
bash scripts/ops/docker_up.sh            # up -d all services
bash scripts/ops/docker_up.sh app         # up -d single service
bash scripts/ops/docker_down.sh           # down
```

Direct compose wrappers are also available when `.env` already exists:

```bash
bash infrastructure/docker/compose.sh ps
```

## Operations

## Export Performance Metrics

Run the automated website performance audit. This auto-discovers the current
Next.js pages and internal API routes, exercises them against the running local
frontend, flushes pending metric writes, and verifies that the exported CSV is
sorted by `duration_ms` descending.

```bash
poetry run python scripts/export_performance_metrics.py
```

Optional flags:

```bash
# Keep the current export instead of resetting it first
poetry run python scripts/export_performance_metrics.py --keep-existing

# Target a different frontend host
poetry run python scripts/export_performance_metrics.py --base-url http://127.0.0.1:3000
```

## Export Docker Logs

Export Docker Compose logs plus a compact issue summary. The exporter now
defaults to the last 12 hours so audits do not mix current faults with stale
historical incidents.

```bash
# Default: export last 12h to docker_logs.txt
poetry run python scripts/export_docker_logs.py

# Explicit window
poetry run python scripts/export_docker_logs.py --since 6h

# Full history when needed
poetry run python scripts/export_docker_logs.py --since all
```

## Docker DNS Smoke Test

Verify that the Docker stack can resolve the external hosts used by the
frontend and ingestion services.

```bash
bash scripts/ops/docker_dns_smoke_test.sh
```

## DB Health Check

Smoke-test for TimescaleDB and MinIO infrastructure. Run after the compose wrapper brings the stack up.

```bash
# Check all DB + MinIO services
poetry run python scripts/ops/db_healthcheck.py
```

## Seed Database

Backfill existing local data into TimescaleDB and seed BirdWeather species / station metadata from the public API.

```bash
# Seed all configured sources plus BirdWeather reference metadata
poetry run python scripts/ops/seed_db.py

# Seed BirdWeather metadata + detections
poetry run python scripts/ops/seed_db.py --birdweather

# Seed only BirdWeather species / station reference metadata
poetry run python scripts/ops/seed_db.py --birdweather-reference

# Seed specific sources
poetry run python scripts/ops/seed_db.py --sovon

# Dry-run: count records without writing
poetry run python scripts/ops/seed_db.py --dry-run
```

## Convert Historical BirdWeather Data

This script works for converting the historical BirdWeather data (2018-2023) that was downloaded manually from the website into our standard format, and optionally downloading the associated audio files. It can be run with various options to control which years to process and whether to include audio.

```bash
# Convert all + download audio
poetry run python scripts/repair/convert_historical_birdweather.py

# No audio (just detections)
poetry run python scripts/repair/convert_historical_birdweather.py --no-audio

# Specific years
poetry run python scripts/repair/convert_historical_birdweather.py --years 2021 2025 --workers 20
```

## Data Quality

Run data quality audits with configurable output directory and pass/fail thresholds.

```bash
poetry run python scripts/ops/data_quality.py
```

## Dagster Sensor State Reconcile

Operational helper that reconciles stale Dagster sensor rows after repository
sensor definitions change. The local stack invokes this automatically before
`dagster-daemon` starts.

```bash
poetry run python scripts/ops/dagster_reconcile_sensor_state.py
```

## Dagster Run State Reconcile

Operational helper that marks orphaned Dagster runs terminal after a local host
or container restart. This matters for the Docker Compose deployment because
`DefaultRunLauncher` cannot preserve active local run workers across a reboot.
The local stack invokes this automatically before `dagster-daemon` starts.

```bash
poetry run python scripts/ops/dagster_reconcile_run_state.py
```

## Precompute Explore Assets

Generate cached `/explore` dropdown data and optional trimmed audio clips for
the frontend.

```bash
# Rebuild options + clips
poetry run python scripts/ops/precompute_explore.py

# Only regenerate options JSON
poetry run python scripts/ops/precompute_explore.py --options-only

# Only trim clips
poetry run python scripts/ops/precompute_explore.py --clips-only
```

## Repair / Migration Scripts

## Seed Audio

One-time backfill: scan `data/raw/birdweather/` for audio files and upload to MinIO.

```bash
poetry run python scripts/ops/seed_audio.py
```

## Prune BirdWeather Raw Audio

Dry-run helper that finds BirdWeather `raw-audio` objects whose linked rows all
already have processed clips, verifies those processed clips still exist in
MinIO, and optionally deletes the raw object. The script scans history in small
observation-date chunks by default so larger historical windows remain usable.

```bash
# Preferred inside ab-ingestion, using mounted application code
docker exec -i -w /app ab-ingestion python -m avian_biosurveillance.ops.prune_birdweather_raw_audio --before-date 2025-01-01

# Legacy host wrapper still works from the repo checkout
poetry run python scripts/ops/prune_birdweather_raw_audio.py --before-date 2025-01-01

# Delete up to 500 verified raw objects in that historical slice
docker exec -i -w /app ab-ingestion python -m avian_biosurveillance.ops.prune_birdweather_raw_audio --before-date 2025-01-01 --limit 500 --apply
```

## Migrate MinIO Storage

Prepare a reversible MinIO storage cutover from the default repo bind mount
(`data/minio`) to a Docker-managed named volume. The helper writes
`config/docker.local.env` with `MINIO_DATA_MOUNT=...` so the change survives
restarts without touching `.env`.

```bash
# Inspect the current MinIO storage mode
bash scripts/ops/migrate_minio_storage.sh --check

# Copy data/minio into the Docker-managed minio-data volume and enable the override
bash scripts/ops/migrate_minio_storage.sh --apply

# Remove the override and fall back to the bind mount again
bash scripts/ops/migrate_minio_storage.sh --rollback
```

Run the helper only after draining MinIO writers, then restart MinIO and verify
health:

```bash
bash infrastructure/docker/compose.sh up -d --no-build --no-deps minio minio-init
bash scripts/ops/recover_minio.sh --check
```

## Migrate TimescaleDB Cluster

Logically migrate the full application PostgreSQL/TimescaleDB data set between
Docker contexts. The default source/target pair is Docker Desktop
(`desktop-linux`) -> native Docker (`default`), which is the required cutover if
you want the historical database and GPU-backed ingestion on the same daemon.

```bash
# Inspect the source/target versions, database list, and downtime scope
bash scripts/ops/migrate_timescaledb_cluster.sh --check

# Migrate avian_biosurveillance + dagster_db from Docker Desktop into native Docker
bash scripts/ops/migrate_timescaledb_cluster.sh --apply

# Do the same and immediately bring the native stack back up
bash scripts/ops/migrate_timescaledb_cluster.sh --apply --restart-target-stack
```

The helper stops the source app containers before the dump so the data stays
consistent, streams `pg_dump` -> `pg_restore` for each selected database, and
leaves the source app containers stopped afterward to avoid split-brain writes.
By default it migrates the application databases (`avian_biosurveillance` and
`dagster_db`) and skips the administrative `postgres` database.

## Roll Out Canonical Grid Cells

Safely apply the canonical `grid_cells_tbl` schema, backfill geodesic 5 km cells,
and validate the Earth Engine grid foreign key on an existing database.

```bash
# Full rollout using .env credentials
poetry run python scripts/repair/migrate_grid_cells.py

# Preview only
poetry run python scripts/repair/migrate_grid_cells.py --dry-run

# Reuse an already-applied schema and just backfill + validate
poetry run python scripts/repair/migrate_grid_cells.py --skip-schema
```

## Normalize Earth Engine Table

Normalize legacy rows in `google_earth_engine_tbl` after audit-driven schema
or payload changes.

```bash
# Preview affected rows only
poetry run python scripts/repair/normalize_earth_engine_table.py --dry-run

# Apply normalization
poetry run python scripts/repair/normalize_earth_engine_table.py
```

## Backfill Earth Engine Population

Repair missing population-related Earth Engine fields without re-running the
entire historical profile workload.

```bash
# Preview station-year targets
poetry run python scripts/repair/backfill_earth_engine_population.py --dry-run

# Backfill a single year
poetry run python scripts/repair/backfill_earth_engine_population.py --year 2024
```

## Refresh Station Summary Function

Re-register the latest `refresh_station_summary()` SQL definition and execute it
manually.

```bash
poetry run python scripts/repair/refresh_station_summary.py
```

## Development / Demo Scripts

## Generate Sample Data

Generate realistic BirdWeather-structured sample data for testing and demos.

```bash
poetry run python scripts/dev/generate_sample_data.py
```

## Tooling

## Code Quality

Run the project's full quality stack — ruff format, ruff lint, mypy strict,
sqlfluff against `db/`, and pytest with the 80% coverage gate — in a single
invocation. Tools are run through `poetry run` automatically when Poetry and
`pyproject.toml` are both present, so the script works without a manually
activated virtualenv.

Phases: `format`, `lint`, `type`, `sql`, `test`.

```bash
./scripts/tooling/code_quality.sh                  # all phases, no writes
./scripts/tooling/code_quality.sh --fix            # auto-fix where possible
./scripts/tooling/code_quality.sh --check          # explicit no-write mode
./scripts/tooling/code_quality.sh --only lint,type # subset only
./scripts/tooling/code_quality.sh --skip sql,test  # everything except
./scripts/tooling/code_quality.sh --no-cov         # skip the coverage gate
```

Frontend quality (`next lint`, vitest, `tsc`) lives in `frontend/` and is
run separately via `npm run lint` and `npm run test`.

## Publish Docker Images Locally

Build, tag, and push selected custom images to your Docker Hub namespace
without relying on CI/CD.

```bash
# Build + push the default custom services (frontend, ingestion, model, dagster)
./scripts/tooling/publish_docker_images.sh -n soheilmp

# Release publish for all default services, including latest
./scripts/tooling/publish_docker_images.sh -n soheilmp --release

# Push only the frontend, ingestion, and model images, and add latest
./scripts/tooling/publish_docker_images.sh -n soheilmp -s frontend,ingestion,model --also-latest

# Reuse an already-built local image and publish a custom tag
./scripts/tooling/publish_docker_images.sh -n soheilmp -s frontend -t demo-2026-03-31 --skip-build

# Publish a named release tag and latest together
./scripts/tooling/publish_docker_images.sh -n soheilmp -s frontend,ingestion,model,dagster -t 2026-03-31 --release

# Dry-run the build/tag/push steps
./scripts/tooling/publish_docker_images.sh -n soheilmp --dry-run
```

Notes:

- Run `docker login` first; the script does not handle credentials directly.
- `--release` is shorthand for adding `latest` alongside your immutable tags.
- If you pass `--tag` together with `--also-latest` or `--release`, the script
  publishes both the explicit tag(s) and `latest`.
- The destination repositories should already exist on Docker Hub if you want
  them private.
- Default target repositories are:
  - `<namespace>/avian-biosurveillance-frontend`
  - `<namespace>/avian-biosurveillance-ingestion`
  - `<namespace>/avian-biosurveillance-model`
  - `<namespace>/avian-biosurveillance-dagster`