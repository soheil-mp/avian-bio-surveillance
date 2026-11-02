-- =============================================================================
-- Domain baseline DDL for BirdWeather detections.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: birdweather_tbl (TimescaleDB hypertable, 1-day chunks)
-- =========================================================================
-- Purpose: Primary acoustic event store for BirdWeather detections.
-- Grain  : One row per classified detection event.
-- PK     : (detection_id, detected_at)
-- Model  : BirdWeatherDetection (db/models/birdweather.py)
-- Loader : upsert_birdweather_detections (db/loaders/birdweather.py)
--
-- Normalisation details:
--   * References species_tbl via species_id.
--   * Logically references birdweather_stations_tbl via station_id (also the source
--     of geographic coordinates — a detection happens at its station, so
--     per-event lat/lon would only differ for mobile/app stations which
--     are excluded from the credible-tier analytical path).
--   * Per-clip duration is derived as audio_end_time_s - audio_start_time_s
--     and is therefore not carried as a separate column.
--   * Value-range validation deferred to the data_quality_tbl pipeline.
--   * No compression or retention policies defined during dev phase.
-- =========================================================================

CREATE TABLE IF NOT EXISTS birdweather_tbl (

    -- Identity & Timestamp
    detection_id TEXT NOT NULL,
    observation_date DATE NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    soundscape_at TIMESTAMPTZ,

    -- FK: species_tbl
    species_id TEXT NOT NULL,

    -- Logical FK: birdweather_stations_tbl (also supplies detection coordinates)
    station_id TEXT NOT NULL,

    -- Detection Metrics
    confidence DOUBLE PRECISION NOT NULL,
    probability DOUBLE PRECISION,
    score DOUBLE PRECISION,
    certainty TEXT,
    eclipse BOOLEAN,            -- Night-time detection flag

    -- Audio Context
    soundscape_id TEXT,
    audio_filename TEXT,
    audio_start_time_s REAL,
    audio_end_time_s REAL,
    audio_filesize_bytes BIGINT,
    audio_url TEXT,

    -- Dynamic station environmental conditions observed at detection time
    station_weather JSONB,
    station_air_pollution JSONB,
    station_sensors JSONB,

    -- AST Enrichment
    -- Format:
    -- {"status": "classified" | "audio_not_downloaded" |
    --  "decode_failed", "tags": [{"label": "Rain", "score": 0.82}, ...]}
    background_sound_clf JSONB,

    -- Feature Engineering
    birdnet_embedding VECTOR,
    perch_v2_embedding VECTOR,
    feature_artifacts JSONB,

    -- Precomputed projection + alert-score per embedding backend.
    -- Populated by avian_biosurveillance.modeling.embedding_projection; consumed
    -- by the frontend /alerts embedding-map endpoint so it never has to fetch
    -- the raw embedding text or run PCA + kNN on the hot path.
    -- Coordinates are scaled to [0, 1] (canvas-ready, with y already flipped so
    -- positive PC2 reads visually "up"). alert_score is clipped to [0, 1].
    birdnet_pc1 REAL,
    birdnet_pc2 REAL,
    birdnet_alert_score REAL,
    perch_v2_pc1 REAL,
    perch_v2_pc2 REAL,
    perch_v2_alert_score REAL,

    -- Indicates if processed artifacts (clip, waveform, spectrogram) exist in MinIO
    has_processed_clip BOOLEAN NOT NULL DEFAULT false,

    PRIMARY KEY (detection_id, detected_at)
);

SELECT create_hypertable(
    'birdweather_tbl',
    'detected_at',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => true
);

-- =========================================================================
-- Forward-compatible column adds for pre-existing databases. The CREATE TABLE
-- above carries the canonical column list for fresh installs; these idempotent
-- ALTERs let `init_database.sh` add the same columns to an existing instance
-- without recreating the hypertable. Safe to replay.
-- =========================================================================

ALTER TABLE birdweather_tbl
    ADD COLUMN IF NOT EXISTS birdnet_pc1 REAL,
    ADD COLUMN IF NOT EXISTS birdnet_pc2 REAL,
    ADD COLUMN IF NOT EXISTS birdnet_alert_score REAL,
    ADD COLUMN IF NOT EXISTS perch_v2_pc1 REAL,
    ADD COLUMN IF NOT EXISTS perch_v2_pc2 REAL,
    ADD COLUMN IF NOT EXISTS perch_v2_alert_score REAL;

-- =========================================================================
-- Indexes
-- Time-range scans depend on TimescaleDB chunk pruning.
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Supports species filtering
CREATE INDEX IF NOT EXISTS idx_bwt_species_id
    ON birdweather_tbl (species_id);

-- Supports station filtering
CREATE INDEX IF NOT EXISTS idx_bwt_station_id
    ON birdweather_tbl (station_id);

-- Supports top-N species per station and joint queries
CREATE INDEX IF NOT EXISTS idx_bwt_station_species_id
    ON birdweather_tbl (station_id, species_id);

-- Dashboard latest-feed and freshness scans
CREATE INDEX IF NOT EXISTS idx_bwt_detected_at_desc
    ON birdweather_tbl (detected_at DESC);

-- Dashboard daily coverage scans use the date projection, not the hypertable key
CREATE INDEX IF NOT EXISTS idx_bwt_observation_date
    ON birdweather_tbl (observation_date);

-- Partial index for latest processed clips feed
CREATE INDEX IF NOT EXISTS idx_bwt_has_processed_clip
    ON birdweather_tbl (detected_at DESC)
    WHERE has_processed_clip;

-- Partial index for the embedding-map query path: filters on
-- has_processed_clip + at least one populated embedding column, then ranges
-- on species_id and detected_at. Leading (species_id, detected_at DESC) gives
-- an ordered key-range scan per species without touching non-embedded rows.
CREATE INDEX IF NOT EXISTS idx_bwt_embedded_species_detected
    ON birdweather_tbl (species_id, detected_at DESC)
    WHERE has_processed_clip
      AND (birdnet_embedding IS NOT null OR perch_v2_embedding IS NOT null);

CREATE INDEX IF NOT EXISTS idx_bwt_embedded_detected_at
    ON birdweather_tbl (detected_at DESC)
    WHERE has_processed_clip
      AND (birdnet_embedding IS NOT null OR perch_v2_embedding IS NOT null);

COMMENT ON TABLE birdweather_tbl IS
    'BirdWeather acoustic detections, one row per classified event, partitioned by detected_at.';
COMMENT ON COLUMN birdweather_tbl.background_sound_clf IS
    'JSONB envelope with the AST background-sound classification status and tags.';
COMMENT ON COLUMN birdweather_tbl.birdnet_embedding IS
    'BirdNET embedding for the processed detection clip, stored as pgvector.';
COMMENT ON COLUMN birdweather_tbl.perch_v2_embedding IS
    'Perch v2 embedding for the processed detection clip, stored as pgvector.';
COMMENT ON COLUMN birdweather_tbl.feature_artifacts IS
    'JSONB manifest of processed audio, spectrogram, waveform, and embedding feature artifacts in MinIO.';
COMMENT ON COLUMN birdweather_tbl.has_processed_clip IS
    'Indicates if a cropped processed clip is available in MinIO.';

COMMIT;
