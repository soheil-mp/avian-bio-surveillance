# Ops (`avian_biosurveillance.ops`)

Operational, CLI-driven jobs for backfills and maintenance. These are intentionally separate from the Dagster orchestration: they call the existing ingestion, processing, and modeling entry points directly, in bounded batches with resumable progress, which makes them well suited to one-off historical catchup and repair.

## Modules

| Module | Purpose |
|---|---|
| `ensure_historical_window.py` | Ensures the 2024–2025 historical ingestion and feature window is complete; records resumable progress under `data/processed/coverage`. |
| `run_birdweather_local_backlog.py` | Runs the 2024–2025 BirdWeather backlog with local raw-audio staging (download → crop → upload → prune). |
| `run_birdweather_feature_backfill.py` | One-off backfill of BirdWeather feature columns for already-processed clips. |
| `prune_birdweather_raw_audio.py` | Safely prunes BirdWeather raw-audio objects from MinIO. |
| `run_modeling_backfill.py` | Runs a full persisted modeling backfill. |

Each is run as a Python module with its own flags, for example:

```bash
python -m avian_biosurveillance.ops.run_modeling_backfill --help
```
