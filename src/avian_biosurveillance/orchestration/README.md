# Orchestration

Dagster-based scheduling and pipeline execution for the Avian Biosurveillance
platform. Each data source and maintenance task is a separate flow so failures
in one do not cascade into others.

## Architecture

The runtime has four cooperating process types (see
[`docs/adr/0001-migrate-prefect-to-dagster.md`](../../../docs/adr/0001-migrate-prefect-to-dagster.md)):

| Process | Purpose |
|---|---|
| `serve.py` in `ab-ingestion` | Long-running **gRPC code-server** for ingestion, audio download/processing, post-processing, schedules, and sensors. |
| `serve.py` in `ab-model` | Long-running **gRPC code-server** for modeling-owned assets, currently `birdweather_feature_engineering` with BirdNET / Perch v2 embeddings. |
| `dagster-webserver` | UI + GraphQL endpoint (port `3030` by default). |
| `dagster-daemon` | Cron scheduler, sensors, and the `QueuedRunCoordinator` that enforces per-deployment concurrency limits. |

Each flow module remains plain Python and exports `run_*` callables. Dagster
wraps them as `@op` / `@job` at registration time inside
[`dagster_defs/jobs.py`](dagster_defs/jobs.py).

## `dagster_defs/` package layout

| Module | Responsibility |
|---|---|
| [`assets.py`](dagster_defs/assets.py) | Software-defined post-processing, audio, and modeling assets. The ingestion location owns station summaries, raw-audio queue/download, audio processing, quality audit, and species tracker; the model location owns feature engineering. |
| [`code_locations.py`](dagster_defs/code_locations.py) | Filters assets/jobs by `AVIAN_DAGSTER_CODE_LOCATION` so Docker can run separate `ingestion` and `model` code locations while tests keep the default all-in-one definitions. |
| [`ingestion_assets.py`](dagster_defs/ingestion_assets.py) | One source-of-truth ingestion asset per data source (`birdweather_raw_data`, `knmi_raw_data`, `seismic_raw_data`, `ebird_raw_data`, `movebank_raw_data`, `earth_engine_raw_data`, `earth_engine_grid_raw_data`, `cds_drought_raw_data`, `sovon_raw_data`). Every asset carries an `op_tags={"avian/deployment": ...}` and a shared `RetryPolicy` for transient network errors. |
| [`jobs.py`](dagster_defs/jobs.py) | Single source of truth that maps every flow key in `config/schedules.json` to a `JobDefinition` (asset jobs for ingestion / postprocessing, op jobs for repairs and catchups). Consumed by both `schedules.py` and `sensors.py`. |
| [`schedules.py`](dagster_defs/schedules.py) | Builds Dagster schedules from `config/schedules.json` only for deployments whose `execution_modes` include `cron`; manual, sensor-owned, and auto-materialized entries stay visible in config without creating stopped schedules. |
| [`sensors.py`](dagster_defs/sensors.py) | Five sensors that drive the dependency graph end-to-end. `depends_on` now feeds the startup post-processing and daily-quality sensors instead of being documentation only (see below). |
| [`naming.py`](dagster_defs/naming.py) | Shared run-key, tag, and automation-name builders. |

## Schedule config model

`config/schedules.json` now carries two kinds of metadata:

| Field | Meaning |
|---|---|
| `execution_modes` | Declares how a deployment is triggered. The current code understands `cron` for schedule registration; other values (`startup`, `sensor`, `auto`, `manual`) are declarative metadata used to explain orchestration ownership and simplify the config. |
| `depends_on` | Declares logical upstream deployments. The startup post-processing and daily-quality sensors now read it directly; the asset graph remains the source of truth for auto-materialized asset chaining. |

`enabled` only controls the default on/off status of cron-backed schedules. `run_on_startup` is separate: the startup sensors use it for boot-time bootstrap runs, even for non-cron backstops such as `station-summary-refresh`.

## Sensors

| Sensor | Default | Purpose |
|---|---|---|
| `launch_startup_ingestion_runs` | running | Fires every Phase-1 deployment with `run_on_startup: true` once per code-server boot, even when older Dagster history exists. |
| `launch_startup_postprocessing_runs` | running | Watches each startup post-processing backstop and fires it once per code-server boot when one of its configured upstream dependencies in `depends_on` has a SUCCESS run. |
| `launch_daily_quality_runs` | running | Watches the `quality-audit.depends_on` cohort. As soon as every declared upstream daily ingestion has a SUCCESS run for the same `avian/logical_date` value, launches the quality audit for that logical date. |
| `launch_birdweather_catchup_runs` | running | Walks the configured BirdWeather date window from start to end. Launches one chronological multi-day `run_birdweather_catchup_flow` window per tick, batches audio and feature follow-up work over that window, and defers the expensive species-tracker rebuild until the final catchup window. |
| `launch_historical_catchup_runs` | running | Walks the configured shared non-BirdWeather historical date window from start to end. Launches one chronological multi-day `run_historical_catchup_flow` window per tick, only advancing once the previous window has reached SUCCESS, with up to 3 retries per failed window. The shared backlog is no longer blocked behind BirdWeather catchup, its follow-up work is batched over each window, and the expensive species-tracker rebuild is deferred until the final catchup window. Set `CATCHUP_AUTOSTART=false` to keep a local backlog paused. Legacy `BACKFILL_AUTOSTART` is still accepted. |

## Asset graph

```
birdweather_raw_data --> birdweather_audio_download_queue --> birdweather_raw_audio_objects --> birdweather_audio_processing --> birdweather_feature_engineering
birdweather_raw_data --> recording_station_summary
birdweather_raw_data + knmi_raw_data + ... + sovon_raw_data --> data_quality_audit_results
recording_station_summary + species_dimension + birdweather_feature_engineering + knmi_raw_data + environmental assets --> species_tracker
```

`AutoMaterializePolicy.eager()` is enabled for every post-processing asset and
the daemon's `auto_materialize` block in [`infrastructure/docker/dagster.yaml`](../../../infrastructure/docker/dagster.yaml)
is turned on, so the daemon launches the downstream chain automatically as
soon as upstream parents materialise.

## Flows

| Module | Flow | Schedule | Tables |
|--------|------|----------|--------|
| `birdweather_flow.py` | `run_birdweather_flow` | daily 00:00 | `birdweather_tbl` |
| `birdweather_flow.py` | `run_birdweather_repair_flow` | manual only | `birdweather_tbl` |
| `knmi_flow.py` | `run_knmi_flow` | daily 00:30 | `knmi_tbl` |
| `knmi_flow.py` | `run_knmi_repair_flow` | manual only | `knmi_tbl` |
| `knmi_flow.py` | `run_seismic_flow` | daily 02:30 | `seismic_tbl` |
| `ebird_flow.py` | `run_ebird_flow` | daily 01:00 | `ebird_tbl` |
| `ebird_flow.py` | `run_ebird_repair_flow` | manual only | `ebird_tbl` |
| `movebank_flow.py` | `run_movebank_flow` | daily 01:30 | `movebank_tbl` |
| `movebank_flow.py` | `run_movebank_repair_flow` | manual only | `movebank_tbl` |
| `sovon_flow.py` | `run_sovon_flow` | daily 02:00 | `sovon_tbl` |
| `earth_engine_flow.py` | `run_earth_engine_grid_flow` | daily 03:00 | `google_earth_engine_tbl` |
| `earth_engine_flow.py` | `run_earth_engine_grid_repair_flow` | manual only | `google_earth_engine_tbl` |
| `cds_drought_flow.py` | `run_cds_drought_flow` | daily 03:40 | `cds_drought_tbl` |
| `cds_drought_flow.py` | `run_cds_drought_catchup_flow` | monthly 06:00 (5th) + startup | `cds_drought_tbl` |
| `historical_daily_pipeline_flow.py` | `run_historical_catchup_flow` | sensor-driven / manual, batched chronological windows | KNMI + eBird + Earth Engine |
| `station_summary_flow.py` | `run_station_summary_flow` | in-flow / sensor-driven | `birdweather_stations_tbl` |
| `derived_aggregates_flow.py` | `run_derived_aggregates_flow` | schedule-backed asset job | `species_tbl` + `movebank_tracks_tbl` + `species_tracker_tbl` |
| `data_quality_flow.py` | `run_quality_flow` | sensor-driven after daily cohort | `data_quality_tbl` |
| `processing/birdweather_raw_audio.py` | `birdweather_raw_audio_ingestion` asset job | auto-materialized after BirdWeather table ingestion | `raw-audio` MinIO |
| `processing/birdweather_audio.py` | `birdweather_audio_processing` asset | auto-materialized after raw audio objects land | `processed-audio` MinIO + `birdweather_tbl.has_processed_clip` |
| `features/birdweather_features.py` | `birdweather_feature_engineering` asset in `ab-model` | auto-materialized after audio processing | `features` MinIO + BirdWeather feature columns |
| `serve.py` | — | — | Hosts the gRPC code-server consumed by the Dagster webserver/daemon |

All deployments are tagged with `avian/deployment=<name>`; the
`QueuedRunCoordinator` enforces `tag_concurrency_limits` of 1 per unique value
so overlapping runs of the same job are queued.

`daily_pipeline_flow.py` is now treated as a legacy compatibility helper for
manual Python callers. The active orchestration model is the Dagster
schedule/sensor/asset graph above.

The day-granular historical backlog is no longer scheduled as five separate
range jobs. Instead, the catchup sensors advance a cursor across the configured
date windows and launch chronological multi-day runs whose width is controlled
by `followup_batch_days`. Each run performs the selected source ingestions and
then batches the relevant follow-up work over that completed window.
BirdWeather catchup batches its raw-audio, audio-processing, and
feature-engineering work instead of repeating those steps for every single day,
while deferring the expensive `refresh_species_tracker()` rebuild until the
final catchup window. The shared historical backlog similarly batches
quality audits per window, defers the expensive
species-tracker rebuild until the final catchup window, and can advance
alongside BirdWeather instead of waiting for BirdWeather to finish the full
backlog. The source-specific repair jobs still exist as manual Dagster jobs,
but they are no longer expected to appear as stopped cron schedules once their
`execution_modes` omit `cron`.

Set `CATCHUP_AUTOSTART=true` on the Dagster code-server container to start the
historical backlog automatically when containers come up. Optional overrides are
`CATCHUP_START_DATE`, `CATCHUP_END_DATE`, `CATCHUP_TARGET`, and
`CATCHUP_SOURCES` (comma-separated; default
`birdweather,knmi,ebird,earth_engine_grid`). The sensors still advance
strictly oldest-first and wait for success before moving to the next window.
Legacy
`BACKFILL_*` names are still accepted for compatibility.

## Usage

```bash
# Start the gRPC code-server (registers all jobs/schedules/sensors)
python -m avian_biosurveillance.orchestration.serve

| `launch_birdweather_catchup_runs` | running | Walks the configured BirdWeather date window from start to end. Launches one `run_birdweather_catchup_flow` run per tick and batches tracker, audio, and feature follow-up work over completed ranges so BirdWeather catchup can keep moving. |
| `launch_historical_catchup_runs` | running | Walks the configured shared non-BirdWeather historical date window from start to end. Launches one `run_historical_catchup_flow` run per tick, only advancing once the previous date has reached SUCCESS, with up to 3 retries per failed day. The shared backlog is no longer blocked behind BirdWeather catchup, and its follow-up work is batched over completed ranges. Set `CATCHUP_AUTOSTART=false` to keep a local backlog paused. Legacy `BACKFILL_AUTOSTART` is still accepted. |
from avian_biosurveillance.orchestration.knmi_flow import run_knmi_repair_flow
run_knmi_repair_flow(start_date='2024-01-01', end_date='2024-01-31')
"
```
then batches the relevant follow-up work over completed ranges. BirdWeather
catchup batches its raw-audio, audio-processing, feature-engineering, and
species-tracker work instead of repeating those steps for every single day.
The shared historical backlog similarly batches quality audits and
species-tracker refreshes, which lets it advance alongside
BirdWeather instead of waiting for BirdWeather to finish the full backlog.
Sovon remains a non-date-granular source and is bootstrapped independently via
its daily-plus-startup flow. The source-specific repair jobs still exist as manual Dagster jobs,
