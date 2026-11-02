export const WEATHER_METRICS = [
  {
    value: 'temperature',
    label: 'Temperature',
    detail: 'KNMI mean temp',
    unit: 'degC',
  },
  {
    value: 'precipitation',
    label: 'Precipitation',
    detail: 'KNMI rain over window',
    unit: 'mm',
  },
  {
    value: 'spi3',
    label: 'SPI-3',
    detail: 'CDS 3-month precipitation index',
    unit: 'index',
  },
  {
    value: 'spei3',
    label: 'SPEI-3',
    detail: 'CDS 3-month evapotranspiration index',
    unit: 'index',
  },
] as const;

export type WeatherMetric = (typeof WEATHER_METRICS)[number]['value'];

export type ExploreWeatherMetricValues = {
  temperature: number | null;
  precipitation: number | null;
  spi3: number | null;
  spei3: number | null;
};

export type ExploreWeatherStatSummary = {
  avg: number | null;
  min: number | null;
  max: number | null;
};

export type ExploreWeatherKnmiStats = {
  temperatureC: ExploreWeatherStatSummary;
  precipitationMm: ExploreWeatherStatSummary;
  relativeHumidityPct: ExploreWeatherStatSummary;
  windSpeedMs: ExploreWeatherStatSummary;
  pressureHpa: ExploreWeatherStatSummary;
  solarRadiationWm2: ExploreWeatherStatSummary;
  cloudCoverOkta: ExploreWeatherStatSummary;
};

export type ExploreWeatherMunicipality = {
  municipalityId: string;
  name: string;
  geometry: GeoJSON.Geometry;
  centroid: { lat: number; lng: number } | null;
  coverage: {
    stationCount: number;
    droughtCellCount: number;
  };
  metrics: ExploreWeatherMetricValues;
  knmi: ExploreWeatherKnmiStats;
  wind: {
    speedMs: number | null;
    flowDirectionDeg: number | null;
  };
  latestKnmiBucket: string | null;
  droughtObservationMonth: string | null;
};

export type ExploreWeatherWindVector = {
  municipalityId: string;
  stationId: string;
  stationName: string | null;
  location: {
    lat: number;
    lng: number;
  };
  observedOn: string;
  speedMs: number;
  flowDirectionDeg: number;
  sampleOrder: number;
  sampleCount: number;
};

export type ExploreWeatherPayload = {
  generatedAt: string;
  dateFrom: string;
  dateTo: string;
  sourceFreshness: {
    knmiLatestBucket: string | null;
    droughtObservationMonth: string | null;
  };
  coverage: {
    municipalityCount: number;
    municipalitiesWithAnyData: number;
    stationsUsed: number;
    droughtCellsUsed: number;
  };
  municipalities: ExploreWeatherMunicipality[];
  windVectors: ExploreWeatherWindVector[];
};

export const WEATHER_OVERLAY_OPTIONS = [
  { value: 'wind', label: 'Wind' },
  { value: 'temperature', label: 'Temperature' },
  { value: 'cloud', label: 'Cloud' },
  { value: 'precipitation', label: 'Precipitation' },
] as const;

export type WeatherOverlay = (typeof WEATHER_OVERLAY_OPTIONS)[number]['value'];

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export const DEFAULT_WEATHER_WINDOW_DAYS = 1;

export function resolveWeatherOverlay(
  raw: string | null | undefined,
): WeatherOverlay | null {
  if (!raw) {
    return 'wind';
  }

  if (raw === 'none') {
    return null;
  }

  return WEATHER_OVERLAY_OPTIONS.some((overlay) => overlay.value === raw)
    ? (raw as WeatherOverlay)
    : 'wind';
}

export function isoDate(value: Date) {
  return value.toISOString().slice(0, 10);
}

export function buildTrailingWeatherDateRange(reference: Date, days: number) {
  const normalizedDays = Math.max(1, Math.floor(days));
  const dateTo = new Date(reference);
  const dateFrom = new Date(reference);
  dateFrom.setUTCDate(dateFrom.getUTCDate() - (normalizedDays - 1));

  return {
    dateFrom: isoDate(dateFrom),
    dateTo: isoDate(dateTo),
  };
}

export function defaultWeatherDateRange(reference = new Date()) {
  return buildTrailingWeatherDateRange(reference, DEFAULT_WEATHER_WINDOW_DAYS);
}

export function resolveWeatherSelectedDate(
  rawDate: string | null | undefined,
  rawDateFrom: string | null | undefined,
  rawDateTo: string | null | undefined,
  reference = new Date(),
) {
  const normalizedDate = normalizeWeatherDate(rawDate);
  if (normalizedDate) {
    return normalizedDate;
  }

  if (rawDateFrom || rawDateTo) {
    return resolveWeatherDateRange(rawDateFrom, rawDateTo, reference).dateTo;
  }

  return defaultWeatherDateRange(reference).dateTo;
}

export function normalizeWeatherDate(raw: string | null | undefined) {
  if (!raw || !ISO_DATE_RE.test(raw)) {
    return null;
  }

  const parsed = new Date(`${raw}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }

  return raw;
}

export function normalizeWeatherMetric(raw: string | null | undefined): WeatherMetric {
  return WEATHER_METRICS.some((metric) => metric.value === raw)
    ? (raw as WeatherMetric)
    : 'temperature';
}

export function resolveWeatherDateRange(
  rawDateFrom: string | null | undefined,
  rawDateTo: string | null | undefined,
  reference = new Date(),
) {
  const defaults = defaultWeatherDateRange(reference);
  const dateFrom = normalizeWeatherDate(rawDateFrom) ?? defaults.dateFrom;
  const dateTo = normalizeWeatherDate(rawDateTo) ?? defaults.dateTo;

  if (dateFrom <= dateTo) {
    return { dateFrom, dateTo };
  }

  return {
    dateFrom: dateTo,
    dateTo: dateFrom,
  };
}

export function metricValueForMunicipality(
  municipality: ExploreWeatherMunicipality,
  metric: WeatherMetric,
) {
  return municipality.metrics[metric];
}

export function formatMetricValue(metric: WeatherMetric, value: number | null) {
  if (value === null || !Number.isFinite(value)) {
    return 'No data';
  }

  switch (metric) {
    case 'temperature':
      return `${value.toFixed(1)} degC`;
    case 'precipitation':
      return `${value.toFixed(1)} mm`;
    case 'spi3':
    case 'spei3':
      return value.toFixed(2);
    default:
      return value.toFixed(2);
  }
}

export function formatMetricCompactValue(metric: WeatherMetric, value: number | null) {
  if (value === null || !Number.isFinite(value)) {
    return 'No data';
  }

  switch (metric) {
    case 'temperature':
      return `${value.toFixed(1)} degC`;
    case 'precipitation':
      return `${value.toFixed(0)} mm`;
    case 'spi3':
    case 'spei3':
      return value.toFixed(2);
    default:
      return value.toFixed(2);
  }
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

export function buildMetricRange(values: number[]) {
  if (values.length === 0) {
    return null;
  }

  return {
    min: Math.min(...values),
    max: Math.max(...values),
  };
}

export function linearMetricSeverity(
  value: number | null,
  range: { min: number; max: number } | null,
) {
  if (value === null || !Number.isFinite(value)) {
    return null;
  }

  if (!range || range.max <= range.min) {
    return 0.5;
  }

  return clamp((value - range.min) / (range.max - range.min), 0, 1);
}

export function droughtMetricSeverity(value: number | null) {
  if (value === null || !Number.isFinite(value)) {
    return null;
  }

  return clamp((-value) / 2.5, 0, 1);
}

export function droughtSignalForMunicipality(municipality: ExploreWeatherMunicipality) {
  const signals = [
    droughtMetricSeverity(municipality.metrics.spi3),
    droughtMetricSeverity(municipality.metrics.spei3),
  ].filter((value): value is number => value !== null);

  if (signals.length === 0) {
    return null;
  }

  return signals.reduce((total, value) => total + value, 0) / signals.length;
}

export function weatherSeverity(
  metric: WeatherMetric,
  value: number | null,
  range: { min: number; max: number } | null,
) {
  if (value === null || !Number.isFinite(value)) {
    return null;
  }

  if (metric === 'spi3' || metric === 'spei3') {
    return clamp((-value) / 2.5, 0, 1);
  }

  if (!range || range.max <= range.min) {
    return 0.5;
  }

  return clamp((value - range.min) / (range.max - range.min), 0, 1);
}