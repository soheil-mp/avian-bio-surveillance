# Modeling (`avian_biosurveillance.modeling`)

The modeling phase turns processed detections, audio embeddings, environmental context, and population activity into a **weekly anomaly score for each target species in each 5×5 km grid cell**. Because there are no clip-level disease labels, the approach is unsupervised: the model learns what normal sound and activity look like from history, then ranks weeks that depart from that baseline. The output is a decision-support signal for expert review, not a disease diagnosis.

## Signals

The score fuses three weekly signals with an Isolation Forest:

1. **Bioacoustic drift** — cosine distance from each clip's BirdNET/Perch v2 embedding to historical clips of the same species, calendar week, and (where data allows) grid cell and time of day. Sparse cells widen their search rather than rely on an unstable local baseline.
2. **Population activity** — effort-normalized weekly features (relative abundance, temporal occupancy, station presence, detections per active station-day) compared against the same week in prior years using robust median/MAD statistics.
3. **Environmental context** — lagged weather and drought values used as continuous context (no thresholds).

## Modules

| Module | Purpose |
|---|---|
| `pipeline.py` | Daily modeling pipeline that produces the anomaly scores. |
| `anomaly_model.py` | Higher-level unsupervised anomaly scoring over bird-behavior streams. |
| `embedding_drift.py` | kNN embedding-drift scoring for processed clips. |
| `embedding_projection.py` | Offline PCA + kNN cosine-isolation alert-score precompute (offline counterpart to the alerts API hot path). |
| `count_baseline.py` | Robust seasonal population-activity baselines for weekly anomalies. |
| `feature_set.py` | Species-week feature-set assembly. |
| `environmental_context.py` | Lagged environmental context features for the anomaly model. |
| `sovon_context.py` | SOVON mortality context for retrospective interpretation. |
| `evaluation.py` | Weak-label evaluation of daily modeling artifacts against SOVON mortality. |
| `tuning.py` | Unsupervised model-selection sweep, ranked by alignment with SOVON mortality context. |
| `settings.py` | Loader for the modeling configuration file. |
| `schemas.py` | Shared schemas for modeling artifacts and run metadata. |

## Configuration

Tunable parameters — baseline windows, embedding-drift settings, and the unsupervised anomaly model — live in `config/modeling.json`, loaded via `settings.py`, which is the single source of truth. The tuning sweep reads `config/modeling_tuning.json`.

## Running

The modeling phase normally runs as part of the Dagster pipeline. For a full persisted backfill outside the scheduler, use `ops/run_modeling_backfill.py`.
