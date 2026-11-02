# Features (`avian_biosurveillance.features`)

Feature engineering for processed BirdWeather clips: it turns each detection's audio into the learned embeddings used by the anomaly model and the visual artifacts used by the dashboard.

## Modules

| Module | Purpose |
|---|---|
| `birdweather_features.py` | The BirdWeather feature-engineering pipeline. |
| `embeddings.py` | BirdNET and Perch v2 embedding adapters. When no real backend is configured, callers receive an explicit `backend_unavailable` status rather than placeholder vectors. |
| `audio_artifacts.py` | Renders image artifacts (mel-spectrograms, waveforms) for processed clips. |
| `materialize_birdweather_visual_artifacts.py` | Materializes saved waveform and spectrogram artifacts on demand. |

Embeddings and visual artifacts are stored in MinIO and referenced from the database.
