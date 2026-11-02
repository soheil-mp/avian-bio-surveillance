-- =============================================================================
-- Domain baseline DDL for unified species tracker aggregates.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: species_tracker_tbl (Derived regular table)
-- =========================================================================
-- Purpose: Unified period/location/species tracker surface for the frontend
--          and analytical queries.
-- Grain  : One row per (row_kind, period_grain, period_start, location,
--          species). Community rows use species_id = NULL.
-- Refresh: refresh_species_tracker()
--
-- Notes:
--   * Bird detections come from birdweather_daily_cagg_tbl.
--   * Network rows use location_type = 'network' and location_id = 'nl'.
--   * KNMI joins at station/network grain; GEE and CDS drought join at grid-cell grain.
--   * Seismic metrics are exposure summaries around the station/grid centroid.
-- =========================================================================

CREATE TABLE IF NOT EXISTS species_tracker_tbl (

    species_tracker_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Identity dimensions
    row_kind TEXT NOT NULL,
    period_grain TEXT NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    period_day_count INTEGER NOT NULL,
    location_type TEXT NOT NULL,
    location_id TEXT NOT NULL,
    station_id TEXT,
    grid_cell_id TEXT,
    grid_system TEXT,
    species_id TEXT,

    -- Ecological metrics
    total_detections BIGINT NOT NULL DEFAULT 0,
    active_days INTEGER NOT NULL DEFAULT 0,
    active_station_count INTEGER NOT NULL DEFAULT 0,
    species_station_count INTEGER,
    detections_per_active_day DOUBLE PRECISION,
    temporal_occupancy_pct DOUBLE PRECISION,
    relative_abundance_pct DOUBLE PRECISION,
    station_presence_pct DOUBLE PRECISION,
    species_richness INTEGER,
    shannon_h DOUBLE PRECISION,
    dominance_index DOUBLE PRECISION,
    pielou_evenness DOUBLE PRECISION,
    simpson_d DOUBLE PRECISION,
    target_species_richness INTEGER,
    target_species_share_pct DOUBLE PRECISION,

    -- Audio / classifier metrics (per-species rows; community rows use the
    -- whole-community pool of detections).
    confidence_mean DOUBLE PRECISION,
    confidence_min DOUBLE PRECISION,
    confidence_max DOUBLE PRECISION,
    confidence_p10 DOUBLE PRECISION,
    confidence_p50 DOUBLE PRECISION,
    confidence_p90 DOUBLE PRECISION,
    high_confidence_detections BIGINT,
    high_confidence_pct DOUBLE PRECISION,
    processed_clip_count BIGINT,
    processed_clip_pct DOUBLE PRECISION,
    embedding_count BIGINT,

    -- Diel / phenology
    first_detection_at TIMESTAMPTZ,
    last_detection_at TIMESTAMPTZ,
    peak_hour_utc INTEGER,
    peak_hour_share_pct DOUBLE PRECISION,
    nocturnal_share_pct DOUBLE PRECISION,
    dawn_share_pct DOUBLE PRECISION,
    dusk_share_pct DOUBLE PRECISION,

    -- Background acoustic scene (from background_sound_clf top-tag-per-detection)
    background_top_label TEXT,
    background_top_share_pct DOUBLE PRECISION,
    background_label_mix JSONB,

    -- Station-grain quality (NULL for grid rows). Sourced from
    -- birdweather_stations_tbl which is the authoritative station-quality store.
    station_lifetime_availability_pct DOUBLE PRECISION,
    station_availability_90d_pct DOUBLE PRECISION,
    station_days_since_last INTEGER,
    station_data_quality_grade TEXT,

    -- Weather and environmental covariates
    knmi_temp_mean_c DOUBLE PRECISION,
    knmi_temp_min_c DOUBLE PRECISION,
    knmi_temp_max_c DOUBLE PRECISION,
    knmi_precip_mm DOUBLE PRECISION,
    knmi_humidity_mean_pct DOUBLE PRECISION,
    knmi_wind_max_ms DOUBLE PRECISION,
    knmi_solar_mean_wm2 DOUBLE PRECISION,

    gee_ndvi_mean DOUBLE PRECISION,
    gee_temp2m_mean_c DOUBLE PRECISION,
    gee_soil_moisture_mean DOUBLE PRECISION,
    gee_precip_mean_mm DOUBLE PRECISION,
    gee_cloud_cover_mean_pct DOUBLE PRECISION,

    cds_spi_1m_mean DOUBLE PRECISION,
    cds_spi_3m_mean DOUBLE PRECISION,
    cds_spei_3m_mean DOUBLE PRECISION,
    cds_spei_12m_mean DOUBLE PRECISION,

    seismic_event_count INTEGER,
    seismic_max_magnitude DOUBLE PRECISION,
    seismic_mean_magnitude DOUBLE PRECISION,
    seismic_nearest_event_km DOUBLE PRECISION,

    -- JSONB sidecars
    species_info JSONB NOT NULL DEFAULT '{}',
    audio_artifacts JSONB NOT NULL DEFAULT '{}',
    metric_provenance JSONB NOT NULL DEFAULT '{}',
    extras JSONB NOT NULL DEFAULT '{}',

    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================================================================
-- Indexes
-- Custom indexes are limited to the primary tracker query paths.
-- =========================================================================

CREATE UNIQUE INDEX IF NOT EXISTS idx_spt_natural_key
    ON species_tracker_tbl (
        row_kind,
        period_grain,
        period_start,
        location_type,
        location_id,
        COALESCE(species_id, '')
    );

CREATE INDEX IF NOT EXISTS idx_spt_location_period
    ON species_tracker_tbl (location_type, location_id, period_grain, period_start DESC);

CREATE INDEX IF NOT EXISTS idx_spt_species_period
    ON species_tracker_tbl (species_id, period_grain, period_start DESC)
    WHERE species_id IS NOT null;

CREATE INDEX IF NOT EXISTS idx_spt_frontend_network_species
        ON species_tracker_tbl (period_grain, species_id, period_start DESC)
        WHERE row_kind = 'species'
            AND location_type = 'network'
            AND location_id = 'nl'
            AND species_id IS NOT null;

CREATE INDEX IF NOT EXISTS idx_spt_grid_period
    ON species_tracker_tbl (grid_cell_id, period_grain, period_start DESC)
    WHERE grid_cell_id IS NOT null;

CREATE INDEX IF NOT EXISTS idx_spt_period_start
    ON species_tracker_tbl (period_start DESC);

CREATE INDEX IF NOT EXISTS idx_spt_computed_at_desc
    ON species_tracker_tbl (computed_at DESC);

CREATE INDEX IF NOT EXISTS idx_spt_species_info_gin
    ON species_tracker_tbl USING gin (species_info jsonb_path_ops);

CREATE INDEX IF NOT EXISTS idx_spt_audio_artifacts_gin
    ON species_tracker_tbl USING gin (audio_artifacts jsonb_path_ops);

COMMENT ON TABLE species_tracker_tbl IS
    'Unified tracker table at period x location x species/community grain '
    'with ecological metrics, classifier/audio metrics, environmental '
    'covariates, station-grain quality, and JSONB sidecars '
    '(species_info, audio_artifacts, metric_provenance, extras). '
    'Rebuilt by refresh_species_tracker().';

COMMENT ON COLUMN species_tracker_tbl.row_kind IS
    'species = one row per species; community = one row for the whole location-period community.';

COMMENT ON COLUMN species_tracker_tbl.location_type IS
    'station rows summarize one BirdWeather station; grid rows summarize one canonical grid cell.';

COMMENT ON COLUMN species_tracker_tbl.dominance_index IS
    'Berger-Parker dominance index: percent of detections from the dominant species in the location-period.';

COMMENT ON COLUMN species_tracker_tbl.pielou_evenness IS
    'Pielou''s J = shannon_h / ln(species_richness); NULL when richness < 2.';

COMMENT ON COLUMN species_tracker_tbl.simpson_d IS
    'Simpson''s diversity (1 - sum(p_i^2)); complements Shannon for low-richness panels.';

COMMENT ON COLUMN species_tracker_tbl.target_species_richness IS
    'Count of distinct target species (config/target_species.json) detected in the location-period.';

COMMENT ON COLUMN species_tracker_tbl.background_label_mix IS
    'JSONB {label: share_pct, ...} from background_sound_clf top-tag-per-detection over the location-period.';

COMMENT ON COLUMN species_tracker_tbl.station_data_quality_grade IS
    'Coarse station grade derived from availability_90d_pct (>=90 A, >=60 B, else C). NULL for grid rows.';

COMMENT ON COLUMN species_tracker_tbl.species_info IS
    'Cached species_tbl projection: taxonomy, alpha codes, color, thumbnail, target-species flag, etc.';

COMMENT ON COLUMN species_tracker_tbl.audio_artifacts IS
    'JSONB manifest of lightweight audio artifact counts '
    '(currently clip_count and embedding_count).';

COMMENT ON COLUMN species_tracker_tbl.metric_provenance IS
    'Refresh-time audit trail: refresh_version, source caggs, period windows, target species count.';

COMMENT ON COLUMN species_tracker_tbl.extras IS
    'Open extension slot for future metrics; default empty object.';

COMMIT;
