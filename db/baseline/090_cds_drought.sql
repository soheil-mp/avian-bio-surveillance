-- =============================================================================
-- Domain baseline DDL for Copernicus CDS drought indicators.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: cds_drought_tbl (TimescaleDB hypertable, 1-year chunks)
-- =========================================================================
-- Purpose: Monthly drought indicators from Copernicus CDS ERA5-derived products.
-- Grain  : One row per cell x month x dataset_type x product_type x ensemble x version.
-- PK     : (grid_cell_id, observation_month, dataset_type, product_type, ensemble_member, data_version)
-- Source : CDS dataset `derived-drought-historical-monthly`.
-- Model  : CDSDroughtObservation (db/models/cds_drought.py)
-- Loader : upsert_cds_drought_observations (db/loaders/cds_drought.py)
--
-- Notes:
--   * Accumulation periods are mapped to SPI/SPEI columns (1/3/6/12/24/36/48 months).
--   * ensemble_member=0 (deterministic), 1-9 (ERA5 ens01-ens09). 
--   * Quality fields populated only for consolidated requests due to CDS limitations.
--   * Value-range validation deferred to the data_quality_tbl pipeline.
-- =========================================================================

CREATE TABLE IF NOT EXISTS cds_drought_tbl (

    -- Identity & temporal grain
    grid_cell_id TEXT NOT NULL,
    observation_month DATE NOT NULL,
    dataset_type TEXT NOT NULL,
    product_type TEXT NOT NULL,
    ensemble_member SMALLINT NOT NULL DEFAULT 0,
    data_version TEXT NOT NULL DEFAULT '1_0',

    -- Grid geometry (WGS 84)
    grid_system TEXT NOT NULL DEFAULT 'cds_latlon_0p25deg',
    source TEXT NOT NULL DEFAULT 'copernicus_cds_era5_drought',
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    bbox_min_lat DOUBLE PRECISION NOT NULL,
    bbox_min_lon DOUBLE PRECISION NOT NULL,
    bbox_max_lat DOUBLE PRECISION NOT NULL,
    bbox_max_lon DOUBLE PRECISION NOT NULL,
    resolution_degrees DOUBLE PRECISION NOT NULL DEFAULT 0.25,

    -- SPI / SPEI (standard deviations from climatology)
    spi_1m DOUBLE PRECISION,
    spi_3m DOUBLE PRECISION,
    spi_6m DOUBLE PRECISION,
    spi_12m DOUBLE PRECISION,
    spi_24m DOUBLE PRECISION,
    spi_36m DOUBLE PRECISION,
    spi_48m DOUBLE PRECISION,
    spei_1m DOUBLE PRECISION,
    spei_3m DOUBLE PRECISION,
    spei_6m DOUBLE PRECISION,
    spei_12m DOUBLE PRECISION,
    spei_24m DOUBLE PRECISION,
    spei_36m DOUBLE PRECISION,
    spei_48m DOUBLE PRECISION,

    -- SPI quality companion variable: probability of zero precipitation
    spi_p0_1m DOUBLE PRECISION,
    spi_p0_3m DOUBLE PRECISION,
    spi_p0_6m DOUBLE PRECISION,
    spi_p0_12m DOUBLE PRECISION,
    spi_p0_24m DOUBLE PRECISION,
    spi_p0_36m DOUBLE PRECISION,
    spi_p0_48m DOUBLE PRECISION,

    -- Normality-test acceptance flags (1 = significant / accepted)
    spi_normality_1m BOOLEAN,
    spi_normality_3m BOOLEAN,
    spi_normality_6m BOOLEAN,
    spi_normality_12m BOOLEAN,
    spi_normality_24m BOOLEAN,
    spi_normality_36m BOOLEAN,
    spi_normality_48m BOOLEAN,
    spei_normality_1m BOOLEAN,
    spei_normality_3m BOOLEAN,
    spei_normality_6m BOOLEAN,
    spei_normality_12m BOOLEAN,
    spei_normality_24m BOOLEAN,
    spei_normality_36m BOOLEAN,
    spei_normality_48m BOOLEAN,

    PRIMARY KEY (
        grid_cell_id,
        observation_month,
        dataset_type,
        product_type,
        ensemble_member,
        data_version
    )
);

SELECT create_hypertable(
    'cds_drought_tbl',
    'observation_month',
    chunk_time_interval => INTERVAL '1 year',
    if_not_exists => true
);

-- =========================================================================
-- Indexes
-- Time-range scans depend on TimescaleDB chunk pruning.
-- PK covers grid_cell_id lookups.
-- =========================================================================

-- Cross-grid product filter
CREATE INDEX IF NOT EXISTS idx_cdt_dataset_product
    ON cds_drought_tbl (dataset_type, product_type, ensemble_member);

-- Dashboard freshness and monthly coverage scans
CREATE INDEX IF NOT EXISTS idx_cdt_observation_month_desc
    ON cds_drought_tbl (observation_month DESC);

-- Grid-alignment trigger ``trg_cdsd_require_cell`` is in
-- ``triggers/cds_drought_require_cell.sql`` with its trigger function.
-- Consumer VIEW ``cds_drought_v`` is in ``views/011_cds_drought.sql``.
-- Spatial helper ``fn_point_to_cell`` is in ``functions/020_fn_point_to_cell.sql``.

COMMENT ON TABLE cds_drought_tbl IS
    'Monthly Copernicus CDS ERA5-derived drought indicators keyed by grid cell, '
    'month, product type, ensemble member, and schema version.';

COMMIT;
