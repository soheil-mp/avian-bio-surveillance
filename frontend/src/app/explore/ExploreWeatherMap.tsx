'use client';

import booleanPointInPolygon from '@turf/boolean-point-in-polygon';
import { point } from '@turf/helpers';
import {
  DEFAULT_ZOOM,
  NETHERLANDS_BOUNDS,
  NETHERLANDS_CENTER,
} from '@/config/map';
import type { LayerSpecification, StyleSpecification } from 'maplibre-gl';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import {
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
} from 'react';
import MapView, { Layer, Marker, Source, type MapRef } from 'react-map-gl/maplibre';

import {
  buildMetricRange,
  droughtSignalForMunicipality,
  linearMetricSeverity,
  type ExploreWeatherMunicipality,
  type ExploreWeatherWindVector,
  resolveWeatherOverlay,
  type WeatherOverlay,
} from './weather';

type ExploreWeatherMapProps = {
  municipalities: ExploreWeatherMunicipality[];
  windVectors: ExploreWeatherWindVector[];
};

type WeatherMapFeatureProperties = {
  municipalityId: string;
  name: string;
  droughtSeverity: number;
  hasDrought: number;
  temperatureC: number;
  temperatureSeverity: number;
  hasTemperature: number;
  cloudCoverOkta: number;
  cloudSeverity: number;
  hasCloudCover: number;
};

type WindFlashFeatureProperties = {
  municipalityId: string;
  stationId: string;
  stationName: string | null;
  observedOn: string;
  windSpeedMs: number;
  flowDirectionDeg: number;
};

type WindArrowOverlay = {
  feature: GeoJSON.Feature<GeoJSON.MultiLineString, WindFlashFeatureProperties>;
  labelLng: number;
  labelLat: number;
  speedLabel: string;
};

type TemperatureLabelOverlay = {
  municipalityId: string;
  lng: number;
  lat: number;
  temperatureLabel: string;
  temperatureSeverity: number;
};

type CloudLabelOverlay = {
  municipalityId: string;
  lng: number;
  lat: number;
  cloudLabel: string;
  cloudDetailLabel: string;
  cloudSeverity: number;
};

type PrecipitationLabelOverlay = {
  municipalityId: string;
  lng: number;
  lat: number;
  precipitationLabel: string;
  precipitationSeverity: number;
};

type ProvinceOverlaySummary = {
  provinceId: string;
  name: string;
  lng: number;
  lat: number;
  areaScore: number;
  temperatureAvg: number | null;
  cloudCoverAvg: number | null;
  precipitationAvg: number | null;
};

type ProvinceBucket = {
  provinceId: string;
  name: string;
  geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon;
  fallbackCenter: { lng: number; lat: number };
  areaScore: number;
  memberCentroids: Array<{ lng: number; lat: number }>;
  temperatureValues: number[];
  cloudValues: number[];
  precipitationValues: number[];
};

type SelectedPopupPosition = {
  lng: number;
  lat: number;
};

type ProvinceFeatureCollection = GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  GeoJSON.GeoJsonProperties
>;

type MapMouseEvent = Parameters<
  NonNullable<ComponentProps<typeof MapView>['onMouseMove']>
>[0];

type MapClickEvent = Parameters<
  NonNullable<ComponentProps<typeof MapView>['onClick']>
>[0];

const WEATHER_MAP_STYLE: StyleSpecification = {
  version: 8,
  name: 'Explore Weather Dark',
  sources: {},
  layers: [
    {
      id: 'explore-weather-background',
      type: 'background',
      paint: {
        'background-color': '#171717',
      },
    },
  ],
};

const WEATHER_INITIAL_ZOOM = DEFAULT_ZOOM;
const WEATHER_MIN_ZOOM = 3.9;
const WEATHER_MAX_ZOOM = 10.5;
const WEATHER_FIT_MAX_ZOOM = 5.45;
const WEATHER_CAMERA_BUFFER_LNG = 0.45;
const WEATHER_CAMERA_BUFFER_LAT = 0.32;
const WEATHER_PAN_BUFFER_LNG = 0.45;
const WEATHER_PAN_BUFFER_LAT = 0.32;

const EMPTY_PROVINCES: ProvinceFeatureCollection = {
  type: 'FeatureCollection',
  features: [],
};

const EMPTY_MUNICIPALITIES: GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  WeatherMapFeatureProperties
> = {
  type: 'FeatureCollection',
  features: [],
};

const EMPTY_WIND_FLASHES: GeoJSON.FeatureCollection<
  GeoJSON.MultiLineString,
  WindFlashFeatureProperties
> = {
  type: 'FeatureCollection',
  features: [],
};

const municipalityBaseFillLayer: LayerSpecification = {
  id: 'explore-weather-municipality-base-fill',
  type: 'fill',
  source: 'explore-weather-municipalities',
  paint: {
    'fill-color': 'rgba(36,36,36,1)',
    'fill-opacity': 0.78,
  },
};

const TEMPERATURE_COLOR_STOPS = [
  { stop: 0, rgb: [56, 74, 88] as const },
  { stop: 0.24, rgb: [75, 96, 105] as const },
  { stop: 0.5, rgb: [98, 117, 101] as const },
  { stop: 0.76, rgb: [137, 132, 90] as const },
  { stop: 1, rgb: [178, 151, 96] as const },
] as const;

const CLOUD_COVER_RANGE = { min: 0, max: 8 } as const;
const MAX_TEMPERATURE_LABELS = 88;
const TEMPERATURE_LABEL_MIN_DISTANCE = 0.125;
const MAX_CLOUD_LABELS = 64;
const CLOUD_LABEL_MIN_DISTANCE = 0.16;
const ACTIVE_OVERLAY_BACKGROUND = 'rgba(74, 222, 128, 0.16)';
const ACTIVE_OVERLAY_BORDER = 'rgba(74, 222, 128, 0.36)';
const ACTIVE_OVERLAY_FOREGROUND = 'rgba(187,247,208,0.96)';
const INACTIVE_OVERLAY_BACKGROUND = 'rgba(18,18,18,0.88)';
const INACTIVE_OVERLAY_BORDER = 'rgba(255,255,255,0.10)';
const INACTIVE_OVERLAY_FOREGROUND = 'rgba(255,255,255,0.62)';

const temperatureAreaFillLayer: LayerSpecification = {
  id: 'explore-weather-temperature-fill',
  type: 'fill',
  source: 'explore-weather-municipalities',
  filter: ['==', ['get', 'hasTemperature'], 1],
  paint: {
    'fill-color': [
      'interpolate',
      ['linear'],
      ['get', 'temperatureSeverity'],
      0,
      'rgba(56,74,88,0.97)',
      0.24,
      'rgba(75,96,105,0.97)',
      0.5,
      'rgba(98,117,101,0.98)',
      0.76,
      'rgba(137,132,90,0.98)',
      1,
      'rgba(178,151,96,0.99)',
    ],
    'fill-opacity': 0,
  },
};

const temperatureAreaGlowLayer: LayerSpecification = {
  id: 'explore-weather-temperature-glow',
  type: 'line',
  source: 'explore-weather-municipalities',
  filter: ['==', ['get', 'hasTemperature'], 1],
  paint: {
    'line-color': [
      'interpolate',
      ['linear'],
      ['get', 'temperatureSeverity'],
      0,
      'rgba(93,111,124,0.14)',
      0.24,
      'rgba(111,129,140,0.16)',
      0.5,
      'rgba(136,151,133,0.18)',
      0.76,
      'rgba(172,163,117,0.2)',
      1,
      'rgba(205,183,118,0.24)',
    ],
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      5,
      0.35,
      7.5,
      0.48,
      10,
      0.62,
    ],
    'line-opacity': 0,
    'line-blur': 0.35,
  },
};

const temperatureMunicipalityLineLayer: LayerSpecification = {
  id: 'explore-weather-temperature-line',
  type: 'line',
  source: 'explore-weather-municipalities',
  paint: {
    'line-color': 'rgba(222, 230, 217, 0.10)',
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      5,
      0.3,
      7.5,
      0.45,
      10,
      0.62,
    ],
    'line-opacity': 0,
  },
};

const cloudAreaFillLayer: LayerSpecification = {
  id: 'explore-weather-cloud-fill',
  type: 'fill',
  source: 'explore-weather-municipalities',
  filter: ['==', ['get', 'hasCloudCover'], 1],
  paint: {
    'fill-color': [
      'interpolate',
      ['linear'],
      ['get', 'cloudSeverity'],
      0,
      'rgba(38,46,52,0.94)',
      0.32,
      'rgba(58,69,75,0.95)',
      0.62,
      'rgba(101,113,120,0.96)',
      1,
      'rgba(178,190,198,0.98)',
    ],
    'fill-opacity': 0,
  },
};

const cloudAreaGlowLayer: LayerSpecification = {
  id: 'explore-weather-cloud-glow',
  type: 'line',
  source: 'explore-weather-municipalities',
  filter: ['==', ['get', 'hasCloudCover'], 1],
  paint: {
    'line-color': [
      'interpolate',
      ['linear'],
      ['get', 'cloudSeverity'],
      0,
      'rgba(110,122,128,0.10)',
      0.32,
      'rgba(136,148,154,0.14)',
      0.62,
      'rgba(172,183,190,0.18)',
      1,
      'rgba(219,228,233,0.24)',
    ],
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      5,
      0.35,
      7.5,
      0.5,
      10,
      0.7,
    ],
    'line-opacity': 0,
    'line-blur': 0.5,
  },
};

const cloudMunicipalityLineLayer: LayerSpecification = {
  id: 'explore-weather-cloud-line',
  type: 'line',
  source: 'explore-weather-municipalities',
  paint: {
    'line-color': 'rgba(234, 240, 244, 0.16)',
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      5,
      0.32,
      7.5,
      0.48,
      10,
      0.66,
    ],
    'line-opacity': 0,
  },
};

const municipalityDataFillLayer: LayerSpecification = {
  id: 'explore-weather-municipality-data-fill',
  type: 'fill',
  source: 'explore-weather-municipalities',
  filter: ['==', ['get', 'hasDrought'], 1],
  paint: {
    'fill-color': [
      'interpolate',
      ['linear'],
      ['get', 'droughtSeverity'],
      0,
      'rgba(56,56,56,0.84)',
      0.45,
      'rgba(150,118,62,0.94)',
      1,
      'rgba(138,52,58,0.98)',
    ],
    'fill-opacity': [
      'interpolate',
      ['linear'],
      ['get', 'droughtSeverity'],
      0,
      0.1,
      0.45,
      0.24,
      1,
      0.42,
    ],
  },
};

const municipalityLineLayer: LayerSpecification = {
  id: 'explore-weather-municipality-line',
  type: 'line',
  source: 'explore-weather-municipalities',
  paint: {
    'line-color': 'rgba(116,116,116,0.62)',
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      5,
      0.45,
      7.5,
      0.7,
      10,
      1.05,
    ],
    'line-opacity': 0.82,
  },
};

const windFlashGlowLayer: LayerSpecification = {
  id: 'explore-weather-wind-glow',
  type: 'line',
  source: 'explore-weather-wind-flashes',
  paint: {
    'line-color': 'rgba(74, 222, 128, 0.28)',
    'line-width': [
      'interpolate',
      ['linear'],
      ['get', 'windSpeedMs'],
      0,
      1.6,
      12,
      4.2,
    ],
    'line-blur': 2.8,
    'line-opacity': 0.52,
  },
  layout: {
    'line-cap': 'round',
    'line-join': 'round',
  },
};

const windFlashStrokeLayer: LayerSpecification = {
  id: 'explore-weather-wind-stroke',
  type: 'line',
  source: 'explore-weather-wind-flashes',
  paint: {
    'line-color': 'rgba(74, 222, 128, 0.96)',
    'line-width': [
      'interpolate',
      ['linear'],
      ['get', 'windSpeedMs'],
      0,
      0.8,
      12,
      2,
    ],
    'line-opacity': 0.95,
  },
  layout: {
    'line-cap': 'round',
    'line-join': 'round',
  },
};

const provinceLineLayer: LayerSpecification = {
  id: 'explore-weather-province-line',
  type: 'line',
  source: 'explore-weather-provinces',
  paint: {
    'line-color': 'rgba(202,202,202,0.58)',
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      5,
      0.85,
      7.5,
      1.15,
      10,
      1.4,
    ],
    'line-opacity': 0.7,
  },
};

const municipalityHitboxLayer: LayerSpecification = {
  id: 'explore-weather-municipality-hitbox',
  type: 'fill',
  source: 'explore-weather-municipalities',
  paint: {
    'fill-color': 'rgba(255,255,255,1)',
    'fill-opacity': 0.001,
  },
};

const hoveredFillLayer: LayerSpecification = {
  id: 'explore-weather-hovered-fill',
  type: 'fill',
  source: 'explore-weather-hovered',
  paint: {
    'fill-color': 'rgba(255,255,255,0.08)',
    'fill-opacity': 0.08,
  },
};

const hoveredLineLayer: LayerSpecification = {
  id: 'explore-weather-hovered-line',
  type: 'line',
  source: 'explore-weather-hovered',
  paint: {
    'line-color': 'rgba(220,220,220,0.94)',
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      5,
      1.05,
      7.5,
      1.35,
      10,
      1.8,
    ],
    'line-opacity': 0.92,
  },
};

const selectedFillLayer: LayerSpecification = {
  id: 'explore-weather-selected-fill',
  type: 'fill',
  source: 'explore-weather-selected',
  paint: {
    'fill-color': 'rgba(255,255,255,0.1)',
    'fill-opacity': 0.06,
  },
};

const selectedLineLayer: LayerSpecification = {
  id: 'explore-weather-selected-line',
  type: 'line',
  source: 'explore-weather-selected',
  paint: {
    'line-color': 'rgba(255,255,255,0.96)',
    'line-width': [
      'interpolate',
      ['linear'],
      ['zoom'],
      5,
      1.45,
      7.5,
      1.95,
      10,
      2.4,
    ],
    'line-opacity': 1,
  },
};

const hoveredMunicipalityCollection = (
  municipalities: Array<GeoJSON.Feature<GeoJSON.Geometry, WeatherMapFeatureProperties>>,
  municipalityId: string | null,
) => {
  if (!municipalityId) {
    return EMPTY_MUNICIPALITIES;
  }

  const feature = municipalities.find((candidate) => candidate.properties.municipalityId === municipalityId);
  if (!feature) {
    return EMPTY_MUNICIPALITIES;
  }

  return {
    type: 'FeatureCollection',
    features: [feature],
  } satisfies GeoJSON.FeatureCollection<GeoJSON.Geometry, WeatherMapFeatureProperties>;
};

function formatWindSpeedLabel(speedMs: number) {
  const rounded = speedMs >= 10 ? speedMs.toFixed(0) : speedMs.toFixed(1);
  return `${rounded} m/s`;
}

function formatTemperatureLabel(temperatureC: number) {
  return `${temperatureC.toFixed(1)}°`;
}

function formatCloudCoverLabel(cloudCoverOkta: number) {
  return `${cloudCoverOkta.toFixed(1)}/8 okta`;
}

function formatCloudCoverPercentLabel(cloudCoverOkta: number) {
  return `${Math.round((cloudCoverOkta / 8) * 100)}%`;
}

function formatPrecipitationLabel(precipitationMm: number) {
  return `${precipitationMm.toFixed(1)} mm`;
}

function isPolygonalGeometry(geometry: GeoJSON.Geometry): geometry is GeoJSON.Polygon | GeoJSON.MultiPolygon {
  return geometry.type === 'Polygon' || geometry.type === 'MultiPolygon';
}

function extendBounds(
  bounds: readonly [number, number, number, number],
  lngPadding: number,
  latPadding: number,
) {
  return [
    bounds[0] - lngPadding,
    bounds[1] - latPadding,
    bounds[2] + lngPadding,
    bounds[3] + latPadding,
  ] as [number, number, number, number];
}

function writeWeatherOverlay(
  params: URLSearchParams,
  nextOverlay: WeatherOverlay | null,
) {
  if (nextOverlay === null) {
    params.set('weatherOverlay', 'none');
    return;
  }

  if (nextOverlay === 'wind') {
    params.delete('weatherOverlay');
    return;
  }

  params.set('weatherOverlay', nextOverlay);
}

function geometryAreaScore(geometry: GeoJSON.Geometry) {
  let minLng = Number.POSITIVE_INFINITY;
  let minLat = Number.POSITIVE_INFINITY;
  let maxLng = Number.NEGATIVE_INFINITY;
  let maxLat = Number.NEGATIVE_INFINITY;

  const visitCoordinates = (coordinates: unknown) => {
    if (!Array.isArray(coordinates) || coordinates.length === 0) {
      return;
    }

    if (
      coordinates.length >= 2
      && typeof coordinates[0] === 'number'
      && typeof coordinates[1] === 'number'
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
    !Number.isFinite(minLng)
    || !Number.isFinite(minLat)
    || !Number.isFinite(maxLng)
    || !Number.isFinite(maxLat)
  ) {
    return 0;
  }

  const latitudeMidpointRadians = ((minLat + maxLat) / 2) * (Math.PI / 180);
  const longitudeScale = Math.max(Math.cos(latitudeMidpointRadians), 0.25);

  return Math.max((maxLng - minLng) * longitudeScale, 0) * Math.max(maxLat - minLat, 0);
}

function geometryMidpoint(geometry: GeoJSON.Geometry) {
  let minLng = Number.POSITIVE_INFINITY;
  let minLat = Number.POSITIVE_INFINITY;
  let maxLng = Number.NEGATIVE_INFINITY;
  let maxLat = Number.NEGATIVE_INFINITY;

  const visitCoordinates = (coordinates: unknown) => {
    if (!Array.isArray(coordinates) || coordinates.length === 0) {
      return;
    }

    if (
      coordinates.length >= 2
      && typeof coordinates[0] === 'number'
      && typeof coordinates[1] === 'number'
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
    !Number.isFinite(minLng)
    || !Number.isFinite(minLat)
    || !Number.isFinite(maxLng)
    || !Number.isFinite(maxLat)
  ) {
    return null;
  }

  return {
    lng: (minLng + maxLng) / 2,
    lat: (minLat + maxLat) / 2,
  };
}

function squaredDistanceBetweenCoordinates(
  left: { lng: number; lat: number },
  right: { lng: number; lat: number },
) {
  const latitudeMidpointRadians = ((left.lat + right.lat) / 2) * (Math.PI / 180);
  const longitudeScale = Math.max(Math.cos(latitudeMidpointRadians), 0.25);
  const deltaLat = left.lat - right.lat;
  const deltaLng = (left.lng - right.lng) * longitudeScale;

  return (deltaLat * deltaLat) + (deltaLng * deltaLng);
}

function selectSpacedOverlays<T extends { lng: number; lat: number }>(
  overlays: T[],
  minDistance: number,
  maxCount: number,
) {
  const selected: T[] = [];
  const minDistanceSquared = minDistance * minDistance;

  for (const overlay of overlays) {
    const isTooClose = selected.some((candidate) => {
      return squaredDistanceBetweenCoordinates(
        { lng: candidate.lng, lat: candidate.lat },
        { lng: overlay.lng, lat: overlay.lat },
      ) < minDistanceSquared;
    });

    if (isTooClose) {
      continue;
    }

    selected.push(overlay);
    if (selected.length >= maxCount) {
      break;
    }
  }

  return selected;
}

function averageCoordinate(coordinates: Array<{ lng: number; lat: number }>) {
  if (coordinates.length === 0) {
    return null;
  }

  const totals = coordinates.reduce(
    (accumulator, coordinate) => ({
      lng: accumulator.lng + coordinate.lng,
      lat: accumulator.lat + coordinate.lat,
    }),
    { lng: 0, lat: 0 },
  );

  return {
    lng: totals.lng / coordinates.length,
    lat: totals.lat / coordinates.length,
  };
}

function averageValues(values: number[]) {
  if (values.length === 0) {
    return null;
  }

  return values.reduce((total, value) => total + value, 0) / values.length;
}

function formatCompactMetricValue(value: number | null, digits: number) {
  if (value === null || !Number.isFinite(value)) {
    return '--';
  }

  return value.toFixed(digits);
}

function formatCompactDate(value: string | null) {
  if (!value) {
    return 'No KNMI date';
  }

  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(parsed);
}

const ExploreWeatherMap = memo(function ExploreWeatherMap({
  municipalities,
  windVectors,
}: ExploreWeatherMapProps) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const mapRef = useRef<MapRef>(null);
  const hoveredMunicipalityIdRef = useRef<string | null>(null);
  const [hoveredMunicipalityId, setHoveredMunicipalityId] = useState<string | null>(null);
  const [selectedMunicipalityId, setSelectedMunicipalityId] = useState<string | null>(null);
  const [selectedPopupPosition, setSelectedPopupPosition] = useState<SelectedPopupPosition | null>(null);
  const [provinces, setProvinces] = useState<ProvinceFeatureCollection>(EMPTY_PROVINCES);
  const activeOverlay = resolveWeatherOverlay(searchParams.get('weatherOverlay'));
  const cameraBounds = useMemo(
    () => extendBounds(NETHERLANDS_BOUNDS, WEATHER_CAMERA_BUFFER_LNG, WEATHER_CAMERA_BUFFER_LAT),
    [],
  );
  const maxCameraBounds = useMemo(
    () => extendBounds(cameraBounds, WEATHER_PAN_BUFFER_LNG, WEATHER_PAN_BUFFER_LAT),
    [cameraBounds],
  );

  useEffect(() => {
    let cancelled = false;

    fetch('/api/boundaries?type=provinces', { cache: 'force-cache' })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Province boundary request failed with ${response.status}`);
        }

        return response.json() as Promise<ProvinceFeatureCollection>;
      })
      .then((payload) => {
        if (cancelled) {
          return;
        }

        setProvinces(payload);
      })
      .catch(() => {
        if (!cancelled) {
          setProvinces(EMPTY_PROVINCES);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedMunicipalityId && !municipalities.some((municipality) => municipality.municipalityId === selectedMunicipalityId)) {
      setSelectedMunicipalityId(null);
      setSelectedPopupPosition(null);
    }
  }, [municipalities, selectedMunicipalityId]);

  const temperatureValues = useMemo(() => {
    return municipalities
      .map((municipality) => municipality.metrics.temperature)
      .filter((value): value is number => value !== null && Number.isFinite(value));
  }, [municipalities]);

  const temperatureRange = useMemo(() => buildMetricRange(temperatureValues), [temperatureValues]);

  const precipitationValues = useMemo(() => {
    return municipalities
      .map((municipality) => municipality.metrics.precipitation)
      .filter((value): value is number => value !== null && Number.isFinite(value));
  }, [municipalities]);

  const precipitationRange = useMemo(() => buildMetricRange(precipitationValues), [precipitationValues]);

  const municipalityFeatures = useMemo(() => {
    return municipalities.map((municipality) => {
      const droughtSeverity = droughtSignalForMunicipality(municipality) ?? 0;
      const hasDrought = municipality.metrics.spi3 !== null || municipality.metrics.spei3 !== null;
      const temperature = municipality.metrics.temperature;
      const temperatureSeverity = linearMetricSeverity(temperature, temperatureRange) ?? 0;
      const hasTemperature = temperature !== null && Number.isFinite(temperature);
      const cloudCover = municipality.knmi.cloudCoverOkta.avg;
      const cloudSeverity = linearMetricSeverity(cloudCover, CLOUD_COVER_RANGE) ?? 0;
      const hasCloudCover = cloudCover !== null && Number.isFinite(cloudCover);

      return {
        type: 'Feature',
        geometry: municipality.geometry,
        properties: {
          municipalityId: municipality.municipalityId,
          name: municipality.name,
          droughtSeverity,
          hasDrought: hasDrought ? 1 : 0,
          temperatureC: hasTemperature ? temperature : 0,
          temperatureSeverity,
          hasTemperature: hasTemperature ? 1 : 0,
          cloudCoverOkta: hasCloudCover ? cloudCover : 0,
          cloudSeverity,
          hasCloudCover: hasCloudCover ? 1 : 0,
        },
      } satisfies GeoJSON.Feature<GeoJSON.Geometry, WeatherMapFeatureProperties>;
    });
  }, [municipalities, temperatureRange]);

  const municipalityGeojson = useMemo(() => {
    return {
      type: 'FeatureCollection',
      features: municipalityFeatures,
    } satisfies GeoJSON.FeatureCollection<GeoJSON.Geometry, WeatherMapFeatureProperties>;
  }, [municipalityFeatures]);

  const provinceSummaries = useMemo<ProvinceOverlaySummary[]>(() => {
    const provinceBuckets: ProvinceBucket[] = provinces.features.flatMap((feature, index) => {
      if (!isPolygonalGeometry(feature.geometry)) {
        return [];
      }

      const fallbackCenter = geometryMidpoint(feature.geometry);
      if (!fallbackCenter) {
        return [];
      }

      const provinceName = typeof feature.properties?.naam === 'string'
        ? feature.properties.naam
        : `Province ${index + 1}`;

      return [{
        provinceId: String(feature.id ?? provinceName ?? index + 1),
        name: provinceName,
        geometry: feature.geometry,
        fallbackCenter,
        areaScore: geometryAreaScore(feature.geometry),
        memberCentroids: [] as Array<{ lng: number; lat: number }>,
        temperatureValues: [] as number[],
        cloudValues: [] as number[],
        precipitationValues: [] as number[],
      } satisfies ProvinceBucket];
    });

    for (const municipality of municipalities) {
      if (!municipality.centroid) {
        continue;
      }

      const municipalityPoint = point([municipality.centroid.lng, municipality.centroid.lat]);
      const provinceBucket = provinceBuckets.find((candidate: ProvinceBucket) => {
        return booleanPointInPolygon(municipalityPoint, candidate.geometry);
      });

      if (!provinceBucket) {
        continue;
      }

      provinceBucket.memberCentroids.push(municipality.centroid);

      if (municipality.metrics.temperature !== null && Number.isFinite(municipality.metrics.temperature)) {
        provinceBucket.temperatureValues.push(municipality.metrics.temperature);
      }

      if (
        municipality.knmi.cloudCoverOkta.avg !== null
        && Number.isFinite(municipality.knmi.cloudCoverOkta.avg)
      ) {
        provinceBucket.cloudValues.push(municipality.knmi.cloudCoverOkta.avg);
      }

      if (
        municipality.metrics.precipitation !== null
        && Number.isFinite(municipality.metrics.precipitation)
      ) {
        provinceBucket.precipitationValues.push(municipality.metrics.precipitation);
      }
    }

    return provinceBuckets
      .map((provinceBucket: ProvinceBucket) => {
        const labelCenter = averageCoordinate(provinceBucket.memberCentroids) ?? provinceBucket.fallbackCenter;

        return {
          provinceId: provinceBucket.provinceId,
          name: provinceBucket.name,
          lng: labelCenter.lng,
          lat: labelCenter.lat,
          areaScore: provinceBucket.areaScore,
          temperatureAvg: averageValues(provinceBucket.temperatureValues),
          cloudCoverAvg: averageValues(provinceBucket.cloudValues),
          precipitationAvg: averageValues(provinceBucket.precipitationValues),
        } satisfies ProvinceOverlaySummary;
      })
      .sort((left: ProvinceOverlaySummary, right: ProvinceOverlaySummary) => right.areaScore - left.areaScore);
  }, [municipalities, provinces]);

  const temperatureLabelOverlays = useMemo<TemperatureLabelOverlay[]>(() => {
    if (activeOverlay !== 'temperature') {
      return [] as TemperatureLabelOverlay[];
    }

    return provinceSummaries
      .flatMap((provinceSummary: ProvinceOverlaySummary) => {
        if (provinceSummary.temperatureAvg === null) {
          return [];
        }

        const temperatureSeverity = linearMetricSeverity(provinceSummary.temperatureAvg, temperatureRange);

        if (temperatureSeverity === null) {
          return [];
        }

        return [{
          municipalityId: provinceSummary.provinceId,
          lng: provinceSummary.lng,
          lat: provinceSummary.lat,
          temperatureLabel: formatTemperatureLabel(provinceSummary.temperatureAvg),
          temperatureSeverity,
        } satisfies TemperatureLabelOverlay];
      });
  }, [activeOverlay, provinceSummaries, temperatureRange]);

  const visibleTemperatureLabelOverlays = useMemo<TemperatureLabelOverlay[]>(() => {
    if (activeOverlay !== 'temperature') {
      return [] as TemperatureLabelOverlay[];
    }

    return selectSpacedOverlays(
      temperatureLabelOverlays,
      0,
      temperatureLabelOverlays.length || 1,
    );
  }, [activeOverlay, temperatureLabelOverlays]);

  const cloudLabelOverlays = useMemo<CloudLabelOverlay[]>(() => {
    if (activeOverlay !== 'cloud') {
      return [] as CloudLabelOverlay[];
    }

    return provinceSummaries
      .flatMap((provinceSummary: ProvinceOverlaySummary) => {
        if (provinceSummary.cloudCoverAvg === null || !Number.isFinite(provinceSummary.cloudCoverAvg)) {
          return [];
        }

        const cloudSeverity = linearMetricSeverity(provinceSummary.cloudCoverAvg, CLOUD_COVER_RANGE);
        if (cloudSeverity === null) {
          return [];
        }

        return [{
          municipalityId: provinceSummary.provinceId,
          lng: provinceSummary.lng,
          lat: provinceSummary.lat,
          cloudLabel: formatCloudCoverPercentLabel(provinceSummary.cloudCoverAvg),
          cloudDetailLabel: formatCloudCoverLabel(provinceSummary.cloudCoverAvg),
          cloudSeverity,
        } satisfies CloudLabelOverlay];
      });
  }, [activeOverlay, provinceSummaries]);

  const visibleCloudLabelOverlays = useMemo<CloudLabelOverlay[]>(() => {
    if (activeOverlay !== 'cloud') {
      return [] as CloudLabelOverlay[];
    }

    return selectSpacedOverlays(
      cloudLabelOverlays,
      0,
      cloudLabelOverlays.length || 1,
    );
  }, [activeOverlay, cloudLabelOverlays]);

  const precipitationLabelOverlays = useMemo<PrecipitationLabelOverlay[]>(() => {
    if (activeOverlay !== 'precipitation') {
      return [] as PrecipitationLabelOverlay[];
    }

    return provinceSummaries
      .flatMap((provinceSummary: ProvinceOverlaySummary) => {
        if (provinceSummary.precipitationAvg === null || !Number.isFinite(provinceSummary.precipitationAvg)) {
          return [];
        }

        const precipitationSeverity = linearMetricSeverity(
          provinceSummary.precipitationAvg,
          precipitationRange,
        );

        if (precipitationSeverity === null) {
          return [];
        }

        return [{
          municipalityId: provinceSummary.provinceId,
          lng: provinceSummary.lng,
          lat: provinceSummary.lat,
          precipitationLabel: formatPrecipitationLabel(provinceSummary.precipitationAvg),
          precipitationSeverity,
        } satisfies PrecipitationLabelOverlay];
      });
  }, [activeOverlay, precipitationRange, provinceSummaries]);

  const visiblePrecipitationLabelOverlays = useMemo<PrecipitationLabelOverlay[]>(() => {
    if (activeOverlay !== 'precipitation') {
      return [] as PrecipitationLabelOverlay[];
    }

    return selectSpacedOverlays(
      precipitationLabelOverlays,
      0,
      precipitationLabelOverlays.length || 1,
    );
  }, [activeOverlay, precipitationLabelOverlays]);

  const windArrowOverlays = useMemo(() => {
    if (activeOverlay !== 'wind') {
      return [] as WindArrowOverlay[];
    }

    const overlays: WindArrowOverlay[] = [];

    for (const windVector of windVectors) {
      const windSpeedMs = windVector.speedMs;
      const flowDirectionDeg = windVector.flowDirectionDeg;
      const directionRadians = (flowDirectionDeg * Math.PI) / 180;
      const latitudeRadians = (windVector.location.lat * Math.PI) / 180;
      const longitudeScale = Math.max(Math.cos(latitudeRadians), 0.25);
      const shaftLength = 0.078;
      const shaftDeltaLat = Math.cos(directionRadians) * shaftLength;
      const shaftDeltaLng = (Math.sin(directionRadians) * shaftLength) / longitudeScale;
      const perpendicularRadians = directionRadians + Math.PI / 2;
      const centeredSampleOrder = windVector.sampleOrder - (windVector.sampleCount + 1) / 2;
      const lateralOffset = centeredSampleOrder * 0.0046;
      const offsetLat = Math.cos(perpendicularRadians) * lateralOffset;
      const offsetLng = (Math.sin(perpendicularRadians) * lateralOffset) / longitudeScale;
      const anchorLng = windVector.location.lng + offsetLng;
      const anchorLat = windVector.location.lat + offsetLat;
      const tailLng = anchorLng - shaftDeltaLng * 0.5;
      const tailLat = anchorLat - shaftDeltaLat * 0.5;
      const headLng = anchorLng + shaftDeltaLng * 0.5;
      const headLat = anchorLat + shaftDeltaLat * 0.5;
      const headLength = shaftLength * 0.32;
      const headSpread = Math.PI / 7;
      const leftWingRadians = directionRadians + Math.PI - headSpread;
      const rightWingRadians = directionRadians + Math.PI + headSpread;
      const leftWingLng = headLng + (Math.sin(leftWingRadians) * headLength) / longitudeScale;
      const leftWingLat = headLat + Math.cos(leftWingRadians) * headLength;
      const rightWingLng = headLng + (Math.sin(rightWingRadians) * headLength) / longitudeScale;
      const rightWingLat = headLat + Math.cos(rightWingRadians) * headLength;
      const labelOffsetAlong = shaftLength * 0.16;
      const labelOffsetSide = 0.011;
      const labelLng = headLng
        + (Math.sin(directionRadians) * labelOffsetAlong) / longitudeScale
        + (Math.sin(perpendicularRadians) * labelOffsetSide) / longitudeScale;
      const labelLat = headLat
        + Math.cos(directionRadians) * labelOffsetAlong
        + Math.cos(perpendicularRadians) * labelOffsetSide;

      overlays.push({
        feature: {
          type: 'Feature',
          geometry: {
            type: 'MultiLineString',
            coordinates: [
              [
                [tailLng, tailLat],
                [headLng, headLat],
              ],
              [
                [headLng, headLat],
                [leftWingLng, leftWingLat],
              ],
              [
                [headLng, headLat],
                [rightWingLng, rightWingLat],
              ],
            ],
          },
          properties: {
            municipalityId: windVector.municipalityId,
            stationId: windVector.stationId,
            stationName: windVector.stationName,
            observedOn: windVector.observedOn,
            windSpeedMs,
            flowDirectionDeg,
          },
        },
        labelLng,
        labelLat,
        speedLabel: formatWindSpeedLabel(windSpeedMs),
      });
    }

    return overlays;
  }, [activeOverlay, windVectors]);

  const windFlashGeojson = useMemo(() => {
    return {
      type: 'FeatureCollection',
      features: windArrowOverlays.map((overlay) => overlay.feature),
    } satisfies GeoJSON.FeatureCollection<GeoJSON.MultiLineString, WindFlashFeatureProperties>;
  }, [windArrowOverlays]);

  const selectedGeojson = useMemo(
    () => hoveredMunicipalityCollection(municipalityFeatures, selectedMunicipalityId),
    [municipalityFeatures, selectedMunicipalityId],
  );

  const selectedMunicipality = useMemo(() => {
    if (!selectedMunicipalityId) {
      return null;
    }

    return municipalities.find((municipality) => municipality.municipalityId === selectedMunicipalityId) ?? null;
  }, [municipalities, selectedMunicipalityId]);

  const hoveredGeojson = useMemo(() => {
    if (!hoveredMunicipalityId || hoveredMunicipalityId === selectedMunicipalityId) {
      return EMPTY_MUNICIPALITIES;
    }

    return hoveredMunicipalityCollection(municipalityFeatures, hoveredMunicipalityId);
  }, [hoveredMunicipalityId, municipalityFeatures, selectedMunicipalityId]);

  const fitMapToFrame = () => {
    const map = mapRef.current?.getMap();
    if (!map) {
      return;
    }

    map.fitBounds(
      [
        [cameraBounds[0], cameraBounds[1]],
        [cameraBounds[2], cameraBounds[3]],
      ],
      {
        padding: {
          top: 28,
          right: 28,
          bottom: 28,
          left: 28,
        },
        duration: 0,
        maxZoom: WEATHER_FIT_MAX_ZOOM,
      },
    );
  };

  const updateOverlay = (nextOverlay: WeatherOverlay | null) => {
    const nextParams = new URLSearchParams(searchParams.toString());
    nextParams.set('tab', 'weather');
    writeWeatherOverlay(nextParams, nextOverlay);
    router.replace(`${pathname}?${nextParams.toString()}`);
  };

  const syncHoveredMunicipality = (municipalityId: string | null) => {
    if (hoveredMunicipalityIdRef.current === municipalityId) {
      return;
    }

    hoveredMunicipalityIdRef.current = municipalityId;
    setHoveredMunicipalityId(municipalityId);

    if (mapRef.current) {
      mapRef.current.getCanvas().style.cursor = municipalityId ? 'pointer' : '';
    }
  };

  useEffect(() => {
    if (
      hoveredMunicipalityIdRef.current &&
      !municipalities.some((municipality) => municipality.municipalityId === hoveredMunicipalityIdRef.current)
    ) {
      syncHoveredMunicipality(null);
    }
  }, [municipalities]);

  const handleMapLoad = () => {
    fitMapToFrame();
    requestAnimationFrame(fitMapToFrame);
    window.setTimeout(fitMapToFrame, 120);
  };

  const handleMouseMove = (event: MapMouseEvent) => {
    const feature = event.features?.[0];
    const municipalityId = typeof feature?.properties?.municipalityId === 'string'
      ? feature.properties.municipalityId
      : null;

    if (!municipalityId) {
      syncHoveredMunicipality(null);
      return;
    }

    syncHoveredMunicipality(municipalityId);
  };

  const handleMouseLeave = () => {
    syncHoveredMunicipality(null);
  };

  const handleClick = (event: MapClickEvent) => {
    const feature = event.features?.[0];
    const municipalityId = typeof feature?.properties?.municipalityId === 'string'
      ? feature.properties.municipalityId
      : null;

    if (!municipalityId) {
      setSelectedMunicipalityId(null);
      setSelectedPopupPosition(null);
      return;
    }

    setSelectedMunicipalityId((current: string | null) => {
      const nextValue = current === municipalityId ? null : municipalityId;

      if (!nextValue) {
        setSelectedPopupPosition(null);
      } else {
        setSelectedPopupPosition({
          lng: event.lngLat.lng,
          lat: event.lngLat.lat,
        });
      }

      return nextValue;
    });
  };

  const showTemperatureOverlay = activeOverlay === 'temperature';
  const showCloudOverlay = activeOverlay === 'cloud';
  const showDroughtOverlay = activeOverlay === null;
  const showMunicipalityHighlights = activeOverlay === null;

  return (
    <div
      className="relative flex h-full min-h-0 overflow-hidden"
      style={{
        background: '#171717',
      }}
    >
      <MapView
        ref={mapRef}
        reuseMaps
        renderWorldCopies={false}
        initialViewState={{
          longitude: NETHERLANDS_CENTER.lng,
          latitude: NETHERLANDS_CENTER.lat,
          zoom: WEATHER_INITIAL_ZOOM,
        }}
        mapStyle={WEATHER_MAP_STYLE}
        attributionControl={false}
        dragRotate={false}
        pitchWithRotate={false}
        touchZoomRotate={false}
        scrollZoom
        doubleClickZoom={false}
        interactiveLayerIds={['explore-weather-municipality-hitbox']}
        maxBounds={maxCameraBounds}
        minZoom={WEATHER_MIN_ZOOM}
        maxZoom={WEATHER_MAX_ZOOM}
        onLoad={handleMapLoad}
        onResize={fitMapToFrame}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        style={{ width: '100%', height: '100%' }}
      >
        <Source id="explore-weather-municipalities" type="geojson" data={municipalityGeojson}>
          <Layer {...municipalityBaseFillLayer} />
          {showTemperatureOverlay ? <Layer {...temperatureAreaFillLayer} /> : null}
          {showTemperatureOverlay ? <Layer {...temperatureAreaGlowLayer} /> : null}
          {showTemperatureOverlay ? <Layer {...temperatureMunicipalityLineLayer} /> : null}
          {showCloudOverlay ? <Layer {...cloudAreaFillLayer} /> : null}
          {showCloudOverlay ? <Layer {...cloudAreaGlowLayer} /> : null}
          {showCloudOverlay ? <Layer {...cloudMunicipalityLineLayer} /> : null}
          {showDroughtOverlay ? <Layer {...municipalityDataFillLayer} /> : null}
          <Layer {...municipalityHitboxLayer} />
          <Layer {...municipalityLineLayer} />
        </Source>

        {activeOverlay === 'wind' ? (
          <Source id="explore-weather-wind-flashes" type="geojson" data={windFlashGeojson}>
            <Layer {...windFlashGlowLayer} />
            <Layer {...windFlashStrokeLayer} />
          </Source>
        ) : null}

        {activeOverlay === 'wind'
          ? windArrowOverlays.map((overlay) => (
              <Marker
                key={`${overlay.feature.properties.stationId}:${overlay.feature.properties.observedOn}`}
                longitude={overlay.labelLng}
                latitude={overlay.labelLat}
                anchor="left"
              >
                <div
                  style={{
                    pointerEvents: 'none',
                    color: 'rgba(187,247,208,0.94)',
                    fontSize: 10,
                    lineHeight: 1,
                    letterSpacing: '0.04em',
                    textShadow: '0 0 10px rgba(0,0,0,0.8), 0 0 4px rgba(0,0,0,0.9)',
                    whiteSpace: 'nowrap',
                    fontWeight: 600,
                  }}
                >
                  {overlay.speedLabel}
                </div>
              </Marker>
            ))
          : null}

        {activeOverlay === 'temperature'
          ? visibleTemperatureLabelOverlays.map((overlay) => (
              <Marker
                key={overlay.municipalityId}
                longitude={overlay.lng}
                latitude={overlay.lat}
                anchor="center"
              >
                <div
                  style={{
                    pointerEvents: 'none',
                    color: 'rgba(187,247,208,0.96)',
                    fontSize: 13.5,
                    lineHeight: 0.94,
                    letterSpacing: '0.08em',
                    whiteSpace: 'nowrap',
                    fontWeight: 700,
                    fontVariantNumeric: 'tabular-nums',
                    fontFamily: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Consolas, monospace',
                    textShadow: '0 0 14px rgba(74,222,128,0.34), 0 1px 10px rgba(0,0,0,0.9)',
                    opacity: 0.82 + (overlay.temperatureSeverity * 0.18),
                  }}
                >
                  {overlay.temperatureLabel}
                </div>
              </Marker>
            ))
          : null}

        {activeOverlay === 'precipitation'
          ? visiblePrecipitationLabelOverlays.map((overlay) => (
              <Marker
                key={overlay.municipalityId}
                longitude={overlay.lng}
                latitude={overlay.lat}
                anchor="center"
              >
                <div
                  style={{
                    pointerEvents: 'none',
                    color: 'rgba(187,247,208,0.96)',
                    fontSize: 12.5,
                    lineHeight: 0.94,
                    letterSpacing: '0.08em',
                    whiteSpace: 'nowrap',
                    fontWeight: 700,
                    fontVariantNumeric: 'tabular-nums',
                    fontFamily: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Consolas, monospace',
                    textShadow: '0 0 14px rgba(74,222,128,0.34), 0 1px 10px rgba(0,0,0,0.9)',
                    opacity: 0.82 + (overlay.precipitationSeverity * 0.18),
                  }}
                >
                  {overlay.precipitationLabel}
                </div>
              </Marker>
            ))
          : null}

        {activeOverlay === 'cloud'
          ? visibleCloudLabelOverlays.map((overlay) => {
              return (
                <Marker
                  key={overlay.municipalityId}
                  longitude={overlay.lng}
                  latitude={overlay.lat}
                  anchor="center"
                >
                  <div
                    style={{
                      pointerEvents: 'none',
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'center',
                      justifyContent: 'center',
                      textAlign: 'center',
                      transform: `scale(${0.98 + overlay.cloudSeverity * 0.08})`,
                      opacity: 0.82 + (overlay.cloudSeverity * 0.18),
                    }}
                  >
                    <div
                      style={{
                        color: 'rgba(187,247,208,0.94)',
                        fontSize: 13.5,
                        lineHeight: 0.94,
                        letterSpacing: '0.08em',
                        whiteSpace: 'nowrap',
                        fontWeight: 700,
                        fontVariantNumeric: 'tabular-nums',
                        fontFamily: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Consolas, monospace',
                        textShadow: '0 0 14px rgba(74,222,128,0.34), 0 1px 10px rgba(0,0,0,0.9)',
                      }}
                    >
                      {overlay.cloudLabel}
                    </div>
                    <div
                      style={{
                        marginTop: 4,
                        color: 'rgba(187,247,208,0.86)',
                        fontSize: 9,
                        lineHeight: 0.98,
                        letterSpacing: '0.08em',
                        whiteSpace: 'nowrap',
                        fontWeight: 600,
                        textAlign: 'center',
                        fontVariantNumeric: 'tabular-nums',
                        fontFamily: '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Consolas, monospace',
                        textShadow: '0 0 12px rgba(74,222,128,0.22), 0 1px 8px rgba(0,0,0,0.78)',
                      }}
                    >
                      {overlay.cloudDetailLabel}
                    </div>
                  </div>
                </Marker>
              );
            })
          : null}

        {provinces.features.length > 0 ? (
          <Source id="explore-weather-provinces" type="geojson" data={provinces}>
            <Layer {...provinceLineLayer} />
          </Source>
        ) : null}

        {showMunicipalityHighlights ? (
          <Source id="explore-weather-hovered" type="geojson" data={hoveredGeojson}>
            <Layer {...hoveredFillLayer} />
            <Layer {...hoveredLineLayer} />
          </Source>
        ) : null}

        {showMunicipalityHighlights ? (
          <Source id="explore-weather-selected" type="geojson" data={selectedGeojson}>
            <Layer {...selectedFillLayer} />
            <Layer {...selectedLineLayer} />
          </Source>
        ) : null}

        {selectedMunicipality && selectedPopupPosition ? (
          <Marker
            longitude={selectedPopupPosition.lng}
            latitude={selectedPopupPosition.lat}
            anchor="bottom"
          >
            <div className="pointer-events-none flex flex-col items-center">
              <div
                className="rounded-xl border px-3 py-2"
                style={{
                  minWidth: 220,
                  background: 'rgba(26,26,26,0.96)',
                  borderColor: 'rgba(255,255,255,0.12)',
                  boxShadow: '0 14px 34px rgba(0,0,0,0.34)',
                }}
              >
                <div
                  className="text-[13px] font-semibold tracking-[-0.02em]"
                  style={{ color: 'rgba(255,255,255,0.92)' }}
                >
                  {selectedMunicipality.name}
                </div>
                <div
                  className="mt-0.5 text-[10px] uppercase tracking-[0.16em]"
                  style={{ color: 'rgba(255,255,255,0.42)' }}
                >
                  {formatCompactDate(selectedMunicipality.latestKnmiBucket)}
                </div>

                <div
                  className="mt-2 grid grid-cols-[minmax(0,1fr)_repeat(3,auto)] gap-x-2 gap-y-1 text-[10px]"
                  style={{ color: 'rgba(255,255,255,0.78)' }}
                >
                  <div />
                  <div style={{ color: 'rgba(255,255,255,0.36)' }}>Avg</div>
                  <div style={{ color: 'rgba(255,255,255,0.36)' }}>Min</div>
                  <div style={{ color: 'rgba(255,255,255,0.36)' }}>Max</div>

                  <div style={{ color: 'rgba(255,255,255,0.54)' }}>Temp</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.temperatureC.avg, 1)}</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.temperatureC.min, 1)}</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.temperatureC.max, 1)}</div>

                  <div style={{ color: 'rgba(255,255,255,0.54)' }}>Rain</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.precipitationMm.avg, 1)}</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.precipitationMm.min, 1)}</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.precipitationMm.max, 1)}</div>

                  <div style={{ color: 'rgba(255,255,255,0.54)' }}>Wind</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.windSpeedMs.avg, 1)}</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.windSpeedMs.min, 1)}</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.windSpeedMs.max, 1)}</div>

                  <div style={{ color: 'rgba(255,255,255,0.54)' }}>Cloud</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.cloudCoverOkta.avg, 1)}</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.cloudCoverOkta.min, 1)}</div>
                  <div>{formatCompactMetricValue(selectedMunicipality.knmi.cloudCoverOkta.max, 1)}</div>
                </div>
              </div>

              <div
                style={{
                  width: 10,
                  height: 10,
                  marginTop: -1,
                  transform: 'rotate(45deg)',
                  background: 'rgba(26,26,26,0.96)',
                  borderRight: '1px solid rgba(255,255,255,0.12)',
                  borderBottom: '1px solid rgba(255,255,255,0.12)',
                }}
              />
            </div>
          </Marker>
        ) : null}
      </MapView>

      <div className="absolute right-3 top-3 z-10 flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => updateOverlay(activeOverlay === 'wind' ? null : 'wind')}
          className="rounded-full px-3 py-1.5 text-[10px] uppercase tracking-[0.18em] transition-colors"
          style={{
            background: activeOverlay === 'wind' ? ACTIVE_OVERLAY_BACKGROUND : INACTIVE_OVERLAY_BACKGROUND,
            border: `1px solid ${activeOverlay === 'wind' ? ACTIVE_OVERLAY_BORDER : INACTIVE_OVERLAY_BORDER}`,
            color: activeOverlay === 'wind' ? ACTIVE_OVERLAY_FOREGROUND : INACTIVE_OVERLAY_FOREGROUND,
            boxShadow: '0 10px 24px rgba(0,0,0,0.18)',
          }}
        >
          Wind
        </button>
        <button
          type="button"
          onClick={() => updateOverlay(activeOverlay === 'temperature' ? null : 'temperature')}
          className="rounded-full px-3 py-1.5 text-[10px] uppercase tracking-[0.18em] transition-colors"
          style={{
            background: activeOverlay === 'temperature' ? ACTIVE_OVERLAY_BACKGROUND : INACTIVE_OVERLAY_BACKGROUND,
            border: `1px solid ${activeOverlay === 'temperature' ? ACTIVE_OVERLAY_BORDER : INACTIVE_OVERLAY_BORDER}`,
            color: activeOverlay === 'temperature' ? ACTIVE_OVERLAY_FOREGROUND : INACTIVE_OVERLAY_FOREGROUND,
            boxShadow: '0 10px 24px rgba(0,0,0,0.18)',
          }}
        >
          Temperature
        </button>
        <button
          type="button"
          onClick={() => updateOverlay(activeOverlay === 'cloud' ? null : 'cloud')}
          className="rounded-full px-3 py-1.5 text-[10px] uppercase tracking-[0.18em] transition-colors"
          style={{
            background: activeOverlay === 'cloud' ? ACTIVE_OVERLAY_BACKGROUND : INACTIVE_OVERLAY_BACKGROUND,
            border: `1px solid ${activeOverlay === 'cloud' ? ACTIVE_OVERLAY_BORDER : INACTIVE_OVERLAY_BORDER}`,
            color: activeOverlay === 'cloud' ? ACTIVE_OVERLAY_FOREGROUND : INACTIVE_OVERLAY_FOREGROUND,
            boxShadow: '0 10px 24px rgba(0,0,0,0.18)',
          }}
        >
          Cloud
        </button>
        <button
          type="button"
          onClick={() => updateOverlay(activeOverlay === 'precipitation' ? null : 'precipitation')}
          className="rounded-full px-3 py-1.5 text-[10px] uppercase tracking-[0.18em] transition-colors"
          style={{
            background: activeOverlay === 'precipitation' ? ACTIVE_OVERLAY_BACKGROUND : INACTIVE_OVERLAY_BACKGROUND,
            border: `1px solid ${activeOverlay === 'precipitation' ? ACTIVE_OVERLAY_BORDER : INACTIVE_OVERLAY_BORDER}`,
            color: activeOverlay === 'precipitation' ? ACTIVE_OVERLAY_FOREGROUND : INACTIVE_OVERLAY_FOREGROUND,
            boxShadow: '0 10px 24px rgba(0,0,0,0.18)',
          }}
        >
          Precipitation
        </button>
      </div>
    </div>
  );
});

ExploreWeatherMap.displayName = 'ExploreWeatherMap';

export default ExploreWeatherMap;