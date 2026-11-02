import type { EarthEngineOverlaySample } from "@/lib/earth-engine-overlays";

export const DEFAULT_GRID_SIZE_KM = 5 as const;
export type SupportedGridSizeKm = typeof DEFAULT_GRID_SIZE_KM;

const DEFAULT_GRID_SYSTEM = "nl_geodesic_5km";

function nullableNumber(value: unknown, digits = 3): number | null {
  if (value == null) return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  const factor = 10 ** digits;
  return Math.round(numeric * factor) / factor;
}

function nullableInteger(value: unknown): number | null {
  if (value == null) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.round(numeric) : null;
}

function parseCellGeometry(value: unknown) {
  if (!value) return null;
  if (typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return null;
    }
  }
  return value;
}

export function resolveGridSizeKm(_raw: string | null): SupportedGridSizeKm {
  return DEFAULT_GRID_SIZE_KM;
}

export function gridSystemForSize(_gridSizeKm: SupportedGridSizeKm): string {
  return DEFAULT_GRID_SYSTEM;
}

export function buildOverlayQuery(
  gridSystem: string,
  options?: { gridCellId?: string },
): { sql: string; params: readonly unknown[] } {
  const params: unknown[] = [gridSystem];
  let gridCellFilter = "";

  if (options?.gridCellId) {
    params.push(options.gridCellId);
    gridCellFilter = "\n            AND gee.grid_cell_id = $2";
  }

  return {
    sql: `
        WITH latest_obs AS (
          -- Anchor every "recent" window to the newest observation in the
          -- dataset rather than the wall clock. The GEE table is historical,
          -- so a CURRENT_DATE-based window matches nothing and every recent_*
          -- / *_mean aggregate silently returns NULL. Evaluated once (InitPlan).
          SELECT MAX(observation_date) AS asof FROM google_earth_engine_tbl
        ),
        gee_base AS (
          SELECT
            gee.grid_cell_id AS parent_grid_cell_id,
            gee.observation_date,
            gee.ndvi,
            gee.surface_water_pct,
            gee.surface_water_recurrence_pct,
            gee.soil_moisture,
            gee.human_modification_index,
            gee.gpw_density_per_km2,
            gee.nighttime_radiance_nw,
            gee.lst_day_celsius,
            gee.temperature_2m_c,
            gee.no2_mol_m2,
            gee.aerosol_index,
            gee.forest_cover_pct,
            gee.forest_loss,
            gee.forest_loss_year,
            gee.cloud_cover_pct,
            gee.fire_brightness_kelvin,
            gee.burned_area_date,
            gee.sst_celsius,
            gee.sst_anom_celsius,
            gee.elevation_m,
            gee.slope_degrees,
            gee.aspect_degrees,
            gee.et_mm_8day,
            gee.pet_mm_8day,
            gee.dynamic_world_label,
            gee.esa_worldcover_label,
            gee.modis_igbp_label
          FROM google_earth_engine_tbl gee
          INNER JOIN grid_cells_tbl gc
            ON gc.grid_cell_id = gee.grid_cell_id
          WHERE gc.grid_system = $1${gridCellFilter}
        ),
        gee_agg AS (
          SELECT
            gee.parent_grid_cell_id AS station_id,
            CONCAT('Grid ', gee.parent_grid_cell_id) AS station_name,
            cell.lat,
            cell.lon,
            cell.bbox_min_lat,
            cell.bbox_min_lon,
            cell.bbox_max_lat,
            cell.bbox_max_lon,
            MAX(cell.grid_size_km)::numeric AS grid_size_km,
            cell.cell_geojson,
            COUNT(*)::int AS profile_rows,
            COUNT(*) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '90 days'
            )::int AS recent_profile_rows,
            MAX(gee.observation_date)::date AS latest_profile_date,
            ROUND(AVG(gee.ndvi) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '90 days'
            )::numeric, 3) AS ndvi_mean,
            ROUND(AVG(gee.surface_water_pct) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '365 days'
            )::numeric, 2) AS surface_water_pct_mean,
            ROUND(AVG(gee.surface_water_recurrence_pct)::numeric, 2) AS surface_water_recurrence_pct_mean,
            ROUND(AVG(gee.soil_moisture) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '180 days'
            )::numeric, 4) AS soil_moisture_mean,
            ROUND(AVG(gee.human_modification_index)::numeric, 3) AS human_modification_mean,
            ROUND(AVG(gee.gpw_density_per_km2)::numeric, 1) AS gpw_density_mean,
            ROUND(AVG(gee.nighttime_radiance_nw) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '365 days'
            )::numeric, 3) AS nighttime_radiance_mean,
            ROUND(AVG(gee.lst_day_celsius) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '90 days'
            )::numeric, 2) AS lst_day_mean,
            ROUND(AVG(gee.temperature_2m_c) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '180 days'
            )::numeric, 2) AS air_temp_mean,
            ROUND(AVG(gee.no2_mol_m2) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '90 days'
            )::numeric, 8) AS no2_mean,
            ROUND(AVG(gee.aerosol_index) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '90 days'
            )::numeric, 3) AS aerosol_mean,
            ROUND(AVG(gee.forest_cover_pct)::numeric, 2) AS forest_cover_mean,
            BOOL_OR(COALESCE(gee.forest_loss, FALSE)) AS forest_loss_any,
            MAX(gee.forest_loss_year) AS forest_loss_year_latest,
            ROUND(AVG(gee.cloud_cover_pct) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '90 days'
            )::numeric, 2) AS cloud_cover_mean,
            ROUND(MAX(gee.fire_brightness_kelvin) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '180 days'
            )::numeric, 1) AS fire_brightness_max,
            COUNT(*) FILTER (
              WHERE gee.burned_area_date IS NOT NULL
                AND gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '365 days'
            )::int AS burned_area_hits,
            ROUND(AVG(gee.sst_celsius) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '120 days'
            )::numeric, 2) AS sst_mean,
            ROUND(AVG(gee.sst_anom_celsius) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '120 days'
            )::numeric, 2) AS sst_anom_mean,
            ROUND(AVG(gee.elevation_m)::numeric, 0) AS elevation_mean,
            ROUND(AVG(gee.slope_degrees)::numeric, 1) AS slope_mean,
            ROUND(AVG(gee.aspect_degrees)::numeric, 1) AS aspect_mean,
            ROUND(AVG(gee.et_mm_8day) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '365 days'
            )::numeric, 2) AS et_mean,
            ROUND(AVG(gee.pet_mm_8day) FILTER (
              WHERE gee.observation_date >= (SELECT asof FROM latest_obs) - INTERVAL '365 days'
            )::numeric, 2) AS pet_mean
          FROM gee_base gee
          INNER JOIN grid_cells_tbl cell
            ON cell.grid_cell_id = gee.parent_grid_cell_id
           AND cell.grid_system = $1
          GROUP BY
            gee.parent_grid_cell_id,
            cell.lat,
            cell.lon,
            cell.bbox_min_lat,
            cell.bbox_min_lon,
            cell.bbox_max_lat,
            cell.bbox_max_lon,
            cell.cell_geojson
        ),
        latest_labels AS (
          SELECT DISTINCT ON (gee.parent_grid_cell_id)
            gee.parent_grid_cell_id AS station_id,
            gee.dynamic_world_label,
            gee.esa_worldcover_label,
            gee.modis_igbp_label
          FROM gee_base gee
          WHERE (
            gee.dynamic_world_label IS NOT NULL
            OR gee.esa_worldcover_label IS NOT NULL
            OR gee.modis_igbp_label IS NOT NULL
          )
          ORDER BY gee.parent_grid_cell_id, gee.observation_date DESC
        )
        SELECT
          gee_agg.station_id,
          gee_agg.station_name,
          gee_agg.lat,
          gee_agg.lon,
          gee_agg.bbox_min_lat,
          gee_agg.bbox_min_lon,
          gee_agg.bbox_max_lat,
          gee_agg.bbox_max_lon,
          gee_agg.grid_size_km,
          gee_agg.cell_geojson,
          gee_agg.profile_rows,
          gee_agg.recent_profile_rows,
          gee_agg.latest_profile_date,
          gee_agg.ndvi_mean,
          gee_agg.surface_water_pct_mean,
          gee_agg.surface_water_recurrence_pct_mean,
          gee_agg.soil_moisture_mean,
          gee_agg.human_modification_mean,
          gee_agg.gpw_density_mean,
          gee_agg.nighttime_radiance_mean,
          gee_agg.lst_day_mean,
          gee_agg.air_temp_mean,
          gee_agg.no2_mean,
          gee_agg.aerosol_mean,
          gee_agg.forest_cover_mean,
          gee_agg.forest_loss_any,
          gee_agg.forest_loss_year_latest,
          gee_agg.cloud_cover_mean,
          gee_agg.fire_brightness_max,
          gee_agg.burned_area_hits,
          gee_agg.sst_mean,
          gee_agg.sst_anom_mean,
          gee_agg.elevation_mean,
          gee_agg.slope_mean,
          gee_agg.aspect_mean,
          gee_agg.et_mean,
          gee_agg.pet_mean,
          latest_labels.dynamic_world_label,
          latest_labels.esa_worldcover_label,
          latest_labels.modis_igbp_label
        FROM gee_agg
        LEFT JOIN latest_labels ON latest_labels.station_id = gee_agg.station_id
        ORDER BY gee_agg.station_id ASC
      `,
    params,
  };
}

export function mapOverlaySample(
  row: Record<string, unknown>,
): EarthEngineOverlaySample {
  return {
    station_id: String(row.station_id ?? ""),
    station_name: String(row.station_name ?? row.station_id ?? "Unknown station"),
    lat: Number(row.lat ?? 0),
    lon: Number(row.lon ?? 0),
    bbox_min_lat: nullableNumber(row.bbox_min_lat, 6),
    bbox_min_lon: nullableNumber(row.bbox_min_lon, 6),
    bbox_max_lat: nullableNumber(row.bbox_max_lat, 6),
    bbox_max_lon: nullableNumber(row.bbox_max_lon, 6),
    grid_size_km: nullableNumber(row.grid_size_km, 2),
    cell_geojson: parseCellGeometry(row.cell_geojson),
    latest_profile_date: row.latest_profile_date ? String(row.latest_profile_date) : null,
    profile_rows: nullableInteger(row.profile_rows) ?? 0,
    recent_profile_rows: nullableInteger(row.recent_profile_rows) ?? 0,
    ndvi_mean: nullableNumber(row.ndvi_mean, 3),
    surface_water_pct_mean: nullableNumber(row.surface_water_pct_mean, 2),
    surface_water_recurrence_pct_mean: nullableNumber(row.surface_water_recurrence_pct_mean, 2),
    soil_moisture_mean: nullableNumber(row.soil_moisture_mean, 4),
    human_modification_mean: nullableNumber(row.human_modification_mean, 3),
    gpw_density_mean: nullableNumber(row.gpw_density_mean, 1),
    nighttime_radiance_mean: nullableNumber(row.nighttime_radiance_mean, 3),
    lst_day_mean: nullableNumber(row.lst_day_mean, 2),
    air_temp_mean: nullableNumber(row.air_temp_mean, 2),
    no2_mean: nullableNumber(row.no2_mean, 8),
    aerosol_mean: nullableNumber(row.aerosol_mean, 3),
    forest_cover_mean: nullableNumber(row.forest_cover_mean, 2),
    forest_loss_any: Boolean(row.forest_loss_any),
    forest_loss_year_latest: nullableInteger(row.forest_loss_year_latest),
    cloud_cover_mean: nullableNumber(row.cloud_cover_mean, 2),
    fire_brightness_max: nullableNumber(row.fire_brightness_max, 1),
    burned_area_hits: nullableInteger(row.burned_area_hits) ?? 0,
    sst_mean: nullableNumber(row.sst_mean, 2),
    sst_anom_mean: nullableNumber(row.sst_anom_mean, 2),
    elevation_mean: nullableNumber(row.elevation_mean, 0),
    slope_mean: nullableNumber(row.slope_mean, 1),
    aspect_mean: nullableNumber(row.aspect_mean, 1),
    et_mean: nullableNumber(row.et_mean, 2),
    pet_mean: nullableNumber(row.pet_mean, 2),
    dynamic_world_label: row.dynamic_world_label ? String(row.dynamic_world_label) : null,
    esa_worldcover_label: row.esa_worldcover_label ? String(row.esa_worldcover_label) : null,
    modis_igbp_label: row.modis_igbp_label ? String(row.modis_igbp_label) : null,
  };
}