import { booleanPointInPolygon } from "@turf/boolean-point-in-polygon";
import { centroid } from "@turf/centroid";
import { point as turfPoint } from "@turf/helpers";
import {
  EARTH_ENGINE_OVERLAY_META,
  type EarthEngineOverlayId,
  type EarthEngineVisualizationMode,
} from "@/config/earth-engine-overlays";

type OverlayProperties = Record<string, string | number | boolean | null>;
type OverlayPolygonGeometry = GeoJSON.Polygon | GeoJSON.MultiPolygon;

export type OverlayFeature = GeoJSON.Feature<GeoJSON.Geometry, OverlayProperties>;
export type OverlayFeatureCollection = GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  OverlayProperties
>;
export type OverlayRegionFeature = GeoJSON.Feature<
  OverlayPolygonGeometry,
  GeoJSON.GeoJsonProperties
>;
export type OverlayRegionCollection = GeoJSON.FeatureCollection<
  OverlayPolygonGeometry,
  GeoJSON.GeoJsonProperties
>;

export interface EarthEngineOverlaySample {
  station_id: string;
  station_name: string;
  lat: number;
  lon: number;
  bbox_min_lat: number | null;
  bbox_min_lon: number | null;
  bbox_max_lat: number | null;
  bbox_max_lon: number | null;
  grid_size_km: number | null;
  cell_geojson: unknown;
  latest_profile_date: string | null;
  profile_rows: number;
  recent_profile_rows: number;
  ndvi_mean: number | null;
  surface_water_pct_mean: number | null;
  surface_water_recurrence_pct_mean: number | null;
  soil_moisture_mean: number | null;
  human_modification_mean: number | null;
  gpw_density_mean: number | null;
  nighttime_radiance_mean: number | null;
  lst_day_mean: number | null;
  air_temp_mean: number | null;
  no2_mean: number | null;
  aerosol_mean: number | null;
  forest_cover_mean: number | null;
  forest_loss_any: boolean;
  forest_loss_year_latest: number | null;
  cloud_cover_mean: number | null;
  fire_brightness_max: number | null;
  burned_area_hits: number;
  sst_mean: number | null;
  sst_anom_mean: number | null;
  elevation_mean: number | null;
  slope_mean: number | null;
  aspect_mean: number | null;
  et_mean: number | null;
  pet_mean: number | null;
  dynamic_world_label: string | null;
  esa_worldcover_label: string | null;
  modis_igbp_label: string | null;
}

export interface EarthEngineOverlayResponse {
  generatedAt: string;
  samplingMode: "grid";
  gridSizeKm: number;
  samples: EarthEngineOverlaySample[];
}

export interface GridCellProfileResponse {
  generatedAt: string;
  samplingMode: "grid";
  gridSizeKm: number;
  gridCellId: string;
  sample: EarthEngineOverlaySample | null;
}

export interface EarthEngineOverlayBundle {
  cells: OverlayFeatureCollection | null;
  points: OverlayFeatureCollection | null;
  labels: OverlayFeatureCollection | null;
  summary: {
    featureCount: number;
    renderedFeatures: number;
    samplingMode: EarthEngineOverlayResponse["samplingMode"];
    visualizationMode: EarthEngineVisualizationMode;
    metricLabel: string;
    metricRange: string | null;
  };
}

interface BuildEarthEngineOverlayOptions {
  samplingMode: EarthEngineOverlayResponse["samplingMode"];
  visualizationMode: EarthEngineVisualizationMode;
  regions?: OverlayRegionCollection | null;
}

interface OverlayMetric {
  numericValue: number | null;
  displayValue: string | null;
  categoryKey: string | null;
  categoryLabel: string | null;
  hasData: boolean;
}

interface OverlayEntry {
  id: string;
  labelId: string;
  name: string;
  longitude: number;
  latitude: number;
  gridSizeKm: number | null;
  sampleCount: number;
  geometry: OverlayPolygonGeometry;
  metric: OverlayMetric;
}

interface NumericRange {
  min: number;
  max: number;
}

interface OverlayStyle {
  fillColor: string;
  fillOpacity: number;
  lineColor: string;
  lineOpacity: number;
  glowOpacity: number;
  scanOpacity: number;
  pointColor: string;
  pointRadius: number;
  pointOpacity: number;
  pointGlow: number;
  textColor: string;
}

type Rgb = [number, number, number];

const DEFAULT_GRID_SYSTEM = "nl_geodesic_5km";
const DEFAULT_BASE_COLOR: Rgb = [141, 163, 154];

const LAND_COVER_COLORS: Array<{ match: RegExp; color: Rgb }> = [
  { match: /wet|marsh|bog|mangrove/i, color: [102, 149, 144] },
  { match: /water|sea|lake|river|coast/i, color: [93, 132, 164] },
  { match: /forest|wood/i, color: [96, 132, 100] },
  { match: /grass|pasture|meadow|herb/i, color: [127, 159, 113] },
  { match: /cropland|crop|agri|farm/i, color: [164, 151, 105] },
  { match: /shrub|scrub/i, color: [138, 144, 101] },
  { match: /urban|built|settlement|impervious/i, color: [151, 138, 128] },
  { match: /bare|sand|rock/i, color: [160, 149, 132] },
  { match: /snow|ice/i, color: [198, 208, 216] },
];

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function roundTo(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function formatLandCoverLabel(label: string): string {
  return label
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function shortLabel(value: string | null): string | null {
  if (!value) return null;
  const compact = value.trim();
  if (!compact) return null;
  if (compact.length <= 14) return compact;

  const words = compact.split(/\s+/).filter(Boolean);
  if (words.length === 0) return compact.slice(0, 14);
  if (words.length === 1) return `${words[0].slice(0, 11)}…`;
  return words[0];
}

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function toRgb(value: string | undefined): Rgb {
  const match = value?.match(/\d+(?:\.\d+)?/g);
  if (!match || match.length < 3) return DEFAULT_BASE_COLOR;
  return [Number(match[0]), Number(match[1]), Number(match[2])];
}

function mixRgb(start: Rgb, end: Rgb, ratio: number): Rgb {
  return [0, 1, 2].map((index) =>
    Math.round(start[index] + (end[index] - start[index]) * clamp(ratio, 0, 1)),
  ) as Rgb;
}

function rgba(color: Rgb, alpha: number): string {
  const [red, green, blue] = color;
  return `rgba(${red},${green},${blue},${roundTo(alpha, 3)})`;
}

function deriveGridSystem(gridSizeKm: number | null): string {
  return gridSizeKm ? `nl_geodesic_${Math.round(gridSizeKm)}km` : DEFAULT_GRID_SYSTEM;
}

function buildRange(values: number[]): NumericRange {
  if (values.length === 0) {
    return { min: 0, max: 1 };
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) {
    return { min: min - 0.5, max: max + 0.5 };
  }

  return { min, max };
}

function normalizeValue(value: number | null, range: NumericRange): number {
  if (value == null) return 0.5;
  const span = range.max - range.min;
  if (span <= 0) return 0.5;
  return clamp((value - range.min) / span, 0, 1);
}

function buildNumericStyle(
  overlayId: EarthEngineOverlayId,
  normalized: number,
): OverlayStyle {
  const overlayMeta = EARTH_ENGINE_OVERLAY_META[overlayId];
  const accent = toRgb(overlayMeta.accent);
  const low = mixRgb([16, 22, 20], accent, 0.28);
  const high = mixRgb(accent, [245, 247, 250], 0.15);
  const fill = mixRgb(low, high, normalized);
  const point = mixRgb(fill, [255, 255, 255], 0.08);
  const isHotspot = overlayMeta.kind === "hotspot";

  return {
    fillColor: rgba(fill, 1),
    fillOpacity: roundTo((isHotspot ? 0.08 : 0.11) + normalized * 0.24, 3),
    lineColor: rgba(mixRgb(fill, [255, 255, 255], 0.16), 0.62),
    lineOpacity: roundTo(0.18 + normalized * 0.28, 3),
    glowOpacity: roundTo((isHotspot ? 0.1 : 0.05) + normalized * 0.08, 3),
    scanOpacity: roundTo(0.05 + normalized * 0.07, 3),
    pointColor: rgba(point, isHotspot ? 0.9 : 0.8),
    pointRadius: roundTo((isHotspot ? 4.6 : 3.4) + normalized * (isHotspot ? 8.4 : 5.8), 2),
    pointOpacity: roundTo((isHotspot ? 0.84 : 0.74) + normalized * 0.08, 3),
    pointGlow: roundTo((isHotspot ? 0.18 : 0.1) + normalized * 0.12, 3),
    textColor: rgba(mixRgb(fill, [255, 255, 255], 0.28), 0.86),
  };
}

function landCoverColor(label: string | null): Rgb {
  if (!label) return DEFAULT_BASE_COLOR;
  const match = LAND_COVER_COLORS.find((entry) => entry.match.test(label));
  return match?.color ?? DEFAULT_BASE_COLOR;
}

function buildCategoricalStyle(label: string | null, strength: number): OverlayStyle {
  const base = landCoverColor(label);
  const fill = mixRgb([18, 23, 21], base, 0.55 + clamp(strength, 0, 1) * 0.25);

  return {
    fillColor: rgba(fill, 1),
    fillOpacity: roundTo(0.14 + clamp(strength, 0.25, 1) * 0.2, 3),
    lineColor: rgba(mixRgb(fill, [255, 255, 255], 0.12), 0.58),
    lineOpacity: roundTo(0.22 + clamp(strength, 0.25, 1) * 0.18, 3),
    glowOpacity: roundTo(0.06 + clamp(strength, 0.25, 1) * 0.08, 3),
    scanOpacity: roundTo(0.05 + clamp(strength, 0.25, 1) * 0.05, 3),
    pointColor: rgba(mixRgb(fill, [255, 255, 255], 0.06), 0.82),
    pointRadius: roundTo(4 + clamp(strength, 0.25, 1) * 4.8, 2),
    pointOpacity: roundTo(0.76 + clamp(strength, 0.25, 1) * 0.08, 3),
    pointGlow: roundTo(0.12 + clamp(strength, 0.25, 1) * 0.08, 3),
    textColor: rgba(mixRgb(fill, [255, 255, 255], 0.22), 0.86),
  };
}

function pickLandCoverLabel(sample: EarthEngineOverlaySample): string | null {
  if (sample.esa_worldcover_label) {
    return formatLandCoverLabel(sample.esa_worldcover_label);
  }
  if (sample.dynamic_world_label) {
    return formatLandCoverLabel(sample.dynamic_world_label);
  }
  if (sample.modis_igbp_label) {
    return formatLandCoverLabel(sample.modis_igbp_label);
  }
  return null;
}

function chooseFirstNumber(...values: Array<number | null | undefined>): number | null {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function formatMetricValue(
  overlayId: EarthEngineOverlayId,
  value: number | null,
  categoryLabel?: string | null,
): string | null {
  if (overlayId === "habitat") {
    return shortLabel(categoryLabel ?? null);
  }
  if (value == null) return null;

  switch (overlayId) {
    case "vegetation":
      return value.toFixed(2);
    case "wetness":
      return `${value.toFixed(0)}%`;
    case "human_pressure":
      return value >= 100 ? value.toFixed(0) : value.toFixed(1);
    case "thermal":
      return `${value.toFixed(1)}°`;
    case "air_quality":
      return value.toFixed(value < 1 ? 2 : 1);
    case "forest":
      return `${value.toFixed(0)}%`;
    case "cloud":
      return `${value.toFixed(0)}%`;
    case "fire":
      return value >= 100 ? `${value.toFixed(0)}K` : `${value.toFixed(0)} hits`;
    case "sst":
      return `${value.toFixed(1)}°`;
    case "terrain":
      return `${value.toFixed(0)}m`;
    default:
      return value.toFixed(1);
  }
}

function resolveOverlayMetric(
  sample: EarthEngineOverlaySample,
  overlayId: EarthEngineOverlayId,
): OverlayMetric {
  switch (overlayId) {
    case "vegetation": {
      const value = sample.ndvi_mean;
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "wetness": {
      const moisture = sample.soil_moisture_mean != null ? sample.soil_moisture_mean * 100 : null;
      const components = [
        sample.surface_water_pct_mean,
        sample.surface_water_recurrence_pct_mean,
        moisture,
      ].filter((value): value is number => typeof value === "number" && Number.isFinite(value));
      const value = mean(components);
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "human_pressure": {
      const value = chooseFirstNumber(
        sample.human_modification_mean,
        sample.gpw_density_mean,
        sample.nighttime_radiance_mean,
      );
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "habitat": {
      const label = pickLandCoverLabel(sample);
      return {
        numericValue: label ? 1 : null,
        displayValue: shortLabel(label),
        categoryKey: label?.toLowerCase() ?? null,
        categoryLabel: label,
        hasData: Boolean(label),
      };
    }
    case "thermal": {
      const value = chooseFirstNumber(sample.air_temp_mean, sample.lst_day_mean);
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "air_quality": {
      const value = chooseFirstNumber(sample.no2_mean, sample.aerosol_mean);
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "forest": {
      const value = sample.forest_cover_mean;
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "cloud": {
      const value = sample.cloud_cover_mean;
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "fire": {
      const value = chooseFirstNumber(sample.fire_brightness_max, sample.burned_area_hits);
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "sst": {
      const value = chooseFirstNumber(sample.sst_mean, sample.sst_anom_mean);
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
    case "terrain": {
      const value = sample.elevation_mean;
      return {
        numericValue: value,
        displayValue: formatMetricValue(overlayId, value),
        categoryKey: null,
        categoryLabel: null,
        hasData: value != null,
      };
    }
  }
}

function bboxGeometry(sample: EarthEngineOverlaySample): OverlayPolygonGeometry | null {
  const minLat = sample.bbox_min_lat;
  const minLon = sample.bbox_min_lon;
  const maxLat = sample.bbox_max_lat;
  const maxLon = sample.bbox_max_lon;

  if (
    minLat == null ||
    minLon == null ||
    maxLat == null ||
    maxLon == null
  ) {
    return null;
  }

  return {
    type: "Polygon",
    coordinates: [[
      [minLon, minLat],
      [maxLon, minLat],
      [maxLon, maxLat],
      [minLon, maxLat],
      [minLon, minLat],
    ]],
  };
}

function isOverlayPolygonGeometry(value: unknown): value is OverlayPolygonGeometry {
  return Boolean(
    value &&
      typeof value === "object" &&
      "type" in value &&
      ((value as { type?: string }).type === "Polygon" ||
        (value as { type?: string }).type === "MultiPolygon"),
  );
}

function extractPolygonGeometry(value: unknown): OverlayPolygonGeometry | null {
  if (isOverlayPolygonGeometry(value)) return value;

  if (!value || typeof value !== "object") return null;

  const maybeFeature = value as {
    type?: string;
    geometry?: unknown;
  };

  if (maybeFeature.type === "Feature" && isOverlayPolygonGeometry(maybeFeature.geometry)) {
    return maybeFeature.geometry;
  }

  return null;
}

function geometryCenter(geometry: OverlayPolygonGeometry): [number, number] | null {
  try {
    const center = centroid({
      type: "Feature",
      geometry,
      properties: {},
    });
    const [longitude, latitude] = center.geometry.coordinates;
    if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null;
    return [longitude, latitude];
  } catch {
    return null;
  }
}

function aggregateMetrics(
  overlayId: EarthEngineOverlayId,
  metrics: OverlayMetric[],
): OverlayMetric | null {
  const overlayMeta = EARTH_ENGINE_OVERLAY_META[overlayId];
  const usable = metrics.filter((metric) => metric.hasData);
  if (usable.length === 0) return null;

  if (overlayMeta.kind === "categorical") {
    const counts = new Map<string, { label: string; count: number }>();
    for (const metric of usable) {
      const key = metric.categoryKey;
      const label = metric.categoryLabel;
      if (!key || !label) continue;
      const current = counts.get(key);
      counts.set(key, {
        label,
        count: (current?.count ?? 0) + 1,
      });
    }

    if (counts.size === 0) return null;

    const dominant = [...counts.entries()].sort((left, right) => {
      return right[1].count - left[1].count;
    })[0];
    const strength = dominant[1].count / usable.length;

    return {
      numericValue: strength,
      displayValue: shortLabel(dominant[1].label),
      categoryKey: dominant[0],
      categoryLabel: dominant[1].label,
      hasData: true,
    };
  }

  const numericValues = usable
    .map((metric) => metric.numericValue)
    .filter((value): value is number => value != null);
  if (numericValues.length === 0) return null;

  const aggregateValue =
    overlayMeta.kind === "hotspot"
      ? Math.max(...numericValues)
      : mean(numericValues);

  return {
    numericValue: aggregateValue,
    displayValue: formatMetricValue(overlayId, aggregateValue),
    categoryKey: null,
    categoryLabel: null,
    hasData: aggregateValue != null,
  };
}

function buildGridEntries(
  samples: EarthEngineOverlaySample[],
  overlayId: EarthEngineOverlayId,
): OverlayEntry[] {
  const entries: OverlayEntry[] = [];

  for (const sample of samples) {
    const metric = resolveOverlayMetric(sample, overlayId);
    if (!metric.hasData) continue;

    const geometry = extractPolygonGeometry(sample.cell_geojson) ?? bboxGeometry(sample);
    if (!geometry) continue;

    const center = geometryCenter(geometry) ?? [sample.lon, sample.lat];
    entries.push({
      id: sample.station_id,
      labelId: sample.station_id,
      name: sample.station_name,
      longitude: center[0],
      latitude: center[1],
      gridSizeKm: sample.grid_size_km,
      sampleCount: 1,
      geometry,
      metric,
    });
  }

  return entries;
}

function findRegionForSample(
  sample: EarthEngineOverlaySample,
  regions: OverlayRegionFeature[],
): OverlayRegionFeature | null {
  const location = turfPoint([sample.lon, sample.lat]);

  for (const region of regions) {
    try {
      if (booleanPointInPolygon(location, region)) {
        return region;
      }
    } catch {
      continue;
    }
  }

  return null;
}

function buildMunicipalityEntries(
  samples: EarthEngineOverlaySample[],
  overlayId: EarthEngineOverlayId,
  regions: OverlayRegionCollection | null | undefined,
): OverlayEntry[] {
  if (!regions?.features?.length) return [];

  const validRegions = regions.features.filter(
    (feature): feature is OverlayRegionFeature => isOverlayPolygonGeometry(feature.geometry),
  );
  const buckets = new Map<
    string,
    {
      region: OverlayRegionFeature;
      metrics: OverlayMetric[];
      gridSizeKm: number | null;
    }
  >();

  for (const sample of samples) {
    const metric = resolveOverlayMetric(sample, overlayId);
    if (!metric.hasData) continue;

    const region = findRegionForSample(sample, validRegions);
    if (!region) continue;

    const regionName = String(region.properties?.naam ?? region.properties?.name ?? sample.station_id);
    const existing = buckets.get(regionName);
    if (existing) {
      existing.metrics.push(metric);
      existing.gridSizeKm = existing.gridSizeKm ?? sample.grid_size_km;
      continue;
    }

    buckets.set(regionName, {
      region,
      metrics: [metric],
      gridSizeKm: sample.grid_size_km,
    });
  }

  const entries: OverlayEntry[] = [];
  for (const [regionName, bucket] of buckets) {
    const metric = aggregateMetrics(overlayId, bucket.metrics);
    if (!metric?.hasData) continue;

    const center = geometryCenter(bucket.region.geometry);
    if (!center) continue;

    entries.push({
      id: regionName,
      labelId: regionName,
      name: regionName,
      longitude: center[0],
      latitude: center[1],
      gridSizeKm: bucket.gridSizeKm,
      sampleCount: bucket.metrics.length,
      geometry: bucket.region.geometry,
      metric,
    });
  }

  return entries;
}

function buildBundleFromEntries(
  entries: OverlayEntry[],
  overlayId: EarthEngineOverlayId,
  options: BuildEarthEngineOverlayOptions,
): EarthEngineOverlayBundle | null {
  if (entries.length === 0) return null;

  const overlayMeta = EARTH_ENGINE_OVERLAY_META[overlayId];
  const numericValues = entries
    .map((entry) => entry.metric.numericValue)
    .filter((value): value is number => value != null);
  const range = buildRange(numericValues);

  const cells: OverlayFeature[] = [];
  const points: OverlayFeature[] = [];
  const labels: OverlayFeature[] = [];

  for (const entry of entries) {
    const normalized = normalizeValue(entry.metric.numericValue, range);
    const style =
      overlayMeta.kind === "categorical"
        ? buildCategoricalStyle(entry.metric.categoryLabel, entry.metric.numericValue ?? 0.65)
        : buildNumericStyle(overlayId, normalized);
    const text =
      overlayMeta.kind === "categorical"
        ? shortLabel(entry.metric.categoryLabel)
        : shortLabel(entry.metric.displayValue);
    const properties: OverlayProperties = {
      grid_cell_id: entry.id,
      station_id: entry.id,
      station_name: entry.name,
      grid_system: deriveGridSystem(entry.gridSizeKm),
      grid_size_km: entry.gridSizeKm,
      lat: roundTo(entry.latitude, 6),
      lon: roundTo(entry.longitude, 6),
      overlay_id: overlayId,
      overlay_kind: overlayMeta.kind,
      overlay_value:
        entry.metric.numericValue != null ? roundTo(entry.metric.numericValue, 4) : null,
      overlay_label: entry.metric.categoryLabel ?? entry.metric.displayValue,
      sample_count: entry.sampleCount,
      fill_color: style.fillColor,
      fill_opacity: style.fillOpacity,
      line_color: style.lineColor,
      line_opacity: style.lineOpacity,
      glow_opacity: style.glowOpacity,
      scan_opacity: style.scanOpacity,
      point_color: style.pointColor,
      point_radius: style.pointRadius,
      point_opacity: style.pointOpacity,
      point_glow: style.pointGlow,
      text: text,
      text_color: style.textColor,
    };

    cells.push({
      type: "Feature",
      geometry: entry.geometry,
      properties,
    });

    points.push({
      type: "Feature",
      geometry: {
        type: "Point",
        coordinates: [entry.longitude, entry.latitude],
      },
      properties,
    });

    if (text) {
      labels.push({
        type: "Feature",
        geometry: {
          type: "Point",
          coordinates: [entry.longitude, entry.latitude],
        },
        properties: {
          grid_cell_id: entry.labelId,
          station_id: entry.id,
          text,
          text_color: style.textColor,
          overlay_id: overlayId,
        },
      });
    }
  }

  return {
    cells: { type: "FeatureCollection", features: cells },
    points: { type: "FeatureCollection", features: points },
    labels: { type: "FeatureCollection", features: labels },
    summary: {
      featureCount: entries.length,
      renderedFeatures: entries.length,
      samplingMode: options.samplingMode,
      visualizationMode: options.visualizationMode,
      metricLabel: overlayMeta.shortLabel,
      metricRange:
        overlayMeta.kind === "categorical"
          ? `${new Set(entries.map((entry) => entry.metric.categoryLabel).filter(Boolean)).size} classes`
          : numericValues.length > 0
            ? `${formatMetricValue(overlayId, range.min) ?? "—"} – ${formatMetricValue(overlayId, range.max) ?? "—"}`
            : null,
    },
  };
}

export function buildEarthEngineOverlayBundle(
  samples: EarthEngineOverlaySample[],
  overlayId: EarthEngineOverlayId,
  options: BuildEarthEngineOverlayOptions,
): EarthEngineOverlayBundle | null {
  const entries =
    options.visualizationMode === "municipality"
      ? buildMunicipalityEntries(samples, overlayId, options.regions)
      : buildGridEntries(samples, overlayId);

  return buildBundleFromEntries(entries, overlayId, options);
}