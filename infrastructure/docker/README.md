# Docker Infrastructure — Avian Biosurveillance Platform

Operational reference for the local development stack defined in
`docker-compose.yml`. Covers service topology, bootstrap behaviour,
environment configuration, common operations, and resource limits.

## Services

| Container | Image / Dockerfile | Port(s) |
| --- | --- | --- |
| `ab-dagster-home-init` | `avian-biosurveillance/dagster:1.9` (one-shot) | — |
| `ab-timescaledb` | `timescale/timescaledb:2.17.2-pg16` | 5432 |
| `ab-timescaledb-init` | `postgres:16-bookworm` | — |
| `ab-autoheal` | `willfarrell/autoheal:1.2.0` | — |
| `ab-minio` | `minio/minio:RELEASE.2025-09-07T16-13-09Z` | 9000 / 9001 |
| `ab-minio-init` | `minio/mc:RELEASE.2025-08-13T08-35-41Z` | — |
| `ab-dagster-webserver` | `Dockerfile.dagster` | 13030 |
| `ab-dagster-daemon` | `Dockerfile.dagster` | — |
| `ab-ingestion` | `Dockerfile.ingestion` (Dagster gRPC code-server) | 4000 |
| `ab-model` | `Dockerfile.model` (modeling / feature-engineering gRPC code-server) | 4001 |
| `ab-frontend` | `Dockerfile.frontend` | 13000 |

All published ports are bound to `127.0.0.1` and are not reachable from
the host network.

## Quick Start

```bash
# From the repository root: create .env with the secrets listed under
# Configuration below (it is gitignored).

bash infrastructure/docker/compose.sh up -d
bash infrastructure/docker/compose.sh ps
```

`infrastructure/docker/compose.sh` wraps the project's Compose invocation
and defaults `COMPOSE_BAKE=false`. It loads `.env` for secrets and
`config/runtime.env` for shared runtime topology. The same repository
layout is used on a laptop or a single on-prem server; stateful local
service data lives under the repo's `data/` tree. Docker Desktop 29.4.x can hang in
`docker compose build` during the bake export/provenance phase for this
stack even after the image layers are built; the wrapper disables bake
mode by default and lets callers override it explicitly.

If containers cannot resolve external hostnames such as
`api.phylopic.org`, `service.pdok.nl`, `media.birdweather.com`, or
`cds.climate.copernicus.eu`, fix Docker's DNS configuration at the host
or Docker Desktop layer before recreating the affected services.

On Linux:

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "dns": ["1.1.1.1", "8.8.8.8"]
}
EOF
sudo systemctl restart docker
bash infrastructure/docker/compose.sh up -d --force-recreate frontend ingestion model
```

On Docker Desktop, set the same DNS servers in Docker Engine settings,
restart Docker Desktop, then recreate the affected services.

## Service URLs

| Service | URL |
| --- | --- |
| Dashboard | http://localhost:13000 |
| Dagster UI | http://localhost:13030 |
| MinIO Console | http://localhost:19001 |
| TimescaleDB | `postgresql://root:***@localhost:15432/avian_biosurveillance` |

## Local Frontend Development

The dashboard can be run either inside Docker on `http://localhost:13000`
or directly from the `frontend/` workspace with `npm run dev`. The local
Next.js server auto-loads the shared repository environment from
`config/runtime.env` and `.env`, so it connects to host-exposed local
services (for example, TimescaleDB on `localhost:15432`) rather than
Docker-only hostnames such as `timescaledb`.

Start the backing services first, then run the frontend locally:

```bash
bash infrastructure/docker/compose.sh up -d \
    timescaledb timescaledb-init minio minio-init dagster-webserver

cd frontend
npm run dev
```

## Initialization

**First boot (empty volume).** PostgreSQL initialises the `root`
superuser, then the `ab-timescaledb-init` sidecar provisions auxiliary
databases and applies the schema tree.

**Every boot.** `ab-timescaledb-init` re-runs `ensure_aux_roles.sh`
against the live database, catching volumes that pre-date the init
scripts and removing legacy roles. The sidecar is fully idempotent. The
Dagster webserver and daemon wait for it to complete before starting.

**Dagster home init.** `ab-dagster-home-init` normalises ownership for
Dagster-managed paths inside `data/dagster_home` before the non-root
Dagster services start. It intentionally skips `/opt/dagster_home/.cache`
because HuggingFace and other runtime caches may live there, and some
Docker bind-mount backends refuse recursive `chown` on downloaded cache
artifacts — including it would cause `docker compose up` to fail during
routine rebuilds.

**Live-stack guard.** If the application schema is already present and
other client backends are connected to `avian_biosurveillance`,
`ab-timescaledb-init` exits early instead of replaying heavy DDL into a
live workload. This prevents routine `docker compose up` rebuilds from
hanging on objects such as `species_tracker_tbl` while ingestion is
mid-refresh.

**MinIO init.** `ab-minio-init` creates the `raw-audio`,
`processed-audio`, and `features` buckets with private access and
provisions a scoped `MINIO_APP_USER` with read/write access to those
buckets only. The object-store path is the repo-relative bind mount
`data/minio`, which keeps the same single-container layout working on a
laptop or a single on-prem server. The directory is ignored by git and
should be included in normal server backups when object artifacts matter.
The Compose defaults throttle MinIO's background scanner/healer
(`MINIO_SCANNER_SPEED=slowest`, `MINIO_HEAL_MAX_IO=1`,
`MINIO_HEAL_MAX_SLEEP=1s`) because Docker Desktop bind mounts can report
`too many open files` during scanner walks even when the container
`nofile` limit is high. Override those variables only on storage backends
that tolerate more aggressive background scans.

**Historical catchup autostart.** BirdWeather has its own sensor-owned
`birdweather-catchup`; `historical-catchup` covers the shared
non-BirdWeather 2024+ backlog. With `CATCHUP_AUTOSTART=true` (the
default), Dagster begins launching both oldest-first catchup paths on
boot and advances one chronological window per sensor tick after the
previous window succeeds. BirdWeather table backfill remains table-only;
raw-audio download, audio processing, and feature engineering run as
batched follow-up work over each completed catchup window. It is not a
one-shot full-range run. Legacy `BACKFILL_*` variables remain supported.
BirdWeather API pagination keeps a small request delay via
`BIRDWEATHER_PAGE_SLEEP_SECONDS` (default `0.02` in Compose, `0.1`
outside Compose) so long all-species catchups can trade speed against
upstream rate-limit pressure without code changes.

**Dagster reboot recovery.** Before `ab-dagster-daemon` resumes
scheduling, it reconciles any orphaned `STARTING` / `STARTED` /
`CANCELING` runs left behind by host or container restarts. With
`DefaultRunLauncher`, Dagster cannot keep active runs alive across a
reboot, so the startup helper marks those runs terminal and lets sensors
retry or advance automatically instead of waiting forever. The ingestion
code-server additionally runs a code-location-scoped reconcile step at
startup, so interrupted BirdWeather (and other ingestion-owned) runs do
not stay stuck in `STARTED` when only `ab-ingestion` is rebuilt.

**Container autoheal.** `ab-autoheal` polls the Docker daemon every
30 s and restarts any container labelled `autoheal=true` whose health
status is `unhealthy`. This is still useful for ordinary app containers
whose healthchecks reflect their actual serving state. MinIO is now
excluded from autoheal on purpose: when Docker's exec layer is degraded,
MinIO can stay reachable from `ab-ingestion` while `docker exec
ab-minio ...` fails, which makes Docker healthchecks report false
negatives and would cause autoheal to bounce a working object store
during BirdWeather backlog repair. Docker's own restart policies still
cover real MinIO process exits; for MinIO `unhealthy` / `setns` / `OCI
runtime exec failed` faults, use `scripts/ops/recover_minio.sh` instead
of autoheal.

## Databases

| Database | Used by |
| --- | --- |
| `avian_biosurveillance` | Application tables + continuous aggregates |
| `dagster_db` | Dagster run / event log / schedule storage, isolated from app schema |

## Roles

| Role | Access | Used by |
| --- | --- | --- |
| `root` | Full DDL + DML + SUPERUSER on `avian_biosurveillance` and `dagster_db` | Schema bootstrap, `ingestion`, `frontend`, `dagster-webserver`, `dagster-daemon`, maintenance scripts |

## Dockerfiles

| File | Notes |
| --- | --- |
| `Dockerfile.dagster` | Slim Python image with `dagster`, `dagster-webserver`, `dagster-postgres`, and `dagster-graphql`; bundles no user code |
| `Dockerfile.ingestion` | Two-stage ingestion / orchestration worker without the optional embedding runtime |
| `Dockerfile.model` | Two-stage modeling worker with Dagster, BirdNET / Perch v2 embedding dependencies, and CPU-only PyTorch |
| `Dockerfile.frontend` | Three-stage image: deps → `next build` → standalone runner |

## Configuration

Create `.env` in the repository root (it is gitignored) and set every
required secret below, one `KEY='value'` per line. Runtime topology lives in
`config/runtime.env`; edit that file for ports, bind hosts, public URLs,
startup controls, and the container log time zone.

| Variable | Required | Notes |
| --- | --- | --- |
| `POSTGRES_DB` | Yes | Default `avian_biosurveillance` |
| `POSTGRES_USER` | Yes | Default `root` |
| `POSTGRES_PASSWORD` | Yes | Used by all local/dev services through the `root` role |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | Yes | Used only by `minio-init` for first-boot provisioning |
| `MINIO_APP_USER` / `MINIO_APP_PASSWORD` | Yes | Scoped service account created by `minio-init` |
| `AVIAN_TIMEZONE` | Runtime config | Default `Europe/Amsterdam`; controls container log timestamps, and TimescaleDB follows it for `log_timezone` |
| `CATCHUP_AUTOSTART` | Runtime config | Default `true`; enables startup of the sensor-owned historical catchup (`BACKFILL_AUTOSTART` is still accepted) |
| `BIRDWEATHER_RAW_AUDIO_MAX_WORKERS` | Optional | Default `4`; caps parallel BirdWeather media downloads inside `ab-ingestion` to reduce SSL EOF and timeout churn |
| `EARTH_ENGINE_HIGH_VOLUME` | Optional | Default `true`; uses the GEE high-volume endpoint for grid ingestion |
| `EARTH_ENGINE_SECTION_WORKERS` | Optional | Default `4`; parallel GEE sections per grid cell |
| `EARTH_ENGINE_GRID_CELL_WORKERS` | Optional | Default `2`; parallel grid cells profiled per date |
| `EARTH_ENGINE_GRID_DB_BATCH_SIZE` | Optional | Default `200`; rows per Earth Engine database upsert batch |
| `EARTH_ENGINE_HTTP_POOL_SIZE` | Optional | Default `10`; caps nested GEE workers to avoid HTTP pool churn |
| `NEXT_PUBLIC_DAGSTER_GRAPHQL_URL` | Runtime config | Baked into the JS bundle at build time |
| `KNMI_OPEN_DATA_API`, `KNMI_EDR`, `KNMI_NOTIFICATION`, `KNMI_WEB_MAP_SERVICE` | KNMI ingestion | |
| `EBIRD_API_KEY` | eBird ingestion | |
| `MOVEBANK_USERNAME` / `MOVEBANK_PASSWORD` | Movebank ingestion | |
| `NASA_EARTHDATA_USERNAME` / `NASA_EARTHDATA_PASSWORD` | Earthdata access | |
| `CDS_API_KEY` | CDS drought ingestion | Keep strict `KEY='value'` format |
| `GEMINI_API_KEY` | Sovon ingestion | Bar-chart extraction |
| `HF_TOKEN` | Model downloads | Hugging Face access token |
| `GEE_PROJECT` | GEE ingestion | Earth Engine cloud project |
| `GEE_SERVICE_ACCOUNT_FILE` / `GEE_SERVICE_ACCOUNT_FILE_HOST` | GEE ingestion | In-container and host paths to the service-account JSON file (preferred over inline `GEE_SERVICE_ACCOUNT_CREDENTIALS`) |
| `DOCKERHUB_NAMESPACE`, `SINGULARITY_IMAGE_TAG` | Image publishing | See `infrastructure/singularity/README.md` |

## Dagster Concurrency

Dagster permits up to four concurrent runs globally while still
enforcing one active run per unique `avian/deployment` tag. The lower
ceiling keeps the local ingestion code-server responsive when startup
catchup, raw-audio downloads, and CDS work overlap inside the same
Docker stack.

Dagster run monitoring is also enabled for `STARTING` and `CANCELING`
timeouts. Combined with the daemon-startup run-state reconcile step,
this keeps interrupted local runs from blocking future queueing after a
reboot.

## Ingestion Flows

| Flow | Schedule | Target |
| --- | --- | --- |
| BirdWeather Detections | daily 00:00 | `birdweather_tbl` |
| KNMI Weather | hourly | `knmi_tbl` |
| Sovon Mortality | 1st of month, 04:00 | `sovon_tbl` |
| Data Quality Audit | daily 03:30 | `data_quality_tbl` |
| Station Summary Refresh | daily 00:30 | `birdweather_stations_tbl` |

Schedules are editable from the dashboard's **Pipelines & Schedules**
page.

## Common Operations

```bash
COMPOSE="bash infrastructure/docker/compose.sh"

$COMPOSE logs -f ingestion model          # stream user-code server logs
$COMPOSE restart model                    # restart feature engineering / modeling
$COMPOSE down                             # stop the stack (volumes preserved)
$COMPOSE down -v                          # stop the stack and delete all data
$COMPOSE build --no-cache ingestion model # force rebuild after Python changes
scripts/ops/recover_minio.sh --check      # inspect MinIO HTTP/Docker readiness
scripts/ops/recover_minio.sh              # recreate unhealthy MinIO only
```

MinIO runs as one container and stores objects under `data/minio` in the
repository checkout. This is the same path pattern on a laptop and on a
single on-prem server:

```bash
mkdir -p data/minio
$COMPOSE up -d --no-build --no-deps minio minio-init
scripts/ops/recover_minio.sh --check
```

If an older checkout already has objects in the previous Docker named
volume, copy or mirror those objects into `data/minio` once before
recreating MinIO with this configuration.

`recover_minio.sh` removes and recreates the Compose MinIO container but
does not delete the `data/minio` directory. Use it when MinIO reports
`unhealthy`, logs `drives-online: 0`, or Docker healthchecks fail with
`OCI runtime exec failed` / `setns` errors after Docker Desktop storage
churn. The recreated container may receive a generated name such as
`<id>_ab-minio`; use `bash infrastructure/docker/compose.sh ps -q minio`
or the Compose service label for follow-up commands instead of assuming
the literal container name. If even this helper times out while removing
or recreating MinIO, Docker itself is wedged; restart Docker Desktop (or
the Docker daemon on native Linux) and rerun the helper. Keep
`ab-autoheal` stopped until the recreated MinIO container picks up the
updated Compose metadata; otherwise an older running MinIO container that
still carries the `autoheal=true` label can be restarted again on the
same false-unhealthy healthcheck loop.

## Resource Limits

Tuned for a ~16 GiB development machine. If a single service needs more
headroom, raise the `ingestion` limits first.

| Service | CPU | Memory | Reservation |
| --- | ---: | ---: | ---: |
| `timescaledb` | 2.0 | 2 GiB | 512 MiB |
| `timescaledb-init` | 0.25 | 128 MiB | 64 MiB |
| `minio` | 1.0 | 1 GiB | 512 MiB |
| `minio-init` | 0.25 | 128 MiB | 64 MiB |
| `dagster-webserver` | 0.75 | 512 MiB | 256 MiB |
| `dagster-daemon` | 0.5 | 512 MiB | 128 MiB |
| `ingestion` | 5.0 | 4 GiB | 1 GiB |
| `model` | 4.0 | 6 GiB | 1 GiB |
| `frontend` | 0.75 | 512 MiB | 128 MiB |

Check live usage with `docker stats --no-stream` before adjusting.

## Manual Image Publishing

```bash
# Build and push with branch + SHA tags
./scripts/tooling/publish_docker_images.sh -n soheilmp

# Add the `latest` tag (release builds)
./scripts/tooling/publish_docker_images.sh -n soheilmp --release

# Publish a specific subset of services
./scripts/tooling/publish_docker_images.sh -n soheilmp -s frontend,ingestion,model

# Explicit release tag
./scripts/tooling/publish_docker_images.sh -n soheilmp -t 2026-03-31 --release
```

## Volumes

| Volume | Stores |
| --- | --- |
| `timescaledb-data` | All TimescaleDB data (application tables + Dagster orchestration metadata) |
| `minio-data` | Raw audio, processed audio, and feature artifacts |
| `ingestion-raw-data` | Persistent ETL raw/cache artifacts (Sovon historical chart, JSON caches) |
