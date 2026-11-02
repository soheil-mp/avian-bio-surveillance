import booleanPointInPolygon from '@turf/boolean-point-in-polygon';
import { point } from '@turf/helpers';
import simplify from '@turf/simplify';
import { NextRequest, NextResponse } from 'next/server';

import {
  defaultWeatherDateRange,
  resolveWeatherDateRange,
  type ExploreWeatherKnmiStats,
  type ExploreWeatherMunicipality,
  type ExploreWeatherPayload,
  type ExploreWeatherStatSummary,
  type ExploreWeatherWindVector,
} from '@/app/explore/weather';
import { MUNICIPALITIES_WFS_URL } from '@/config/map';
import { query } from '@/lib/db';
import { withMeasuredApiRoute } from '@/lib/performance/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const CACHE_TTL_MS = 15 * 60 * 1000;
const WEATHER_CACHE_VERSION = 'v6';
const MUNICIPALITY_CACHE_TTL_MS = 24 * 60 * 60 * 1000;
const MUNICIPALITY_SIMPLIFY_TOLERANCE = 0.0035;
const MUNICIPALITY_SPATIAL_STEP_DEG = 0.25;
const RESPONSE_HEADERS = {
  'Cache-Control': 'public, s-maxage=900, stale-while-revalidate=600',
};

type MunicipalityBoundary = {
  municipalityId: string;
  name: string;
  geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon;
  centroid: { lat: number; lng: number } | null;
  bbox: [number, number, number, number] | null;
};

type MunicipalityBoundaryCollection = GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  GeoJSON.GeoJsonProperties
>;

type KnmiAggregateRow = {
  station_id: string;
  station_lat: number | string | null;
  station_lon: number | string | null;
  temperature_c_avg: number | string | null;
  temperature_c_min: number | string | null;
  temperature_c_max: number | string | null;
  precipitation_mm_avg: number | string | null;
  precipitation_mm_min: number | string | null;
  precipitation_mm_max: number | string | null;
  relative_humidity_pct_avg: number | string | null;
  relative_humidity_pct_min: number | string | null;
  relative_humidity_pct_max: number | string | null;
  wind_speed_ms_avg: number | string | null;
  wind_speed_ms_min: number | string | null;
  wind_speed_ms_max: number | string | null;
  pressure_hpa_avg: number | string | null;
  pressure_hpa_min: number | string | null;
  pressure_hpa_max: number | string | null;
  solar_radiation_wm2_avg: number | string | null;
  solar_radiation_wm2_min: number | string | null;
  solar_radiation_wm2_max: number | string | null;
  cloud_cover_okta_avg: number | string | null;
  cloud_cover_okta_min: number | string | null;
  cloud_cover_okta_max: number | string | null;
  latest_bucket: string | null;
};

type WindVectorRow = {
  station_id: string;
  station_name: string | null;
  station_lat: number | string | null;
  station_lon: number | string | null;
  observed_on: string;
  wind_speed_ms: number | string | null;
  flow_direction_deg: number | string | null;
  sample_order: number | string;
  sample_count: number | string;
};

type DroughtMonthRow = {
  observation_month: string | null;
};

type CoverageWindowRow = {
  latest_date: string | null;
};

type DroughtAggregateRow = {
  grid_cell_id: string;
  lat: number | string;
  lon: number | string;
  spi_3m: number | string | null;
  spei_3m: number | string | null;
  observation_month: string | null;
};

type MunicipalityAccumulator = {
  stationCount: number;
  droughtCellCount: number;
  temperature: NumericSummaryAccumulator;
  precipitation: NumericSummaryAccumulator;
  relativeHumidity: NumericSummaryAccumulator;
  windSpeed: NumericSummaryAccumulator;
  pressure: NumericSummaryAccumulator;
  solarRadiation: NumericSummaryAccumulator;
  cloudCover: NumericSummaryAccumulator;
  windVectorXTotal: number;
  windVectorYTotal: number;
  windVectorSamples: number;
  spi3Total: number;
  spi3Samples: number;
  spei3Total: number;
  spei3Samples: number;
  latestKnmiBucket: string | null;
  droughtObservationMonth: string | null;
};

type KnmiStationSummary = {
  stationId: string;
  lat: number;
  lon: number;
  latestBucket: string | null;
  temperatureAvg: number | null;
  temperatureMin: number | null;
  temperatureMax: number | null;
  precipitationAvg: number | null;
  precipitationMin: number | null;
  precipitationMax: number | null;
  relativeHumidityAvg: number | null;
  relativeHumidityMin: number | null;
  relativeHumidityMax: number | null;
  windSpeedAvg: number | null;
  windSpeedMin: number | null;
  windSpeedMax: number | null;
  pressureAvg: number | null;
  pressureMin: number | null;
  pressureMax: number | null;
  solarRadiationAvg: number | null;
  solarRadiationMin: number | null;
  solarRadiationMax: number | null;
  cloudCoverAvg: number | null;
  cloudCoverMin: number | null;
  cloudCoverMax: number | null;
};

type ExploreWeatherRouteCacheState = {
  weatherCache: Map<string, { expiresAt: number; payload: ExploreWeatherPayload }>;
  municipalityBoundaryCache: { expiresAt: number; payload: MunicipalityBoundary[] } | null;
  municipalitySpatialIndexCache: { expiresAt: number; cells: Map<string, MunicipalityBoundary[]> } | null;
  stationMunicipalityCache: { expiresAt: number; ids: Map<string, string | null> } | null;
  droughtMunicipalityCache: { expiresAt: number; ids: Map<string, string | null> } | null;
};

const exploreWeatherRouteState = (
  globalThis as typeof globalThis & {
    __exploreWeatherRouteState?: ExploreWeatherRouteCacheState;
  }
).__exploreWeatherRouteState ??= {
  weatherCache: new Map<string, { expiresAt: number; payload: ExploreWeatherPayload }>(),
  municipalityBoundaryCache: null,
  municipalitySpatialIndexCache: null,
  stationMunicipalityCache: null,
  droughtMunicipalityCache: null,
};

const weatherCache = exploreWeatherRouteState.weatherCache;

type NumericSummaryAccumulator = {
  total: number;
  samples: number;
  min: number | null;
  max: number | null;
};

function createNumericSummaryAccumulator(): NumericSummaryAccumulator {
  return {
    total: 0,
    samples: 0,
    min: null,
    max: null,
  };
}

function updateNumericSummaryAccumulator(
  accumulator: NumericSummaryAccumulator,
  avg: number | null,
  min: number | null,
  max: number | null,
) {
  if (avg !== null) {
    accumulator.total += avg;
    accumulator.samples += 1;
  }

  if (min !== null) {
    accumulator.min = accumulator.min === null ? min : Math.min(accumulator.min, min);
  }

  if (max !== null) {
    accumulator.max = accumulator.max === null ? max : Math.max(accumulator.max, max);
  }
}

function buildStatSummary(accumulator: NumericSummaryAccumulator): ExploreWeatherStatSummary {
  return {
    avg: averageOrNull(accumulator.total, accumulator.samples),
    min: accumulator.min,
    max: accumulator.max,
  };
}

function accumulatorHasKnmiSummary(accumulator: MunicipalityAccumulator) {
  return (
    accumulator.temperature.samples > 0 ||
    accumulator.precipitation.samples > 0 ||
    accumulator.relativeHumidity.samples > 0 ||
    accumulator.windSpeed.samples > 0 ||
    accumulator.pressure.samples > 0 ||
    accumulator.solarRadiation.samples > 0 ||
    accumulator.cloudCover.samples > 0
  );
}

function applyKnmiStationSummary(
  accumulator: MunicipalityAccumulator,
  summary: KnmiStationSummary,
) {
  updateNumericSummaryAccumulator(
    accumulator.temperature,
    summary.temperatureAvg,
    summary.temperatureMin,
    summary.temperatureMax,
  );
  updateNumericSummaryAccumulator(
    accumulator.precipitation,
    summary.precipitationAvg,
    summary.precipitationMin,
    summary.precipitationMax,
  );
  updateNumericSummaryAccumulator(
    accumulator.relativeHumidity,
    summary.relativeHumidityAvg,
    summary.relativeHumidityMin,
    summary.relativeHumidityMax,
  );
  updateNumericSummaryAccumulator(
    accumulator.windSpeed,
    summary.windSpeedAvg,
    summary.windSpeedMin,
    summary.windSpeedMax,
  );
  updateNumericSummaryAccumulator(
    accumulator.pressure,
    summary.pressureAvg,
    summary.pressureMin,
    summary.pressureMax,
  );
  updateNumericSummaryAccumulator(
    accumulator.solarRadiation,
    summary.solarRadiationAvg,
    summary.solarRadiationMin,
    summary.solarRadiationMax,
  );
  updateNumericSummaryAccumulator(
    accumulator.cloudCover,
    summary.cloudCoverAvg,
    summary.cloudCoverMin,
    summary.cloudCoverMax,
  );

  if (summary.latestBucket && (!accumulator.latestKnmiBucket || summary.latestBucket > accumulator.latestKnmiBucket)) {
    accumulator.latestKnmiBucket = summary.latestBucket;
  }
}

function squaredDistanceBetweenPoints(
  from: { lat: number; lng: number },
  to: { lat: number; lng: number },
) {
  const latitudeRadians = ((from.lat + to.lat) / 2) * (Math.PI / 180);
  const longitudeScale = Math.max(Math.cos(latitudeRadians), 0.25);
  const deltaLat = from.lat - to.lat;
  const deltaLng = (from.lng - to.lng) * longitudeScale;

  return (deltaLat * deltaLat) + (deltaLng * deltaLng);
}

function nearestKnmiStationSummary(
  municipality: MunicipalityBoundary,
  summaries: KnmiStationSummary[],
) {
  if (!municipality.centroid || summaries.length === 0) {
    return null;
  }

  let nearest: KnmiStationSummary | null = null;
  let nearestDistance = Number.POSITIVE_INFINITY;

  for (const summary of summaries) {
    const distance = squaredDistanceBetweenPoints(
      municipality.centroid,
      { lat: summary.lat, lng: summary.lon },
    );

    if (distance < nearestDistance) {
      nearest = summary;
      nearestDistance = distance;
    }
  }

  return nearest;
}

function createAccumulator(): MunicipalityAccumulator {
  return {
    stationCount: 0,
    droughtCellCount: 0,
    temperature: createNumericSummaryAccumulator(),
    precipitation: createNumericSummaryAccumulator(),
    relativeHumidity: createNumericSummaryAccumulator(),
    windSpeed: createNumericSummaryAccumulator(),
    pressure: createNumericSummaryAccumulator(),
    solarRadiation: createNumericSummaryAccumulator(),
    cloudCover: createNumericSummaryAccumulator(),
    windVectorXTotal: 0,
    windVectorYTotal: 0,
    windVectorSamples: 0,
    spi3Total: 0,
    spi3Samples: 0,
    spei3Total: 0,
    spei3Samples: 0,
    latestKnmiBucket: null,
    droughtObservationMonth: null,
  };
}

function numberOrNull(value: number | string | null | undefined) {
  if (value === null || value === undefined) {
    return null;
  }

  const numericValue = Number(value);
  return Number.isFinite(numericValue) ? numericValue : null;
}

function geometryBounds(geometry: GeoJSON.Geometry) {
  let minLng = Number.POSITIVE_INFINITY;
  let minLat = Number.POSITIVE_INFINITY;
  let maxLng = Number.NEGATIVE_INFINITY;
  let maxLat = Number.NEGATIVE_INFINITY;

  const visitCoordinates = (coordinates: unknown) => {
    if (!Array.isArray(coordinates) || coordinates.length === 0) {
      return;
    }

    if (
      coordinates.length >= 2 &&
      typeof coordinates[0] === 'number' &&
      typeof coordinates[1] === 'number'
    ) {
      const lng = coordinates[0];
      const lat = coordinates[1];
      minLng = Math.min(minLng, lng);
      minLat = Math.min(minLat, lat);
      maxLng = Math.max(maxLng, lng);
      maxLat = Math.max(maxLat, lat);
      return;
    }

    for (const child of coordinates) {
      visitCoordinates(child);
    }
  };

  visitCoordinates((geometry as { coordinates?: unknown }).coordinates);

  if (
    !Number.isFinite(minLng) ||
    !Number.isFinite(minLat) ||
    !Number.isFinite(maxLng) ||
    !Number.isFinite(maxLat)
  ) {
    return null;
  }

  return [minLng, minLat, maxLng, maxLat] as [number, number, number, number];
}

function geometryCentroid(geometry: GeoJSON.Geometry) {
  const bounds = geometryBounds(geometry);
  if (!bounds) {
    return null;
  }

  return {
    lng: (bounds[0] + bounds[2]) / 2,
    lat: (bounds[1] + bounds[3]) / 2,
  };
}

function pointInBounds(
  lng: number,
  lat: number,
  bounds: [number, number, number, number] | null,
) {
  if (!bounds) {
    return true;
  }

  return lng >= bounds[0] && lng <= bounds[2] && lat >= bounds[1] && lat <= bounds[3];
}

function isPolygonGeometry(
  geometry: GeoJSON.Geometry | null | undefined,
): geometry is GeoJSON.Polygon | GeoJSON.MultiPolygon {
  return geometry?.type === 'Polygon' || geometry?.type === 'MultiPolygon';
}

function isFeatureCollection(value: unknown): value is MunicipalityBoundaryCollection {
  return (
    !!value &&
    typeof value === 'object' &&
    Array.isArray((value as { features?: unknown }).features)
  );
}

function municipalitySpatialBucket(value: number) {
  return Math.floor(value / MUNICIPALITY_SPATIAL_STEP_DEG);
}

function municipalitySpatialCellKey(lng: number, lat: number) {
  return `${municipalitySpatialBucket(lng)}:${municipalitySpatialBucket(lat)}`;
}

function buildMunicipalitySpatialIndex(municipalities: MunicipalityBoundary[]) {
  const cells = new Map<string, MunicipalityBoundary[]>();

  for (const municipality of municipalities) {
    if (!municipality.bbox) {
      continue;
    }

    const minLngBucket = municipalitySpatialBucket(municipality.bbox[0]);
    const minLatBucket = municipalitySpatialBucket(municipality.bbox[1]);
    const maxLngBucket = municipalitySpatialBucket(municipality.bbox[2]);
    const maxLatBucket = municipalitySpatialBucket(municipality.bbox[3]);

    for (let lngBucket = minLngBucket; lngBucket <= maxLngBucket; lngBucket += 1) {
      for (let latBucket = minLatBucket; latBucket <= maxLatBucket; latBucket += 1) {
        const key = `${lngBucket}:${latBucket}`;
        const current = cells.get(key);

        if (current) {
          current.push(municipality);
        } else {
          cells.set(key, [municipality]);
        }
      }
    }
  }

  return cells;
}

function resetMunicipalityLookupCaches(expiresAt: number, municipalities: MunicipalityBoundary[]) {
  exploreWeatherRouteState.municipalitySpatialIndexCache = {
    expiresAt,
    cells: buildMunicipalitySpatialIndex(municipalities),
  };
  exploreWeatherRouteState.stationMunicipalityCache = {
    expiresAt,
    ids: new Map(),
  };
  exploreWeatherRouteState.droughtMunicipalityCache = {
    expiresAt,
    ids: new Map(),
  };
}

function ensureStationMunicipalityCache() {
  if (
    exploreWeatherRouteState.stationMunicipalityCache &&
    exploreWeatherRouteState.stationMunicipalityCache.expiresAt > Date.now()
  ) {
    return exploreWeatherRouteState.stationMunicipalityCache;
  }

  const expiresAt = exploreWeatherRouteState.municipalityBoundaryCache?.expiresAt
    ?? (Date.now() + MUNICIPALITY_CACHE_TTL_MS);
  exploreWeatherRouteState.stationMunicipalityCache = {
    expiresAt,
    ids: new Map(),
  };
  return exploreWeatherRouteState.stationMunicipalityCache;
}

function ensureDroughtMunicipalityCache() {
  if (
    exploreWeatherRouteState.droughtMunicipalityCache &&
    exploreWeatherRouteState.droughtMunicipalityCache.expiresAt > Date.now()
  ) {
    return exploreWeatherRouteState.droughtMunicipalityCache;
  }

  const expiresAt = exploreWeatherRouteState.municipalityBoundaryCache?.expiresAt
    ?? (Date.now() + MUNICIPALITY_CACHE_TTL_MS);
  exploreWeatherRouteState.droughtMunicipalityCache = {
    expiresAt,
    ids: new Map(),
  };
  return exploreWeatherRouteState.droughtMunicipalityCache;
}

function slimNamedBoundaryFeatures(collection: MunicipalityBoundaryCollection) {
  return {
    ...collection,
    features: collection.features.map((feature, index) => {
      const name =
        feature.properties && typeof feature.properties === 'object'
          ? (feature.properties.naam as string | undefined)
          : undefined;

      return {
        ...feature,
        id: feature.id ?? index + 1,
        properties: typeof name === 'string' ? { naam: name } : {},
      };
    }),
  } satisfies MunicipalityBoundaryCollection;
}

async function loadMunicipalities() {
  if (
    exploreWeatherRouteState.municipalityBoundaryCache &&
    exploreWeatherRouteState.municipalityBoundaryCache.expiresAt > Date.now()
  ) {
    if (
      !exploreWeatherRouteState.municipalitySpatialIndexCache ||
      exploreWeatherRouteState.municipalitySpatialIndexCache.expiresAt <= Date.now()
    ) {
      resetMunicipalityLookupCaches(
        exploreWeatherRouteState.municipalityBoundaryCache.expiresAt,
        exploreWeatherRouteState.municipalityBoundaryCache.payload,
      );
    }

    return exploreWeatherRouteState.municipalityBoundaryCache.payload;
  }

  const response = await fetch(MUNICIPALITIES_WFS_URL, {
    cache: 'force-cache',
    next: { revalidate: 86_400 },
    headers: {
      Accept: 'application/json',
      'User-Agent': 'avian-biosurveillance-dashboard/0.1',
    },
  });

  if (!response.ok) {
    throw new Error(`Municipality boundary request failed with ${response.status}`);
  }

  const raw = await response.json();
  if (!isFeatureCollection(raw)) {
    throw new Error('Municipality boundary response is malformed');
  }

  const data = slimNamedBoundaryFeatures(
    simplify(raw, {
      tolerance: MUNICIPALITY_SIMPLIFY_TOLERANCE,
      highQuality: false,
      mutate: false,
    }) as MunicipalityBoundaryCollection,
  );

  const payload = data.features.flatMap((feature, index) => {
    if (!isPolygonGeometry(feature.geometry)) {
      return [];
    }

    const name =
      feature.properties && typeof feature.properties === 'object'
        ? (feature.properties.naam as string | undefined)
        : undefined;
    const municipalityId = String(feature.id ?? name ?? `municipality-${index + 1}`);

    return [{
      municipalityId,
      name: name ?? `Municipality ${index + 1}`,
      geometry: feature.geometry,
      centroid: geometryCentroid(feature.geometry),
      bbox: geometryBounds(feature.geometry),
    } satisfies MunicipalityBoundary];
  });

  exploreWeatherRouteState.municipalityBoundaryCache = {
    expiresAt: Date.now() + MUNICIPALITY_CACHE_TTL_MS,
    payload,
  };
  resetMunicipalityLookupCaches(exploreWeatherRouteState.municipalityBoundaryCache.expiresAt, payload);

  return payload;
}

async function loadKnmiAggregate(dateFrom: string, dateTo: string) {
  const result = await query<KnmiAggregateRow>(`
    WITH station_window AS (
      SELECT
        station_id,
        ROUND(AVG(station_lat_avg)::numeric, 6)::double precision AS station_lat,
        ROUND(AVG(station_lon_avg)::numeric, 6)::double precision AS station_lon,
        ROUND(AVG(temperature_c_avg)::numeric, 2)::double precision AS temperature_c_avg,
        ROUND(MIN(temperature_c_min)::numeric, 2)::double precision AS temperature_c_min,
        ROUND(MAX(temperature_c_max)::numeric, 2)::double precision AS temperature_c_max,
        ROUND(AVG(precipitation_mm_sum)::numeric, 2)::double precision AS precipitation_mm_avg,
        ROUND(MIN(precipitation_mm_sum)::numeric, 2)::double precision AS precipitation_mm_min,
        ROUND(MAX(precipitation_mm_sum)::numeric, 2)::double precision AS precipitation_mm_max,
        ROUND(AVG(relative_humidity_pct_avg)::numeric, 2)::double precision AS relative_humidity_pct_avg,
        ROUND(MIN(relative_humidity_pct_min)::numeric, 2)::double precision AS relative_humidity_pct_min,
        ROUND(MAX(relative_humidity_pct_max)::numeric, 2)::double precision AS relative_humidity_pct_max,
        ROUND(AVG(wind_speed_ms_avg)::numeric, 2)::double precision AS wind_speed_ms_avg,
        ROUND(MIN(wind_speed_ms_min)::numeric, 2)::double precision AS wind_speed_ms_min,
        ROUND(MAX(wind_speed_ms_max)::numeric, 2)::double precision AS wind_speed_ms_max,
        ROUND(AVG(pressure_hpa_avg)::numeric, 2)::double precision AS pressure_hpa_avg,
        ROUND(MIN(pressure_hpa_min)::numeric, 2)::double precision AS pressure_hpa_min,
        ROUND(MAX(pressure_hpa_max)::numeric, 2)::double precision AS pressure_hpa_max,
        ROUND(AVG(solar_radiation_wm2_avg)::numeric, 2)::double precision AS solar_radiation_wm2_avg,
        ROUND(MIN(solar_radiation_wm2_min)::numeric, 2)::double precision AS solar_radiation_wm2_min,
        ROUND(MAX(solar_radiation_wm2_max)::numeric, 2)::double precision AS solar_radiation_wm2_max,
        ROUND(AVG(cloud_cover_okta_avg)::numeric, 2)::double precision AS cloud_cover_okta_avg,
        ROUND(MIN(cloud_cover_okta_min)::numeric, 2)::double precision AS cloud_cover_okta_min,
        ROUND(MAX(cloud_cover_okta_max)::numeric, 2)::double precision AS cloud_cover_okta_max,
        MAX(observation_date)::text AS latest_bucket
      FROM knmi_daily_cagg_tbl
      WHERE bucket >= $1::date
        AND bucket < ($2::date + INTERVAL '1 day')
      GROUP BY station_id
    )
    SELECT
      s.station_id,
      s.station_lat,
      s.station_lon,
      s.temperature_c_avg,
      s.temperature_c_min,
      s.temperature_c_max,
      s.precipitation_mm_avg,
      s.precipitation_mm_min,
      s.precipitation_mm_max,
      s.relative_humidity_pct_avg,
      s.relative_humidity_pct_min,
      s.relative_humidity_pct_max,
      s.wind_speed_ms_avg,
      s.wind_speed_ms_min,
      s.wind_speed_ms_max,
      s.pressure_hpa_avg,
      s.pressure_hpa_min,
      s.pressure_hpa_max,
      s.solar_radiation_wm2_avg,
      s.solar_radiation_wm2_min,
      s.solar_radiation_wm2_max,
      s.cloud_cover_okta_avg,
      s.cloud_cover_okta_min,
      s.cloud_cover_okta_max,
      s.latest_bucket
    FROM station_window s
    ORDER BY s.station_id ASC
  `, [dateFrom, dateTo]);

  return result.rows;
}

async function loadWindVectors(dateFrom: string, dateTo: string) {
  const result = await query<WindVectorRow>(`
    WITH station_day_wind AS (
      SELECT
        station_id,
        station_name,
        ROUND(station_lat_avg::numeric, 6)::double precision AS station_lat,
        ROUND(station_lon_avg::numeric, 6)::double precision AS station_lon,
        observation_date::text AS observed_on,
        ROUND(wind_speed_ms_avg::numeric, 2)::double precision AS wind_speed_ms,
        ROUND(flow_direction_deg_circular_avg::numeric, 2)::double precision AS flow_direction_deg
      FROM knmi_daily_cagg_tbl
      WHERE bucket >= $1::date
        AND bucket < ($2::date + INTERVAL '1 day')
        AND wind_speed_ms_avg IS NOT NULL
        AND flow_direction_deg_circular_avg IS NOT NULL
    )
    SELECT
      w.station_id,
      w.station_name,
      w.station_lat,
      w.station_lon,
      w.observed_on,
      w.wind_speed_ms,
      w.flow_direction_deg,
      ROW_NUMBER() OVER (PARTITION BY w.station_id ORDER BY w.observed_on ASC) AS sample_order,
      COUNT(*) OVER (PARTITION BY w.station_id) AS sample_count
    FROM station_day_wind w
    ORDER BY w.observed_on ASC, w.station_id ASC
  `, [dateFrom, dateTo]);

  return result.rows;
}

async function resolveDroughtMonth(dateFrom: string, dateTo: string) {
  const result = await query<DroughtMonthRow>(`
    WITH drought_candidates AS (
      SELECT
        MAX(observation_month) FILTER (
          WHERE observation_month >= date_trunc('month', $1::date)::date
            AND observation_month <= $2::date
        ) AS in_window_month,
        MAX(observation_month) FILTER (
          WHERE observation_month <= $2::date
        ) AS fallback_month
      FROM cds_drought_tbl
      WHERE ensemble_member = 0
    )
    SELECT COALESCE(in_window_month, fallback_month)::text AS observation_month
    FROM drought_candidates
  `, [dateFrom, dateTo]);

  return result.rows[0]?.observation_month ?? null;
}

async function resolveDefaultWeatherWindow() {
  const [knmiCoverage, droughtCoverage] = await Promise.all([
    query<CoverageWindowRow>(`
      SELECT MAX(bucket)::date::text AS latest_date
      FROM knmi_daily_cagg_tbl
    `),
    query<CoverageWindowRow>(`
      SELECT MAX(observation_month)::text AS latest_date
      FROM cds_drought_tbl
      WHERE ensemble_member = 0
    `),
  ]);

  const anchorDate =
    knmiCoverage.rows[0]?.latest_date ??
    droughtCoverage.rows[0]?.latest_date ??
    defaultWeatherDateRange().dateTo;
  const reference = new Date(`${anchorDate}T00:00:00Z`);

  return defaultWeatherDateRange(
    Number.isNaN(reference.getTime()) ? new Date() : reference,
  );
}

async function loadDroughtAggregate(observationMonth: string) {
  const result = await query<DroughtAggregateRow>(`
    SELECT
      grid_cell_id,
      lat,
      lon,
      spi_3m,
      spei_3m,
      observation_month::text AS observation_month
    FROM cds_drought_tbl
    WHERE ensemble_member = 0
      AND observation_month = $1::date
    ORDER BY grid_cell_id ASC
  `, [observationMonth]);

  return result.rows;
}

function locateMunicipality(
  lng: number,
  lat: number,
  municipalities: MunicipalityBoundary[],
) {
  const locationPoint = point([lng, lat]);
  const candidateMunicipalities = exploreWeatherRouteState.municipalitySpatialIndexCache?.expiresAt
    && exploreWeatherRouteState.municipalitySpatialIndexCache.expiresAt > Date.now()
    ? exploreWeatherRouteState.municipalitySpatialIndexCache.cells.get(municipalitySpatialCellKey(lng, lat)) ?? municipalities
    : municipalities;

  for (const municipality of candidateMunicipalities) {
    if (!pointInBounds(lng, lat, municipality.bbox)) {
      continue;
    }

    if (booleanPointInPolygon(locationPoint, municipality.geometry)) {
      return municipality;
    }
  }

  return null;
}

function resolveStationMunicipalityId(
  stationId: string,
  stationLng: number,
  stationLat: number,
  municipalities: MunicipalityBoundary[],
) {
  const cache = ensureStationMunicipalityCache();
  if (cache.ids.has(stationId)) {
    return cache.ids.get(stationId) ?? null;
  }

  const municipalityId = locateMunicipality(stationLng, stationLat, municipalities)?.municipalityId ?? null;
  cache.ids.set(stationId, municipalityId);
  return municipalityId;
}

function resolveDroughtMunicipalityId(
  gridCellId: string,
  lng: number,
  lat: number,
  municipalities: MunicipalityBoundary[],
) {
  const cache = ensureDroughtMunicipalityCache();
  if (cache.ids.has(gridCellId)) {
    return cache.ids.get(gridCellId) ?? null;
  }

  const municipalityId = locateMunicipality(lng, lat, municipalities)?.municipalityId ?? null;
  cache.ids.set(gridCellId, municipalityId);
  return municipalityId;
}

function averageOrNull(total: number, count: number) {
  if (count === 0) {
    return null;
  }

  return Math.round((total / count) * 100) / 100;
}

function directionOrNull(vectorXTotal: number, vectorYTotal: number, count: number) {
  if (count === 0) {
    return null;
  }

  const direction = ((Math.atan2(vectorXTotal, vectorYTotal) * 180) / Math.PI + 360) % 360;
  return Math.round(direction * 100) / 100;
}

function toMunicipalityPayload(
  municipality: MunicipalityBoundary,
  accumulator: MunicipalityAccumulator,
): ExploreWeatherMunicipality {
  const knmi = {
    temperatureC: buildStatSummary(accumulator.temperature),
    precipitationMm: buildStatSummary(accumulator.precipitation),
    relativeHumidityPct: buildStatSummary(accumulator.relativeHumidity),
    windSpeedMs: buildStatSummary(accumulator.windSpeed),
    pressureHpa: buildStatSummary(accumulator.pressure),
    solarRadiationWm2: buildStatSummary(accumulator.solarRadiation),
    cloudCoverOkta: buildStatSummary(accumulator.cloudCover),
  } satisfies ExploreWeatherKnmiStats;

  return {
    municipalityId: municipality.municipalityId,
    name: municipality.name,
    geometry: municipality.geometry,
    centroid: municipality.centroid,
    coverage: {
      stationCount: accumulator.stationCount,
      droughtCellCount: accumulator.droughtCellCount,
    },
    metrics: {
      temperature: knmi.temperatureC.avg,
      precipitation: knmi.precipitationMm.avg,
      spi3: averageOrNull(accumulator.spi3Total, accumulator.spi3Samples),
      spei3: averageOrNull(accumulator.spei3Total, accumulator.spei3Samples),
    },
    knmi,
    wind: {
      speedMs: knmi.windSpeedMs.avg,
      flowDirectionDeg: directionOrNull(
        accumulator.windVectorXTotal,
        accumulator.windVectorYTotal,
        accumulator.windVectorSamples,
      ),
    },
    latestKnmiBucket: accumulator.latestKnmiBucket,
    droughtObservationMonth: accumulator.droughtObservationMonth,
  };
}

const handleGet = async (request: NextRequest) => {
  const { searchParams } = request.nextUrl;
  const rawDateFrom = searchParams.get('dateFrom');
  const rawDateTo = searchParams.get('dateTo');
  const { dateFrom, dateTo } = rawDateFrom || rawDateTo
    ? resolveWeatherDateRange(rawDateFrom, rawDateTo)
    : await resolveDefaultWeatherWindow();
  const cacheKey = `${WEATHER_CACHE_VERSION}:${dateFrom}:${dateTo}`;
  const cached = weatherCache.get(cacheKey);

  if (cached && cached.expiresAt > Date.now()) {
    return NextResponse.json(cached.payload, { headers: RESPONSE_HEADERS });
  }

  const [municipalities, knmiRows, windRows, droughtObservationMonth] = await Promise.all([
    loadMunicipalities(),
    loadKnmiAggregate(dateFrom, dateTo),
    loadWindVectors(dateFrom, dateTo),
    resolveDroughtMonth(dateFrom, dateTo),
  ]);

  const droughtRows = droughtObservationMonth ? await loadDroughtAggregate(droughtObservationMonth) : [];
  const accumulators = new Map<string, MunicipalityAccumulator>(
    municipalities.map((municipality) => [municipality.municipalityId, createAccumulator()]),
  );
  const knmiStationSummaries: KnmiStationSummary[] = [];
  const windVectors: ExploreWeatherWindVector[] = [];
  let stationsUsed = 0;
  let droughtCellsUsed = 0;
  let knmiLatestBucket: string | null = null;

  for (const row of knmiRows) {
    const stationLat = numberOrNull(row.station_lat);
    const stationLon = numberOrNull(row.station_lon);
    if (stationLat === null || stationLon === null) {
      continue;
    }

    const stationSummary = {
      stationId: row.station_id,
      lat: stationLat,
      lon: stationLon,
      latestBucket: row.latest_bucket,
      temperatureAvg: numberOrNull(row.temperature_c_avg),
      temperatureMin: numberOrNull(row.temperature_c_min),
      temperatureMax: numberOrNull(row.temperature_c_max),
      precipitationAvg: numberOrNull(row.precipitation_mm_avg),
      precipitationMin: numberOrNull(row.precipitation_mm_min),
      precipitationMax: numberOrNull(row.precipitation_mm_max),
      relativeHumidityAvg: numberOrNull(row.relative_humidity_pct_avg),
      relativeHumidityMin: numberOrNull(row.relative_humidity_pct_min),
      relativeHumidityMax: numberOrNull(row.relative_humidity_pct_max),
      windSpeedAvg: numberOrNull(row.wind_speed_ms_avg),
      windSpeedMin: numberOrNull(row.wind_speed_ms_min),
      windSpeedMax: numberOrNull(row.wind_speed_ms_max),
      pressureAvg: numberOrNull(row.pressure_hpa_avg),
      pressureMin: numberOrNull(row.pressure_hpa_min),
      pressureMax: numberOrNull(row.pressure_hpa_max),
      solarRadiationAvg: numberOrNull(row.solar_radiation_wm2_avg),
      solarRadiationMin: numberOrNull(row.solar_radiation_wm2_min),
      solarRadiationMax: numberOrNull(row.solar_radiation_wm2_max),
      cloudCoverAvg: numberOrNull(row.cloud_cover_okta_avg),
      cloudCoverMin: numberOrNull(row.cloud_cover_okta_min),
      cloudCoverMax: numberOrNull(row.cloud_cover_okta_max),
    } satisfies KnmiStationSummary;

    knmiStationSummaries.push(stationSummary);

    const municipalityId = resolveStationMunicipalityId(row.station_id, stationLon, stationLat, municipalities);
    if (!municipalityId) {
      if (row.latest_bucket && (!knmiLatestBucket || row.latest_bucket > knmiLatestBucket)) {
        knmiLatestBucket = row.latest_bucket;
      }
      continue;
    }

    stationsUsed += 1;
    const accumulator = accumulators.get(municipalityId);
    if (!accumulator) {
      continue;
    }

    accumulator.stationCount += 1;

    applyKnmiStationSummary(accumulator, stationSummary);

    if (row.latest_bucket && (!accumulator.latestKnmiBucket || row.latest_bucket > accumulator.latestKnmiBucket)) {
      accumulator.latestKnmiBucket = row.latest_bucket;
    }
    if (row.latest_bucket && (!knmiLatestBucket || row.latest_bucket > knmiLatestBucket)) {
      knmiLatestBucket = row.latest_bucket;
    }
  }


  for (const municipality of municipalities) {
    const accumulator = accumulators.get(municipality.municipalityId);
    if (!accumulator || accumulatorHasKnmiSummary(accumulator)) {
      continue;
    }

    const nearestSummary = nearestKnmiStationSummary(municipality, knmiStationSummaries);
    if (!nearestSummary) {
      continue;
    }

    applyKnmiStationSummary(accumulator, nearestSummary);
  }
  for (const row of windRows) {
    const stationLat = numberOrNull(row.station_lat);
    const stationLon = numberOrNull(row.station_lon);
    const windSpeed = numberOrNull(row.wind_speed_ms);
    const flowDirectionDeg = numberOrNull(row.flow_direction_deg);
    const sampleOrder = numberOrNull(row.sample_order);
    const sampleCount = numberOrNull(row.sample_count);
    if (
      stationLat === null ||
      stationLon === null ||
      windSpeed === null ||
      flowDirectionDeg === null ||
      sampleOrder === null ||
      sampleCount === null
    ) {
      continue;
    }

    const municipalityId = resolveStationMunicipalityId(row.station_id, stationLon, stationLat, municipalities);
    const accumulator = municipalityId ? accumulators.get(municipalityId) : null;

    if (accumulator) {
      const directionRadians = (flowDirectionDeg * Math.PI) / 180;
      accumulator.windVectorXTotal += Math.sin(directionRadians) * windSpeed;
      accumulator.windVectorYTotal += Math.cos(directionRadians) * windSpeed;
      accumulator.windVectorSamples += 1;
    }

    if (municipalityId) {
      windVectors.push({
        municipalityId,
        stationId: row.station_id,
        stationName: row.station_name,
        location: {
          lat: stationLat,
          lng: stationLon,
        },
        observedOn: row.observed_on,
        speedMs: windSpeed,
        flowDirectionDeg,
        sampleOrder,
        sampleCount,
      });
    }
  }

  for (const row of droughtRows) {
    const lat = numberOrNull(row.lat);
    const lon = numberOrNull(row.lon);
    if (lat === null || lon === null) {
      continue;
    }

    const municipalityId = resolveDroughtMunicipalityId(row.grid_cell_id, lon, lat, municipalities);
    if (!municipalityId) {
      continue;
    }

    droughtCellsUsed += 1;
    const accumulator = accumulators.get(municipalityId);
    if (!accumulator) {
      continue;
    }

    accumulator.droughtCellCount += 1;

    const spi3 = numberOrNull(row.spi_3m);
    if (spi3 !== null) {
      accumulator.spi3Total += spi3;
      accumulator.spi3Samples += 1;
    }

    const spei3 = numberOrNull(row.spei_3m);
    if (spei3 !== null) {
      accumulator.spei3Total += spei3;
      accumulator.spei3Samples += 1;
    }

    if (
      row.observation_month &&
      (!accumulator.droughtObservationMonth || row.observation_month > accumulator.droughtObservationMonth)
    ) {
      accumulator.droughtObservationMonth = row.observation_month;
    }
  }

  const municipalityPayload = municipalities
    .map((municipality) => {
      const accumulator = accumulators.get(municipality.municipalityId) ?? createAccumulator();
      return toMunicipalityPayload(municipality, accumulator);
    })
    .sort((left, right) => left.name.localeCompare(right.name));

  const municipalitiesWithAnyData = municipalityPayload.filter((municipality) => {
    return Object.values(municipality.metrics).some((value) => value !== null);
  }).length;

  const payload: ExploreWeatherPayload = {
    generatedAt: new Date().toISOString(),
    dateFrom,
    dateTo,
    sourceFreshness: {
      knmiLatestBucket,
      droughtObservationMonth,
    },
    coverage: {
      municipalityCount: municipalityPayload.length,
      municipalitiesWithAnyData,
      stationsUsed,
      droughtCellsUsed,
    },
    municipalities: municipalityPayload,
    windVectors,
  };

  weatherCache.set(cacheKey, {
    expiresAt: Date.now() + CACHE_TTL_MS,
    payload,
  });

  return NextResponse.json(payload, { headers: RESPONSE_HEADERS });
};

export const GET = withMeasuredApiRoute(handleGet);