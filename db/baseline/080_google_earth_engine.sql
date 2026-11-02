-- =============================================================================
-- Domain baseline DDL for Google Earth Engine grid features.
-- Applied lexicographically by bootstrap/init_database.sh.
-- All DDL must be idempotent (CREATE IF NOT EXISTS, etc.).
-- =============================================================================

BEGIN;

-- =========================================================================
-- TABLE: google_earth_engine_tbl
-- =========================================================================
-- Purpose: Canonical base-grid GEE feature facts.
-- Grain  : One row per grid cell per observation date.
-- PK     : (grid_cell_id, observation_date)
-- Source : Google Earth Engine
--
-- Notes:
--   * Geometry is stored in grid_cells_tbl (FK target).
--   * Use google_earth_engine_grid_tbl VIEW to join geometry.
--   * Value-range validation deferred to the data_quality_tbl pipeline.
--   * In dev, destructive resets should be run explicitly, not on bootstrap replay.
-- =========================================================================

CREATE TABLE IF NOT EXISTS google_earth_engine_tbl (

    -- Identity
    grid_cell_id TEXT NOT NULL,
    observation_date DATE NOT NULL,
    source TEXT,

    -- Key numeric features (flat for fast querying / joins)
    elevation_m DOUBLE PRECISION,
    slope_degrees DOUBLE PRECISION,
    aspect_degrees DOUBLE PRECISION,
    ndvi DOUBLE PRECISION,
    evi DOUBLE PRECISION,
    lai DOUBLE PRECISION,
    fpar DOUBLE PRECISION,
    lst_day_celsius DOUBLE PRECISION,
    lst_night_celsius DOUBLE PRECISION,
    temperature_2m_c DOUBLE PRECISION,
    dewpoint_2m_c DOUBLE PRECISION,
    total_precipitation_mm DOUBLE PRECISION,
    snowfall_m DOUBLE PRECISION,
    snow_depth_m DOUBLE PRECISION,
    soil_moisture DOUBLE PRECISION,
    wind_speed_10m_ms DOUBLE PRECISION,
    wind_u_10m_ms DOUBLE PRECISION,
    wind_v_10m_ms DOUBLE PRECISION,
    surface_pressure_pa DOUBLE PRECISION,
    soil_temp_level1_c DOUBLE PRECISION,
    soil_moisture_level2 DOUBLE PRECISION,
    surface_solar_rad_down_j_m2 DOUBLE PRECISION,
    surface_thermal_rad_down_j_m2 DOUBLE PRECISION,
    total_evaporation_m DOUBLE PRECISION,
    leaf_area_index_high DOUBLE PRECISION,
    leaf_area_index_low DOUBLE PRECISION,
    cloud_cover_pct DOUBLE PRECISION,
    cloud_top_height_m DOUBLE PRECISION,
    cloud_base_height_m DOUBLE PRECISION,
    cloud_optical_depth DOUBLE PRECISION,
    no2_mol_m2 DOUBLE PRECISION,
    co_mol_m2 DOUBLE PRECISION,
    so2_mol_m2 DOUBLE PRECISION,
    ozone_mol_m2 DOUBLE PRECISION,
    aerosol_index DOUBLE PRECISION,
    land_cover_class INTEGER,
    dynamic_world_label TEXT,
    esa_worldcover_class INTEGER,
    esa_worldcover_label TEXT,
    modis_igbp_class INTEGER,
    modis_igbp_label TEXT,
    forest_cover_pct DOUBLE PRECISION,
    forest_loss BOOLEAN,
    forest_gain BOOLEAN,
    forest_loss_year INTEGER,
    surface_water_pct DOUBLE PRECISION,
    surface_water_change_abs DOUBLE PRECISION,
    surface_water_seasonality_months DOUBLE PRECISION,
    surface_water_recurrence_pct DOUBLE PRECISION,
    nighttime_radiance_nw DOUBLE PRECISION,
    cloud_free_observations INTEGER,
    worldpop_count DOUBLE PRECISION,
    gpw_density_per_km2 DOUBLE PRECISION,
    human_modification_index DOUBLE PRECISION,
    burned_area_date INTEGER,
    fire_brightness_kelvin DOUBLE PRECISION,
    clay_pct DOUBLE PRECISION,
    sand_pct DOUBLE PRECISION,
    organic_carbon_g_kg DOUBLE PRECISION,
    soil_ph DOUBLE PRECISION,
    bulk_density_kg_m3 DOUBLE PRECISION,
    et_mm_8day DOUBLE PRECISION,
    pet_mm_8day DOUBLE PRECISION,
    sst_celsius DOUBLE PRECISION,
    sst_anom_celsius DOUBLE PRECISION,

    -- JSONB overflow + per-section availability metadata
    profile_data JSONB NOT NULL DEFAULT '{}',
    section_availability JSONB NOT NULL DEFAULT '{}',

    -- Sentinel-2 SR indices
    s2_ndvi DOUBLE PRECISION,
    s2_ndwi DOUBLE PRECISION,
    s2_mndwi DOUBLE PRECISION,
    s2_ndmi DOUBLE PRECISION,
    s2_evi DOUBLE PRECISION,
    s2_nbr DOUBLE PRECISION,
    s2_image_count INTEGER,

    -- Sentinel-1 SAR GRD
    s1_vv_db DOUBLE PRECISION,
    s1_vh_db DOUBLE PRECISION,
    s1_vv_vh_ratio_db DOUBLE PRECISION,
    s1_image_count INTEGER,

    -- Dynamic World probability bands
    dw_water_prob DOUBLE PRECISION,
    dw_trees_prob DOUBLE PRECISION,
    dw_grass_prob DOUBLE PRECISION,
    dw_flooded_vegetation_prob DOUBLE PRECISION,
    dw_crops_prob DOUBLE PRECISION,
    dw_shrub_and_scrub_prob DOUBLE PRECISION,
    dw_built_prob DOUBLE PRECISION,
    dw_bare_prob DOUBLE PRECISION,
    dw_snow_and_ice_prob DOUBLE PRECISION,

    -- Copernicus Global Land Cover 100 m cover fractions
    cop_lc_tree_pct DOUBLE PRECISION,
    cop_lc_shrub_pct DOUBLE PRECISION,
    cop_lc_grass_pct DOUBLE PRECISION,
    cop_lc_crops_pct DOUBLE PRECISION,
    cop_lc_urban_pct DOUBLE PRECISION,
    cop_lc_bare_pct DOUBLE PRECISION,
    cop_lc_water_permanent_pct DOUBLE PRECISION,
    cop_lc_water_seasonal_pct DOUBLE PRECISION,
    cop_lc_moss_lichen_pct DOUBLE PRECISION,
    cop_lc_snow_ice_pct DOUBLE PRECISION,

    -- CHIRPS + MODIS snow / GPP
    chirps_precip_mm DOUBLE PRECISION,
    modis_snow_cover_pct DOUBLE PRECISION,
    modis_ndsi DOUBLE PRECISION,
    modis_gpp_kg_c_m2 DOUBLE PRECISION,
    modis_psn_net_kg_c_m2 DOUBLE PRECISION,

    -- MODIS MCD12Q2 land-surface phenology (annual, cycle 1)
    phenology_greenup_doy INTEGER,
    phenology_midgreenup_doy INTEGER,
    phenology_peak_doy INTEGER,
    phenology_maturity_doy INTEGER,
    phenology_senescence_doy INTEGER,
    phenology_dormancy_doy INTEGER,
    phenology_evi_amplitude DOUBLE PRECISION,
    phenology_evi_area DOUBLE PRECISION,
    phenology_qa INTEGER,

    -- NASA Black Marble VNP46A2 (BRDF-corrected daily nightlights)
    black_marble_ntl_nw DOUBLE PRECISION,
    black_marble_qa INTEGER,

    -- S5P additional species
    ch4_ppb DOUBLE PRECISION,
    hcho_mol_m2 DOUBLE PRECISION,

    -- JRC categorical + FIRMS extras
    surface_water_max_extent INTEGER,
    surface_water_transition INTEGER,
    fire_frp_mw DOUBLE PRECISION,
    fire_confidence INTEGER,

    -- ERA5-Land additional bands
    skin_temperature_c DOUBLE PRECISION,
    soil_temp_level2_c DOUBLE PRECISION,
    soil_temp_level3_c DOUBLE PRECISION,
    soil_temp_level4_c DOUBLE PRECISION,
    soil_moisture_level3 DOUBLE PRECISION,
    soil_moisture_level4 DOUBLE PRECISION,
    potential_evaporation_m DOUBLE PRECISION,
    evap_bare_soil_m DOUBLE PRECISION,
    evap_open_water_m DOUBLE PRECISION,
    evap_canopy_m DOUBLE PRECISION,
    evap_vegetation_m DOUBLE PRECISION,
    runoff_m DOUBLE PRECISION,
    surface_runoff_m DOUBLE PRECISION,
    sub_surface_runoff_m DOUBLE PRECISION,
    surface_latent_heat_j_m2 DOUBLE PRECISION,
    surface_sensible_heat_j_m2 DOUBLE PRECISION,
    surface_net_solar_rad_j_m2 DOUBLE PRECISION,
    surface_net_thermal_rad_j_m2 DOUBLE PRECISION,
    era5_snow_cover DOUBLE PRECISION,
    era5_snow_density DOUBLE PRECISION,
    era5_snowmelt_m DOUBLE PRECISION,
    era5_snow_albedo DOUBLE PRECISION,

    -- ET latent heat + QA flags
    latent_heat_le_j_m2_day DOUBLE PRECISION,
    potential_le_j_m2_day DOUBLE PRECISION,
    et_qc INTEGER,
    lst_qc_day INTEGER,
    lst_qc_night INTEGER,
    ndvi_summary_qa INTEGER,
    population_source_year INTEGER,

    -- Pipeline Metadata
    profiler_mode TEXT,
    profiler_elapsed_s REAL,

    PRIMARY KEY (grid_cell_id, observation_date),

    CONSTRAINT fk_geeg_grid_cell
        FOREIGN KEY (grid_cell_id)
        REFERENCES grid_cells_tbl (grid_cell_id)
);

-- =========================================================================
-- Indexes
-- Custom indexes are limited to high-value query paths.
-- =========================================================================

-- Time-axis scans
CREATE INDEX IF NOT EXISTS idx_geeg_observation_date
    ON google_earth_engine_tbl (observation_date);

-- Per-cell drill-downs
CREATE INDEX IF NOT EXISTS idx_geeg_grid_cell_id
    ON google_earth_engine_tbl (grid_cell_id);

-- Frontend overlays and latest-label lookups
CREATE INDEX IF NOT EXISTS idx_geeg_grid_cell_observation_desc
    ON google_earth_engine_tbl (grid_cell_id, observation_date DESC);

-- JSONB containment lookups
CREATE INDEX IF NOT EXISTS idx_geeg_profile_data_gin
    ON google_earth_engine_tbl USING gin (profile_data);
CREATE INDEX IF NOT EXISTS idx_geeg_section_availability_gin
    ON google_earth_engine_tbl USING gin (section_availability);

COMMENT ON TABLE google_earth_engine_tbl IS
    'Canonical base-grid GEE feature facts keyed by grid_cell_id and observation_date.';

COMMIT;
