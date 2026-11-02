# Processing (`avian_biosurveillance.processing`)

Audio processing for raw BirdWeather soundscapes. This layer is kept separate from detection-table ingestion so the slow download/crop/upload step can be modeled as its own Dagster asset.

## Modules

| Module | Purpose |
|---|---|
| `birdweather_audio.py` | Crops raw soundscapes to their BirdNET detection windows and stores the resulting mono FLAC clips in MinIO's `processed-audio` bucket. |
| `birdweather_raw_audio.py` | Raw-audio queue and downloader, kept independent of detection-table ingestion. |
| `birdweather_raw_storage.py` | Helpers for locally staged BirdWeather raw-audio files. |
