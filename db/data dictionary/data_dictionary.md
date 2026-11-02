# Data Dictionary

<!-- AUTO-GENERATED — do not edit directly. Run `make dict` in `db/` to regenerate (last generated: 2026-05-07). -->
<!-- Source: db/baseline/*.sql · db/caggs/*.sql · db/views/*.sql · db/dict/_external_datasets.md -->

## Contents

- [BirdWeather](#birdweather)
- [KNMI Weather](#knmi-weather)
- [Sovon Mortality](#sovon-mortality)
- [eBird](#ebird)
- [Movebank](#movebank)
- [Canonical Grid Cells](#canonical-grid-cells)
- [Google Earth Engine](#google-earth-engine)
- [CDS Drought Grid](#cds-drought-grid)
- [KNMI Seismic](#knmi-seismic)
- [BirdWeather Stations](#birdweather-stations)
- [Data Quality Audit](#data-quality-audit)
- [Species Reference](#species-reference)
- [Movebank Tracks](#movebank-tracks)
- [Species Tracker](#species-tracker)
- [BirdWeather Daily (Continuous Aggregate)](#birdweather-daily-continuous-aggregate)
- [KNMI Daily (Continuous Aggregate)](#knmi-daily-continuous-aggregate)
- [GEE Grid (View)](#gee-grid-view)
- [CDS Drought (View)](#cds-drought-view)
- [AudioDB (MinIO Object Storage)](#audiodb-minio-object-storage)

## BirdWeather

- **Source object:** `birdweather_tbl` (TimescaleDB hypertable · 1 day chunks; `baseline/020_birdweather.sql`)
- **Grain:** One row per classified detection event.
- **Primary key:** (`detection_id`, `detected_at`)
- **Overview:** BirdWeather acoustic detections, one row per classified event, partitioned by detected_at.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `detection_id` | `TEXT` | No | `—` | PK |  |
| `observation_date` | `DATE` | No | `—` | — |  |
| `detected_at` | `TIMESTAMPTZ` | No | `—` | PK; partition key |  |
| `soundscape_at` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `species_id` | `TEXT` | No | `—` | — |  |
| `station_id` | `TEXT` | No | `—` | — |  |
| `confidence` | `DOUBLE PRECISION` | No | `—` | — |  |
| `probability` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `score` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `certainty` | `TEXT` | Yes | `—` | — |  |
| `eclipse` | `BOOLEAN` | Yes | `—` | — | Night-time detection flag |
| `soundscape_id` | `TEXT` | Yes | `—` | — |  |
| `audio_filename` | `TEXT` | Yes | `—` | — |  |
| `audio_start_time_s` | `REAL` | Yes | `—` | — |  |
| `audio_end_time_s` | `REAL` | Yes | `—` | — |  |
| `audio_filesize_bytes` | `BIGINT` | Yes | `—` | — |  |
| `audio_url` | `TEXT` | Yes | `—` | — |  |
| `station_weather` | `JSONB` | Yes | `—` | — |  |
| `station_air_pollution` | `JSONB` | Yes | `—` | — |  |
| `station_sensors` | `JSONB` | Yes | `—` | — |  |
| `background_sound_clf` | `JSONB` | Yes | `—` | — | JSONB envelope with the AST background-sound classification status and tags. |
| `birdnet_embedding` | `VECTOR` | Yes | `—` | — | BirdNET embedding for the processed detection clip, stored as pgvector. |
| `perch_v2_embedding` | `VECTOR` | Yes | `—` | — | Perch v2 embedding for the processed detection clip, stored as pgvector. |
| `feature_artifacts` | `JSONB` | Yes | `—` | — | JSONB manifest of processed audio, spectrogram, waveform, and embedding feature artifacts in MinIO. |
| `has_processed_clip` | `BOOLEAN` | No | `false` | — | Indicates if a cropped processed clip is available in MinIO. |

## KNMI Weather

- **Source object:** `knmi_tbl` (TimescaleDB hypertable · 1 day chunks; `baseline/030_knmi.sql`)
- **Grain:** One row per KNMI station per 10-minute observation.
- **Primary key:** (`observed_at`, `station_id`)
- **Overview:** KNMI 10-minute weather observations keyed by station and timestamp.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `observed_at` | `TIMESTAMPTZ` | No | `—` | PK; partition key |  |
| `observation_date` | `DATE` | No | `—` | — |  |
| `station_id` | `TEXT` | No | `—` | PK |  |
| `station_name` | `TEXT` | Yes | `—` | — |  |
| `station_lat` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `station_lon` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `station_elevation_m` | `REAL` | Yes | `—` | — |  |
| `temperature_c` | `REAL` | Yes | `—` | — | ta (°C) |
| `relative_humidity_pct` | `REAL` | Yes | `—` | — | rh (%) |
| `wind_speed_ms` | `REAL` | Yes | `—` | — | ff (m/s) |
| `wind_direction_deg` | `REAL` | Yes | `—` | — | dd (°) |
| `pressure_hpa` | `REAL` | Yes | `—` | — | pp (hPa) |
| `precipitation_mm` | `REAL` | Yes | `—` | — | R1H (mm) |
| `solar_radiation_wm2` | `REAL` | Yes | `—` | — | qg (W/m²) |
| `cloud_cover_okta` | `REAL` | Yes | `—` | — | n (okta; 9 = obscured) |

## Sovon Mortality

- **Source object:** `sovon_tbl` (database table; `baseline/040_sovon.sql`)
- **Grain:** One row per species x year x month.
- **Primary key:** `mortality_id`
- **Overview:** Monthly Sovon dead-bird mortality counts by species, used as an epidemiological baseline.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `mortality_id` | `BIGINT` | Yes | `—` | PK; identity column |  |
| `observation_date` | `DATE` | Yes | `—` | — |  |
| `year` | `INTEGER` | No | `—` | — | noqa: RF04 |
| `month` | `INTEGER` | No | `—` | — | noqa: RF04 |
| `species_english` | `TEXT` | No | `—` | — |  |
| `species_dutch` | `TEXT` | Yes | `—` | — |  |
| `sovon_species_id` | `INTEGER` | Yes | `—` | — |  |
| `dead_bird_count` | `INTEGER` | No | `0` | — |  |
| `n_total` | `INTEGER` | Yes | `—` | — |  |
| `source_image` | `TEXT` | Yes | `—` | — |  |

## eBird

- **Source object:** `ebird_tbl` (TimescaleDB hypertable · 1 day chunks; `baseline/050_ebird.sql`)
- **Grain:** One row per species per checklist submission per observation time.
- **Primary key:** (`sub_id`, `obs_dt`, `species_code`)
- **Overview:** eBird checklist observations at species-by-checklist granularity, with hotspot metadata denormalized inline.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `sub_id` | `TEXT` | No | `—` | PK |  |
| `observation_date` | `DATE` | No | `—` | — |  |
| `obs_dt` | `TIMESTAMPTZ` | No | `—` | PK; partition key |  |
| `species_code` | `TEXT` | No | `—` | PK |  |
| `common_name` | `TEXT` | No | `—` | — |  |
| `scientific_name` | `TEXT` | Yes | `—` | — |  |
| `how_many` | `INTEGER` | Yes | `—` | — |  |
| `presence_noted` | `BOOLEAN` | No | `false` | — |  |
| `loc_id` | `TEXT` | No | `—` | — |  |
| `loc_name` | `TEXT` | Yes | `—` | — |  |
| `lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `lng` | `DOUBLE PRECISION` | No | `—` | — |  |
| `location_private` | `BOOLEAN` | No | `false` | — |  |
| `obs_valid` | `BOOLEAN` | No | `true` | — |  |
| `obs_reviewed` | `BOOLEAN` | No | `false` | — |  |
| `subnational2_code` | `TEXT` | Yes | `—` | — |  |
| `subnational2_name` | `TEXT` | Yes | `—` | — |  |
| `subnational1_code` | `TEXT` | Yes | `—` | — |  |
| `subnational1_name` | `TEXT` | Yes | `—` | — |  |
| `country_code` | `TEXT` | Yes | `—` | — |  |
| `country_name` | `TEXT` | Yes | `—` | — |  |
| `user_display_name` | `TEXT` | Yes | `—` | — |  |
| `checklist_id` | `TEXT` | Yes | `—` | — |  |
| `has_comments` | `BOOLEAN` | Yes | `—` | — |  |
| `has_rich_media` | `BOOLEAN` | Yes | `—` | — |  |
| `num_species_all_time` | `INTEGER` | Yes | `—` | — |  |
| `is_hotspot` | `BOOLEAN` | Yes | `—` | — |  |

## Movebank

- **Source object:** `movebank_tbl` (TimescaleDB hypertable · 7 days chunks; `baseline/060_movebank.sql`)
- **Grain:** One row per GPS fix (event) per individual per study.
- **Primary key:** (`event_id`, `study_id`, `timestamp`)
- **Overview:** Movebank GPS telemetry with denormalized individual, tag, and deployment attributes for analytical joins.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `event_id` | `BIGINT` | No | `—` | PK |  |
| `study_id` | `BIGINT` | No | `—` | PK |  |
| `timestamp` | `TIMESTAMPTZ` | No | `—` | PK; partition key | noqa: RF04 |
| `individual_id` | `BIGINT` | Yes | `—` | — |  |
| `individual_local_identifier` | `TEXT` | Yes | `—` | — |  |
| `individual_taxon_canonical_name` | `TEXT` | Yes | `—` | — |  |
| `tag_id` | `BIGINT` | Yes | `—` | — |  |
| `tag_local_identifier` | `TEXT` | Yes | `—` | — |  |
| `deployment_id` | `BIGINT` | Yes | `—` | — |  |
| `sensor_type_id` | `BIGINT` | Yes | `—` | — |  |
| `location_lat` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `location_long` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `ground_speed` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `heading` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `height_above_ellipsoid` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `visible` | `BOOLEAN` | Yes | `—` | — |  |
| `individual_sex` | `TEXT` | Yes | `—` | — |  |
| `individual_life_stage` | `TEXT` | Yes | `—` | — |  |
| `tag_manufacturer_name` | `TEXT` | Yes | `—` | — |  |
| `tag_model` | `TEXT` | Yes | `—` | — |  |
| `tag_serial_number` | `TEXT` | Yes | `—` | — |  |
| `deployment_local_identifier` | `TEXT` | Yes | `—` | — |  |
| `deploy_on_timestamp` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `deploy_off_timestamp` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `deploy_on_latitude` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `deploy_on_longitude` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `deployment_comments` | `TEXT` | Yes | `—` | — |  |
| `location_accuracy_comments` | `TEXT` | Yes | `—` | — |  |
| `manipulation_type` | `TEXT` | Yes | `—` | — |  |
| `study_site` | `TEXT` | Yes | `—` | — |  |
| `tag_readout_method` | `TEXT` | Yes | `—` | — |  |
| `alt_project_id` | `TEXT` | Yes | `—` | — |  |
| `attachment_type` | `TEXT` | Yes | `—` | — |  |
| `animal_mass_g` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `update_ts` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `import_marked_outlier` | `BOOLEAN` | Yes | `—` | — |  |
| `event_comments` | `TEXT` | Yes | `—` | — |  |
| `individual_comments` | `TEXT` | Yes | `—` | — |  |
| `individual_ring_id` | `TEXT` | Yes | `—` | — |  |
| `individual_number_of_events` | `BIGINT` | Yes | `—` | — |  |
| `individual_number_of_deployments` | `INTEGER` | Yes | `—` | — |  |
| `individual_taxon_detail` | `TEXT` | Yes | `—` | — |  |
| `tag_comments` | `TEXT` | Yes | `—` | — |  |
| `height_above_msl_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `barometric_pressure_pa` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `external_temperature_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |

## Canonical Grid Cells

- **Source object:** `grid_cells_tbl` (database table; `baseline/070_grid_cells.sql`)
- **Grain:** One row per canonical grid cell.
- **Primary key:** `grid_cell_id`
- **Overview:** Canonical grid-cell registry: centroid, bounding box, and GeoJSON geometry.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `grid_cell_id` | `TEXT` | Yes | `—` | PK |  |
| `lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_min_lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_min_lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_max_lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_max_lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `grid_size_km` | `DOUBLE PRECISION` | No | `—` | — |  |
| `cell_geojson` | `JSONB` | No | `—` | — |  |
| `grid_system` | `TEXT` | No | `—` | — |  |
| `source` | `TEXT` | Yes | `—` | — |  |
| `grid_row` | `INTEGER` | Yes | `—` | — | Row index for row/col-style grids (e.g., nl_geodesic_5km). NULL otherwise. |
| `grid_col` | `INTEGER` | Yes | `—` | — | Column index for row/col-style grids. NULL otherwise. |

## Google Earth Engine

- **Source object:** `google_earth_engine_tbl` (database table; `baseline/080_google_earth_engine.sql`)
- **Grain:** One row per grid cell per observation date.
- **Primary key:** (`grid_cell_id`, `observation_date`)
- **Foreign keys:**
  - `grid_cell_id` → `grid_cells_tbl(grid_cell_id)`
- **Overview:** Canonical base-grid GEE feature facts keyed by grid_cell_id and observation_date.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `grid_cell_id` | `TEXT` | No | `—` | PK; FK → `grid_cells_tbl(grid_cell_id)` |  |
| `observation_date` | `DATE` | No | `—` | PK |  |
| `source` | `TEXT` | Yes | `—` | — |  |
| `elevation_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `slope_degrees` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `aspect_degrees` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `ndvi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `lai` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `fpar` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `lst_day_celsius` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `lst_night_celsius` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `temperature_2m_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dewpoint_2m_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `total_precipitation_mm` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `snowfall_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `snow_depth_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_moisture` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `wind_speed_10m_ms` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `wind_u_10m_ms` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `wind_v_10m_ms` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_pressure_pa` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_temp_level1_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_moisture_level2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_solar_rad_down_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_thermal_rad_down_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `total_evaporation_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `leaf_area_index_high` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `leaf_area_index_low` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_cover_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_top_height_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_base_height_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_optical_depth` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `no2_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `co_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `so2_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `ozone_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `aerosol_index` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `land_cover_class` | `INTEGER` | Yes | `—` | — |  |
| `dynamic_world_label` | `TEXT` | Yes | `—` | — |  |
| `esa_worldcover_class` | `INTEGER` | Yes | `—` | — |  |
| `esa_worldcover_label` | `TEXT` | Yes | `—` | — |  |
| `modis_igbp_class` | `INTEGER` | Yes | `—` | — |  |
| `modis_igbp_label` | `TEXT` | Yes | `—` | — |  |
| `forest_cover_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `forest_loss` | `BOOLEAN` | Yes | `—` | — |  |
| `forest_gain` | `BOOLEAN` | Yes | `—` | — |  |
| `forest_loss_year` | `INTEGER` | Yes | `—` | — |  |
| `surface_water_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_water_change_abs` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_water_seasonality_months` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_water_recurrence_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `nighttime_radiance_nw` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_free_observations` | `INTEGER` | Yes | `—` | — |  |
| `worldpop_count` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `gpw_density_per_km2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `human_modification_index` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `burned_area_date` | `INTEGER` | Yes | `—` | — |  |
| `fire_brightness_kelvin` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `clay_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `sand_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `organic_carbon_g_kg` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_ph` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `bulk_density_kg_m3` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `et_mm_8day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `pet_mm_8day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `sst_celsius` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `sst_anom_celsius` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `profile_data` | `JSONB` | No | `'{}'` | — |  |
| `section_availability` | `JSONB` | No | `'{}'` | — |  |
| `s2_ndvi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_ndwi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_mndwi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_ndmi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_evi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_nbr` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_image_count` | `INTEGER` | Yes | `—` | — |  |
| `s1_vv_db` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s1_vh_db` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s1_vv_vh_ratio_db` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s1_image_count` | `INTEGER` | Yes | `—` | — |  |
| `dw_water_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_trees_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_grass_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_flooded_vegetation_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_crops_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_shrub_and_scrub_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_built_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_bare_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_snow_and_ice_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_tree_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_shrub_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_grass_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_crops_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_urban_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_bare_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_water_permanent_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_water_seasonal_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_moss_lichen_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_snow_ice_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `chirps_precip_mm` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `modis_snow_cover_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `modis_ndsi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `modis_gpp_kg_c_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `modis_psn_net_kg_c_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `phenology_greenup_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_midgreenup_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_peak_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_maturity_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_senescence_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_dormancy_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_evi_amplitude` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `phenology_evi_area` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `phenology_qa` | `INTEGER` | Yes | `—` | — |  |
| `black_marble_ntl_nw` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `black_marble_qa` | `INTEGER` | Yes | `—` | — |  |
| `ch4_ppb` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `hcho_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_water_max_extent` | `INTEGER` | Yes | `—` | — |  |
| `surface_water_transition` | `INTEGER` | Yes | `—` | — |  |
| `fire_frp_mw` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `fire_confidence` | `INTEGER` | Yes | `—` | — |  |
| `skin_temperature_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_temp_level2_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_temp_level3_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_temp_level4_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_moisture_level3` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_moisture_level4` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `potential_evaporation_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evap_bare_soil_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evap_open_water_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evap_canopy_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evap_vegetation_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `runoff_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_runoff_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `sub_surface_runoff_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_latent_heat_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_sensible_heat_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_net_solar_rad_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_net_thermal_rad_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `era5_snow_cover` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `era5_snow_density` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `era5_snowmelt_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `era5_snow_albedo` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `latent_heat_le_j_m2_day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `potential_le_j_m2_day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `et_qc` | `INTEGER` | Yes | `—` | — |  |
| `lst_qc_day` | `INTEGER` | Yes | `—` | — |  |
| `lst_qc_night` | `INTEGER` | Yes | `—` | — |  |
| `ndvi_summary_qa` | `INTEGER` | Yes | `—` | — |  |
| `population_source_year` | `INTEGER` | Yes | `—` | — |  |
| `profiler_mode` | `TEXT` | Yes | `—` | — |  |
| `profiler_elapsed_s` | `REAL` | Yes | `—` | — |  |

## CDS Drought Grid

- **Source object:** `cds_drought_tbl` (TimescaleDB hypertable · 1 year chunks; `baseline/090_cds_drought.sql`)
- **Grain:** One row per cell x month x dataset_type x product_type x ensemble x version.
- **Primary key:** (`grid_cell_id`, `observation_month`, `dataset_type`, `product_type`, `ensemble_member`, `data_version`)
- **Overview:** Monthly Copernicus CDS ERA5-derived drought indicators keyed by grid cell, month, product type, ensemble member, and schema version.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `grid_cell_id` | `TEXT` | No | `—` | PK |  |
| `observation_month` | `DATE` | No | `—` | PK; partition key |  |
| `dataset_type` | `TEXT` | No | `—` | PK |  |
| `product_type` | `TEXT` | No | `—` | PK |  |
| `ensemble_member` | `SMALLINT` | No | `0` | PK |  |
| `data_version` | `TEXT` | No | `'1_0'` | PK |  |
| `grid_system` | `TEXT` | No | `'cds_latlon_0p25deg'` | — |  |
| `source` | `TEXT` | No | `'copernicus_cds_era5_drought'` | — |  |
| `lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_min_lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_min_lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_max_lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_max_lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `resolution_degrees` | `DOUBLE PRECISION` | No | `0.25` | — |  |
| `spi_1m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_3m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_6m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_12m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_24m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_36m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_48m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_1m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_3m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_6m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_12m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_24m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_36m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_48m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_1m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_3m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_6m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_12m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_24m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_36m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_48m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_normality_1m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_3m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_6m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_12m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_24m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_36m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_48m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_1m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_3m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_6m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_12m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_24m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_36m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_48m` | `BOOLEAN` | Yes | `—` | — |  |

## KNMI Seismic

- **Source object:** `seismic_tbl` (database table; `baseline/100_seismic.sql`)
- **Grain:** One row per earthquake event.
- **Primary key:** `event_id`
- **Overview:** KNMI FDSN earthquake catalogue events with coordinates and raw attributes.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `event_id` | `TEXT` | Yes | `—` | PK |  |
| `event_time` | `TIMESTAMPTZ` | No | `—` | — |  |
| `latitude` | `DOUBLE PRECISION` | No | `—` | — |  |
| `longitude` | `DOUBLE PRECISION` | No | `—` | — |  |
| `region` | `TEXT` | Yes | `—` | — |  |
| `depth_km` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `magnitude` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `magnitude_type` | `TEXT` | Yes | `—` | — |  |
| `description` | `TEXT` | Yes | `—` | — |  |
| `attributes` | `JSONB` | No | `'{}'` | — |  |

## BirdWeather Stations

- **Source object:** `birdweather_stations_tbl` (database table; `baseline/110_birdweather_stations.sql`)
- **Grain:** One row per BirdWeather station.
- **Primary key:** `station_id`
- **Overview:** Unified BirdWeather station table. Metadata seeded from public API; detection summaries, freshness, lifetime availability, and recent 90-day availability updated by refresh_station_summary().

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `station_id` | `TEXT` | Yes | `—` | PK |  |
| `station_name` | `TEXT` | Yes | `—` | — |  |
| `station_type` | `TEXT` | Yes | `—` | — |  |
| `lat` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `lon` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `station_continent` | `TEXT` | Yes | `—` | — |  |
| `station_country` | `TEXT` | Yes | `—` | — |  |
| `station_state` | `TEXT` | Yes | `—` | — |  |
| `station_location` | `TEXT` | Yes | `—` | — |  |
| `station_timezone` | `TEXT` | Yes | `—` | — |  |
| `total_detections` | `INT` | No | `0` | — |  |
| `top_species` | `JSONB` | No | `'[]'` | — |  |
| `unique_species` | `INT` | No | `0` | — |  |
| `avg_confidence` | `NUMERIC(5, 4)` | Yes | `—` | — |  |
| `high_confidence_detections` | `INT` | No | `0` | — |  |
| `first_detection` | `DATE` | Yes | `—` | — |  |
| `last_detection` | `DATE` | Yes | `—` | — |  |
| `days_since_last` | `INT` | Yes | `—` | — |  |
| `unique_days` | `INT` | No | `0` | — |  |
| `monthly_detections` | `JSONB` | No | `'{}'` | — |  |
| `lifetime_availability_pct` | `DOUBLE PRECISION` | Yes | `—` | — | 100 * unique_days / inclusive span from first to last detection. |
| `availability_90d_pct` | `DOUBLE PRECISION` | Yes | `—` | — | 100 * active days in trailing 90-day window / 90, capped at 100. |

## Data Quality Audit

- **Source object:** `data_quality_tbl` (database table; `baseline/120_data_quality.sql`)
- **Primary key:** `id`
- **Overview:** Run-level and metric-level quality audit records. Station quality lives in birdweather_stations_tbl.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `id` | `BIGINT` | Yes | `—` | PK; identity column |  |
| `run_id` | `BIGINT` | No | `nextval('data_quality_run_id_seq')` | — |  |
| `kind` | `TEXT` | No | `—` | — |  |
| `status` | `TEXT` | No | `'running'` | — |  |
| `table_name` | `TEXT` | Yes | `—` | — | metric: source table |
| `dimension` | `TEXT` | Yes | `—` | — | metric: quality dimension |
| `metric` | `TEXT` | Yes | `—` | — | metric: metric name |
| `value` | `DOUBLE PRECISION` | Yes | `—` | — | metric: measured value  -- noqa: RF04 |
| `score` | `DOUBLE PRECISION` | Yes | `—` | — | metric / summary: score or composite score |
| `detail` | `JSONB` | No | `'{}'` | — |  |
| `created_at` | `TIMESTAMPTZ` | No | `now()` | — |  |

## Species Reference

- **Source object:** `species_tbl` (database table; `baseline/130_species.sql`)
- **Grain:** One row per species (BirdWeather species_id).
- **Primary key:** `species_id`
- **Overview:** Canonical species dimension: API metadata, activity envelope, and cached PhyloPic assets.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `species_id` | `TEXT` | Yes | `—` | PK |  |
| `common_name` | `TEXT` | No | `—` | — |  |
| `scientific_name` | `TEXT` | Yes | `—` | — |  |
| `alpha_code` | `TEXT` | Yes | `—` | — |  |
| `alpha6_code` | `TEXT` | Yes | `—` | — |  |
| `ebird_code` | `TEXT` | Yes | `—` | — |  |
| `species_color` | `TEXT` | Yes | `—` | — |  |
| `species_image_url` | `TEXT` | Yes | `—` | — |  |
| `species_thumbnail_url` | `TEXT` | Yes | `—` | — |  |
| `birdweather_url` | `TEXT` | Yes | `—` | — |  |
| `ebird_url` | `TEXT` | Yes | `—` | — |  |
| `ml_url` | `TEXT` | Yes | `—` | — |  |
| `wikipedia_url` | `TEXT` | Yes | `—` | — |  |
| `wikipedia_summary` | `TEXT` | Yes | `—` | — |  |
| `prediction_area` | `JSONB` | Yes | `—` | — |  |
| `range_map` | `JSONB` | Yes | `—` | — |  |
| `first_seen` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `last_seen` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `total_detections` | `BIGINT` | No | `0` | — |  |
| `station_count` | `INTEGER` | No | `0` | — |  |
| `phylopic_svg_url` | `TEXT` | Yes | `—` | — |  |
| `phylopic_thumb_url` | `TEXT` | Yes | `—` | — |  |
| `phylopic_attribution` | `TEXT` | No | `''` | — |  |
| `phylopic_uuid` | `TEXT` | No | `''` | — |  |
| `phylopic_fetched_at` | `TIMESTAMPTZ` | Yes | `—` | — |  |

## Movebank Tracks

- **Source object:** `movebank_tracks_tbl` (database table; `baseline/140_movebank_tracks.sql`)
- **Grain:** One row per (study_id, individual_key).
- **Primary key:** (`study_id`, `individual_key`)
- **Overview:** Pre-decimated per-individual GPS tracks with raw counts, bbox, and start/end summaries. Rebuilt by refresh_movebank_tracks().

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `study_id` | `BIGINT` | No | `—` | PK |  |
| `species` | `TEXT` | Yes | `—` | — |  |
| `individual_key` | `TEXT` | No | `—` | PK |  |
| `raw_point_count` | `INTEGER` | No | `—` | — |  |
| `point_count` | `INTEGER` | No | `—` | — |  |
| `bbox` | `JSONB` | No | `—` | — |  |
| `start_ts` | `TIMESTAMPTZ` | No | `—` | — |  |
| `start_point` | `JSONB` | No | `—` | — |  |
| `end_ts` | `TIMESTAMPTZ` | No | `—` | — |  |
| `end_point` | `JSONB` | No | `—` | — |  |
| `track_points` | `JSONB` | No | `—` | — |  |

## Species Tracker

- **Source object:** `species_tracker_tbl` (database table; `baseline/150_species_tracker.sql`)
- **Grain:** One row per (row_kind, period_grain, period_start, location,
- **Primary key:** `species_tracker_id`
- **Overview:** Unified tracker table at period x location x species/community grain with ecological metrics, classifier/audio metrics, environmental covariates, station-grain quality, and JSONB sidecars (species_info, audio_artifacts, metric_provenance, extras). Rebuilt by refresh_species_tracker().

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `species_tracker_id` | `BIGINT` | Yes | `—` | PK; identity column |  |
| `row_kind` | `TEXT` | No | `—` | — | species = one row per species; community = one row for the whole location-period community. |
| `period_grain` | `TEXT` | No | `—` | — |  |
| `period_start` | `DATE` | No | `—` | — |  |
| `period_end` | `DATE` | No | `—` | — |  |
| `period_day_count` | `INTEGER` | No | `—` | — |  |
| `location_type` | `TEXT` | No | `—` | — | station rows summarize one BirdWeather station; grid rows summarize one canonical grid cell. |
| `location_id` | `TEXT` | No | `—` | — |  |
| `station_id` | `TEXT` | Yes | `—` | — |  |
| `grid_cell_id` | `TEXT` | Yes | `—` | — |  |
| `grid_system` | `TEXT` | Yes | `—` | — |  |
| `species_id` | `TEXT` | Yes | `—` | — |  |
| `total_detections` | `BIGINT` | No | `0` | — |  |
| `active_days` | `INTEGER` | No | `0` | — |  |
| `active_station_count` | `INTEGER` | No | `0` | — |  |
| `species_station_count` | `INTEGER` | Yes | `—` | — |  |
| `detections_per_active_day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `temporal_occupancy_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `relative_abundance_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `station_presence_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `species_richness` | `INTEGER` | Yes | `—` | — |  |
| `shannon_h` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dominance_index` | `DOUBLE PRECISION` | Yes | `—` | — | Berger-Parker dominance index: percent of detections from the dominant species in the location-period. |
| `pielou_evenness` | `DOUBLE PRECISION` | Yes | `—` | — | Pielou's J = shannon_h / ln(species_richness); NULL when richness < 2. |
| `simpson_d` | `DOUBLE PRECISION` | Yes | `—` | — | Simpson's diversity (1 - sum(p_i^2)); complements Shannon for low-richness panels. |
| `target_species_richness` | `INTEGER` | Yes | `—` | — | Count of distinct target species (config/target_species.json) detected in the location-period. |
| `target_species_share_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `confidence_mean` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `confidence_min` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `confidence_max` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `confidence_p10` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `confidence_p50` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `confidence_p90` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `high_confidence_detections` | `BIGINT` | Yes | `—` | — |  |
| `high_confidence_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `processed_clip_count` | `BIGINT` | Yes | `—` | — |  |
| `processed_clip_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `embedding_count` | `BIGINT` | Yes | `—` | — |  |
| `first_detection_at` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `last_detection_at` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `peak_hour_utc` | `INTEGER` | Yes | `—` | — |  |
| `peak_hour_share_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `nocturnal_share_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dawn_share_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dusk_share_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `background_top_label` | `TEXT` | Yes | `—` | — |  |
| `background_top_share_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `background_label_mix` | `JSONB` | Yes | `—` | — | JSONB {label: share_pct, ...} from background_sound_clf top-tag-per-detection over the location-period. |
| `station_lifetime_availability_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `station_availability_90d_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `station_days_since_last` | `INTEGER` | Yes | `—` | — |  |
| `station_data_quality_grade` | `TEXT` | Yes | `—` | — | Coarse station grade derived from availability_90d_pct (>=90 A, >=60 B, else C). NULL for grid rows. |
| `knmi_temp_mean_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `knmi_temp_min_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `knmi_temp_max_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `knmi_precip_mm` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `knmi_humidity_mean_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `knmi_wind_max_ms` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `knmi_solar_mean_wm2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `gee_ndvi_mean` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `gee_temp2m_mean_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `gee_soil_moisture_mean` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `gee_precip_mean_mm` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `gee_cloud_cover_mean_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cds_spi_1m_mean` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cds_spi_3m_mean` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cds_spei_3m_mean` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cds_spei_12m_mean` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `seismic_event_count` | `INTEGER` | Yes | `—` | — |  |
| `seismic_max_magnitude` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `seismic_mean_magnitude` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `seismic_nearest_event_km` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `species_info` | `JSONB` | No | `'{}'` | — | Cached species_tbl projection: taxonomy, alpha codes, color, thumbnail, target-species flag, etc. |
| `audio_artifacts` | `JSONB` | No | `'{}'` | — | JSONB manifest of detection IDs with processed clips and embeddings (capped at 200 IDs each), plus top-confidence clip and counts. |
| `metric_provenance` | `JSONB` | No | `'{}'` | — | Refresh-time audit trail: refresh_version, source caggs, period windows, target species count. |
| `extras` | `JSONB` | No | `'{}'` | — | Open extension slot for future metrics; default empty object. |
| `computed_at` | `TIMESTAMPTZ` | No | `NOW()` | — |  |

## BirdWeather Daily (Continuous Aggregate)

- **Source object:** `birdweather_daily_cagg_tbl` (continuous aggregate; `caggs/010_birdweather_daily.sql`)
- **Sources:** materializes from `birdweather_tbl`

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `species_id` | `TEXT` | Yes | `—` | — |  |
| `station_id` | `TEXT` | Yes | `—` | — |  |
| `bucket` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `detection_count` | `BIGINT` | Yes | `—` | — |  |
| `confidence_sum` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `avg_confidence` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `min_confidence` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `max_confidence` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `high_confidence_count` | `BIGINT` | Yes | `—` | — |  |

## KNMI Daily (Continuous Aggregate)

- **Source object:** `knmi_daily_cagg_tbl` (continuous aggregate; `caggs/020_knmi_daily.sql`)
- **Sources:** materializes from `knmi_tbl`

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `station_id` | `TEXT` | Yes | `—` | — |  |
| `bucket` | `TIMESTAMPTZ` | Yes | `—` | — |  |
| `daily_mean_temp_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `daily_max_temp_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `daily_min_temp_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `daily_precip_mm` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `daily_mean_rh_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `daily_max_wind_ms` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `daily_mean_solar_wm2` | `DOUBLE PRECISION` | Yes | `—` | — |  |

## GEE Grid (View)

- **Source object:** `google_earth_engine_grid_tbl` (view; `views/010_google_earth_engine_grid.sql`)
- **Primary key:** (`grid_cell_id`, `observation_date`)
- **Foreign keys:**
  - `grid_cell_id` → `grid_cells_tbl(grid_cell_id)`
- **Sources:** reads from `google_earth_engine_tbl`, `grid_cells_tbl`
- **Overview:** Backward-compatible view of google_earth_engine_tbl joined with grid_cells_tbl for geometry. All writes go to the facts table.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `grid_cell_id` | `TEXT` | No | `—` | PK; FK → `grid_cells_tbl(grid_cell_id)` |  |
| `observation_date` | `DATE` | No | `—` | PK |  |
| `source` | `TEXT` | Yes | `—` | — |  |
| `elevation_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `slope_degrees` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `aspect_degrees` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `ndvi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `lai` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `fpar` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `lst_day_celsius` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `lst_night_celsius` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `temperature_2m_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dewpoint_2m_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `total_precipitation_mm` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `snowfall_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `snow_depth_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_moisture` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `wind_speed_10m_ms` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `wind_u_10m_ms` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `wind_v_10m_ms` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_pressure_pa` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_temp_level1_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_moisture_level2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_solar_rad_down_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_thermal_rad_down_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `total_evaporation_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `leaf_area_index_high` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `leaf_area_index_low` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_cover_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_top_height_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_base_height_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_optical_depth` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `no2_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `co_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `so2_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `ozone_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `aerosol_index` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `land_cover_class` | `INTEGER` | Yes | `—` | — |  |
| `dynamic_world_label` | `TEXT` | Yes | `—` | — |  |
| `esa_worldcover_class` | `INTEGER` | Yes | `—` | — |  |
| `esa_worldcover_label` | `TEXT` | Yes | `—` | — |  |
| `modis_igbp_class` | `INTEGER` | Yes | `—` | — |  |
| `modis_igbp_label` | `TEXT` | Yes | `—` | — |  |
| `forest_cover_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `forest_loss` | `BOOLEAN` | Yes | `—` | — |  |
| `forest_gain` | `BOOLEAN` | Yes | `—` | — |  |
| `forest_loss_year` | `INTEGER` | Yes | `—` | — |  |
| `surface_water_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_water_change_abs` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_water_seasonality_months` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_water_recurrence_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `nighttime_radiance_nw` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cloud_free_observations` | `INTEGER` | Yes | `—` | — |  |
| `worldpop_count` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `gpw_density_per_km2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `human_modification_index` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `burned_area_date` | `INTEGER` | Yes | `—` | — |  |
| `fire_brightness_kelvin` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `clay_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `sand_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `organic_carbon_g_kg` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_ph` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `bulk_density_kg_m3` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `et_mm_8day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `pet_mm_8day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `sst_celsius` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `sst_anom_celsius` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `profile_data` | `JSONB` | No | `'{}'` | — |  |
| `section_availability` | `JSONB` | No | `'{}'` | — |  |
| `s2_ndvi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_ndwi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_mndwi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_ndmi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_evi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_nbr` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s2_image_count` | `INTEGER` | Yes | `—` | — |  |
| `s1_vv_db` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s1_vh_db` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s1_vv_vh_ratio_db` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `s1_image_count` | `INTEGER` | Yes | `—` | — |  |
| `dw_water_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_trees_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_grass_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_flooded_vegetation_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_crops_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_shrub_and_scrub_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_built_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_bare_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `dw_snow_and_ice_prob` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_tree_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_shrub_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_grass_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_crops_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_urban_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_bare_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_water_permanent_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_water_seasonal_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_moss_lichen_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cop_lc_snow_ice_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `chirps_precip_mm` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `modis_snow_cover_pct` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `modis_ndsi` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `modis_gpp_kg_c_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `modis_psn_net_kg_c_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `phenology_greenup_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_midgreenup_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_peak_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_maturity_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_senescence_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_dormancy_doy` | `INTEGER` | Yes | `—` | — |  |
| `phenology_evi_amplitude` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `phenology_evi_area` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `phenology_qa` | `INTEGER` | Yes | `—` | — |  |
| `black_marble_ntl_nw` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `black_marble_qa` | `INTEGER` | Yes | `—` | — |  |
| `ch4_ppb` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `hcho_mol_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_water_max_extent` | `INTEGER` | Yes | `—` | — |  |
| `surface_water_transition` | `INTEGER` | Yes | `—` | — |  |
| `fire_frp_mw` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `fire_confidence` | `INTEGER` | Yes | `—` | — |  |
| `skin_temperature_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_temp_level2_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_temp_level3_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_temp_level4_c` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_moisture_level3` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `soil_moisture_level4` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `potential_evaporation_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evap_bare_soil_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evap_open_water_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evap_canopy_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `evap_vegetation_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `runoff_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_runoff_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `sub_surface_runoff_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_latent_heat_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_sensible_heat_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_net_solar_rad_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `surface_net_thermal_rad_j_m2` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `era5_snow_cover` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `era5_snow_density` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `era5_snowmelt_m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `era5_snow_albedo` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `latent_heat_le_j_m2_day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `potential_le_j_m2_day` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `et_qc` | `INTEGER` | Yes | `—` | — |  |
| `lst_qc_day` | `INTEGER` | Yes | `—` | — |  |
| `lst_qc_night` | `INTEGER` | Yes | `—` | — |  |
| `ndvi_summary_qa` | `INTEGER` | Yes | `—` | — |  |
| `population_source_year` | `INTEGER` | Yes | `—` | — |  |
| `profiler_mode` | `TEXT` | Yes | `—` | — |  |
| `profiler_elapsed_s` | `REAL` | Yes | `—` | — |  |
| `grid_system` | `TEXT` | Yes | `—` | — |  |
| `lat` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `lon` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `bbox_min_lat` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `bbox_min_lon` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `bbox_max_lat` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `bbox_max_lon` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `grid_size_km` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `cell_geojson` | `JSONB` | Yes | `—` | — |  |

## CDS Drought (View)

- **Source object:** `cds_drought_v` (view; `views/011_cds_drought.sql`)
- **Primary key:** (`grid_cell_id`, `observation_month`, `dataset_type`, `product_type`, `ensemble_member`, `data_version`)
- **Sources:** reads from `cds_drought_tbl`, `grid_cells_tbl`
- **Overview:** cds_drought_tbl enriched with grid-cell polygon from grid_cells_tbl.

| Column | Type | Nullable | Default | Constraints / notes | Description |
|---|---|---|---|---|---|
| `grid_cell_id` | `TEXT` | No | `—` | PK |  |
| `observation_month` | `DATE` | No | `—` | PK; partition key |  |
| `dataset_type` | `TEXT` | No | `—` | PK |  |
| `product_type` | `TEXT` | No | `—` | PK |  |
| `ensemble_member` | `SMALLINT` | No | `0` | PK |  |
| `data_version` | `TEXT` | No | `'1_0'` | PK |  |
| `grid_system` | `TEXT` | No | `'cds_latlon_0p25deg'` | — |  |
| `source` | `TEXT` | No | `'copernicus_cds_era5_drought'` | — |  |
| `lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_min_lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_min_lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_max_lat` | `DOUBLE PRECISION` | No | `—` | — |  |
| `bbox_max_lon` | `DOUBLE PRECISION` | No | `—` | — |  |
| `resolution_degrees` | `DOUBLE PRECISION` | No | `0.25` | — |  |
| `spi_1m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_3m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_6m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_12m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_24m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_36m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_48m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_1m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_3m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_6m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_12m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_24m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_36m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spei_48m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_1m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_3m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_6m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_12m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_24m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_36m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_p0_48m` | `DOUBLE PRECISION` | Yes | `—` | — |  |
| `spi_normality_1m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_3m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_6m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_12m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_24m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_36m` | `BOOLEAN` | Yes | `—` | — |  |
| `spi_normality_48m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_1m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_3m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_6m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_12m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_24m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_36m` | `BOOLEAN` | Yes | `—` | — |  |
| `spei_normality_48m` | `BOOLEAN` | Yes | `—` | — |  |
| `cell_geojson` | `JSONB` | Yes | `—` | — |  |

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
