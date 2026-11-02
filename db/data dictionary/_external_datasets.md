<!-- Static sidecar — appended by generate_dict.py to the auto-generated data_dictionary.md.
     Update this file to document datasets that live outside the TimescaleDB instance. -->

## AudioDB (MinIO Object Storage)

- **Source object:** Internal MinIO service `minio`
- **Classification:** S3-compatible object-storage layer rather than a row-oriented analytical dataset.
- **Grain:** One object per stored audio artifact.
- **Logical key:** `(bucket, object_name)`
- **Overview:** Private object store used to persist raw and processed audio files for ingestion, audio playback, and downstream processing. In this project, MinIO functions as infrastructure-backed storage, so it is documented here as a storage-layer source rather than as a tabular data source.

| Storage element | Type | Access | Typical contents | Typical key / identifier pattern | Notes |
|---|---|---|---|---|---|
| `raw-audio` | Bucket | Private | Original audio captured from BirdWeather or other acoustic-recording workflows. | Example object name from client docs: `2024-01-15/station_123/det_456.mp3` | Primary landing zone for source audio before downstream transformations. |
| `processed-audio` | Bucket | Private | Derived audio assets such as filtered, segmented, or clip-level exports. | Usually follows a pipeline-defined object path under the processed bucket. | Used for downstream playback, QA, and model-oriented artifacts. |
