"use client";

import booleanPointInPolygon from "@turf/boolean-point-in-polygon";
import { point } from "@turf/helpers";
import type { LayerSpecification } from "maplibre-gl";
import { type CSSProperties, useEffect, useMemo, useState } from "react";
import { Layer, Marker, Source } from "react-map-gl/maplibre";

import {
  buildMetricRange,
  linearMetricSeverity,
  type ExploreWeatherMunicipality,
  type ExploreWeatherPayload,
  type ExploreWeatherWindVector,
} from "@/app/explore/weather";

// Live-map weather overlay modes. Everything is shown as floating value labels
// over the untouched satellite basemap (no area fills / no state highlighting),
// plus wind arrows. "drought" shows the SPI-3 index value per province.
export type LiveWeatherOverlay =
  | "wind"
  | "temperature"
  | "cloud"
  | "precipitation"
  | "drought";

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

type DroughtLabelOverlay = {
  municipalityId: string;
  lng: number;
  lat: number;
  droughtLabel: string;
  dry: boolean;
  intensity: number;
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
  droughtAvg: number | null;
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
  droughtValues: number[];
};

type ProvinceFeatureCollection = GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  GeoJSON.GeoJsonProperties
>;

const LABEL_FONT =
  '"IBM Plex Mono", "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
const CLOUD_COVER_RANGE = { min: 0, max: 8 } as const;

// Wind speed labels render as DOM marker pills with a blurred backdrop, one per
// station-day (≈70). Every arrow carries its own value label so each vector's
// magnitude is always legible; the arrows are a single GeoJSON source
// (effectively free on the GPU) and convey direction and magnitude.

// Frosted "HUD" pill so value labels read over any background (no map tinting).
const LABEL_PILL_STYLE: CSSProperties = {
  padding: "2px 7px",
  borderRadius: 999,
  background: "rgba(7,13,20,0.62)",
  border: "1px solid rgba(255,255,255,0.12)",
  boxShadow: "0 2px 10px rgba(0,0,0,0.45)",
  backdropFilter: "blur(3px)",
  WebkitBackdropFilter: "blur(3px)",
};

const windFlashGlowLayer: LayerSpecification = {
  id: "live-weather-wind-glow",
  type: "line",
  source: "live-weather-wind-flashes",
  paint: {
    "line-color": "rgba(120,240,170,0.42)",
    "line-width": ["interpolate", ["linear"], ["get", "windSpeedMs"], 0, 2.4, 12, 5.6],
    "line-blur": 3.2,
    "line-opacity": 0.6,
  },
  layout: { "line-cap": "round", "line-join": "round" },
};

// Dark casing beneath the stroke so green arrows cut through green terrain.
const windFlashCasingLayer: LayerSpecification = {
  id: "live-weather-wind-casing",
  type: "line",
  source: "live-weather-wind-flashes",
  paint: {
    "line-color": "rgba(2,14,8,0.7)",
    "line-width": ["interpolate", ["linear"], ["get", "windSpeedMs"], 0, 2.2, 12, 4],
    "line-opacity": 0.85,
  },
  layout: { "line-cap": "round", "line-join": "round" },
};

const windFlashStrokeLayer: LayerSpecification = {
  id: "live-weather-wind-stroke",
  type: "line",
  source: "live-weather-wind-flashes",
  paint: {
    "line-color": "rgba(141,247,175,0.98)",
    "line-width": ["interpolate", ["linear"], ["get", "windSpeedMs"], 0, 1, 12, 2.4],
    "line-opacity": 0.98,
  },
  layout: { "line-cap": "round", "line-join": "round" },
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

function formatDroughtLabel(spi: number) {
  const sign = spi > 0 ? "+" : spi < 0 ? "\u2212" : "";
  return `SPI ${sign}${Math.abs(spi).toFixed(1)}`;
}

function isPolygonalGeometry(
  geometry: GeoJSON.Geometry,
): geometry is GeoJSON.Polygon | GeoJSON.MultiPolygon {
  return geometry.type === "Polygon" || geometry.type === "MultiPolygon";
}

function visitBounds(geometry: GeoJSON.Geometry) {
  let minLng = Number.POSITIVE_INFINITY;
  let minLat = Number.POSITIVE_INFINITY;
  let maxLng = Number.NEGATIVE_INFINITY;
  let maxLat = Number.NEGATIVE_INFINITY;
  const visit = (coordinates: unknown) => {
    if (!Array.isArray(coordinates) || coordinates.length === 0) return;
    if (
      coordinates.length >= 2 &&
      typeof coordinates[0] === "number" &&
      typeof coordinates[1] === "number"
    ) {
      const lng = coordinates[0];
      const lat = coordinates[1];
      minLng = Math.min(minLng, lng);
      minLat = Math.min(minLat, lat);
      maxLng = Math.max(maxLng, lng);
      maxLat = Math.max(maxLat, lat);
      return;
    }
    for (const child of coordinates) visit(child);
  };
  visit((geometry as { coordinates?: unknown }).coordinates);
  return { minLng, minLat, maxLng, maxLat };
}

function geometryAreaScore(geometry: GeoJSON.Geometry) {
  const { minLng, minLat, maxLng, maxLat } = visitBounds(geometry);
  if (
    !Number.isFinite(minLng) ||
    !Number.isFinite(minLat) ||
    !Number.isFinite(maxLng) ||
    !Number.isFinite(maxLat)
  ) {
    return 0;
  }
  const latitudeMidpointRadians = ((minLat + maxLat) / 2) * (Math.PI / 180);
  const longitudeScale = Math.max(Math.cos(latitudeMidpointRadians), 0.25);
  return Math.max((maxLng - minLng) * longitudeScale, 0) * Math.max(maxLat - minLat, 0);
}

function geometryMidpoint(geometry: GeoJSON.Geometry) {
  const { minLng, minLat, maxLng, maxLat } = visitBounds(geometry);
  if (
    !Number.isFinite(minLng) ||
    !Number.isFinite(minLat) ||
    !Number.isFinite(maxLng) ||
    !Number.isFinite(maxLat)
  ) {
    return null;
  }
  return { lng: (minLng + maxLng) / 2, lat: (minLat + maxLat) / 2 };
}

function squaredDistanceBetweenCoordinates(
  left: { lng: number; lat: number },
  right: { lng: number; lat: number },
) {
  const latitudeMidpointRadians = ((left.lat + right.lat) / 2) * (Math.PI / 180);
  const longitudeScale = Math.max(Math.cos(latitudeMidpointRadians), 0.25);
  const deltaLat = left.lat - right.lat;
  const deltaLng = (left.lng - right.lng) * longitudeScale;
  return deltaLat * deltaLat + deltaLng * deltaLng;
}

function selectSpacedOverlays<T extends { lng: number; lat: number }>(
  overlays: T[],
  minDistance: number,
  maxCount: number,
) {
  const selected: T[] = [];
  const minDistanceSquared = minDistance * minDistance;
  for (const overlay of overlays) {
    const isTooClose = selected.some(
      (candidate) =>
        squaredDistanceBetweenCoordinates(
          { lng: candidate.lng, lat: candidate.lat },
          { lng: overlay.lng, lat: overlay.lat },
        ) < minDistanceSquared,
    );
    if (isTooClose) continue;
    selected.push(overlay);
    if (selected.length >= maxCount) break;
  }
  return selected;
}

function averageCoordinate(coordinates: Array<{ lng: number; lat: number }>) {
  if (coordinates.length === 0) return null;
  const totals = coordinates.reduce(
    (acc, c) => ({ lng: acc.lng + c.lng, lat: acc.lat + c.lat }),
    { lng: 0, lat: 0 },
  );
  return { lng: totals.lng / coordinates.length, lat: totals.lat / coordinates.length };
}

function averageValues(values: number[]) {
  if (values.length === 0) return null;
  return values.reduce((total, value) => total + value, 0) / values.length;
}

export function WeatherOverlay({
  overlay,
  provinces,
}: {
  overlay: LiveWeatherOverlay | null;
  provinces: ProvinceFeatureCollection | null;
}) {
  const [payload, setPayload] = useState<ExploreWeatherPayload | null>(null);

  useEffect(() => {
    if (!overlay || payload) return;
    const controller = new AbortController();
    fetch("/api/explore/weather", { signal: controller.signal })
      .then((response) => (response.ok ? (response.json() as Promise<ExploreWeatherPayload>) : null))
      .then((data) => {
        if (data) setPayload(data);
      })
      .catch(() => {});
    return () => controller.abort();
  }, [overlay, payload]);

  const municipalities: ExploreWeatherMunicipality[] = useMemo(
    () => payload?.municipalities ?? [],
    [payload],
  );
  const windVectors: ExploreWeatherWindVector[] = useMemo(
    () => payload?.windVectors ?? [],
    [payload],
  );

  const temperatureRange = useMemo(
    () =>
      buildMetricRange(
        municipalities
          .map((m) => m.metrics.temperature)
          .filter((v): v is number => v !== null && Number.isFinite(v)),
      ),
    [municipalities],
  );
  const precipitationRange = useMemo(
    () =>
      buildMetricRange(
        municipalities
          .map((m) => m.metrics.precipitation)
          .filter((v): v is number => v !== null && Number.isFinite(v)),
      ),
    [municipalities],
  );

  const provinceSummaries = useMemo<ProvinceOverlaySummary[]>(() => {
    if (!provinces) return [];
    const provinceBuckets: ProvinceBucket[] = provinces.features.flatMap((feature, index) => {
      if (!feature.geometry || !isPolygonalGeometry(feature.geometry)) return [];
      const fallbackCenter = geometryMidpoint(feature.geometry);
      if (!fallbackCenter) return [];
      const provinceName =
        typeof feature.properties?.naam === "string"
          ? feature.properties.naam
          : `Province ${index + 1}`;
      return [
        {
          provinceId: String(feature.id ?? provinceName ?? index + 1),
          name: provinceName,
          geometry: feature.geometry,
          fallbackCenter,
          areaScore: geometryAreaScore(feature.geometry),
          memberCentroids: [],
          temperatureValues: [],
          cloudValues: [],
          precipitationValues: [],
          droughtValues: [],
        } satisfies ProvinceBucket,
      ];
    });

    for (const municipality of municipalities) {
      if (!municipality.centroid) continue;
      const municipalityPoint = point([municipality.centroid.lng, municipality.centroid.lat]);
      const bucket = provinceBuckets.find((candidate) =>
        booleanPointInPolygon(municipalityPoint, candidate.geometry),
      );
      if (!bucket) continue;
      bucket.memberCentroids.push(municipality.centroid);
      if (municipality.metrics.temperature !== null && Number.isFinite(municipality.metrics.temperature)) {
        bucket.temperatureValues.push(municipality.metrics.temperature);
      }
      if (municipality.knmi.cloudCoverOkta.avg !== null && Number.isFinite(municipality.knmi.cloudCoverOkta.avg)) {
        bucket.cloudValues.push(municipality.knmi.cloudCoverOkta.avg);
      }
      if (municipality.metrics.precipitation !== null && Number.isFinite(municipality.metrics.precipitation)) {
        bucket.precipitationValues.push(municipality.metrics.precipitation);
      }
      const spi = municipality.metrics.spi3 ?? municipality.metrics.spei3;
      if (spi !== null && spi !== undefined && Number.isFinite(spi)) {
        bucket.droughtValues.push(spi);
      }
    }

    return provinceBuckets
      .map((bucket) => {
        const labelCenter = averageCoordinate(bucket.memberCentroids) ?? bucket.fallbackCenter;
        return {
          provinceId: bucket.provinceId,
          name: bucket.name,
          lng: labelCenter.lng,
          lat: labelCenter.lat,
          areaScore: bucket.areaScore,
          temperatureAvg: averageValues(bucket.temperatureValues),
          cloudCoverAvg: averageValues(bucket.cloudValues),
          precipitationAvg: averageValues(bucket.precipitationValues),
          droughtAvg: averageValues(bucket.droughtValues),
        } satisfies ProvinceOverlaySummary;
      })
      .sort((left, right) => right.areaScore - left.areaScore);
  }, [municipalities, provinces]);

  const temperatureLabels = useMemo<TemperatureLabelOverlay[]>(() => {
    if (overlay !== "temperature") return [];
    const labels = provinceSummaries.flatMap((summary) => {
      if (summary.temperatureAvg === null) return [];
      const severity = linearMetricSeverity(summary.temperatureAvg, temperatureRange);
      if (severity === null) return [];
      return [
        {
          municipalityId: summary.provinceId,
          lng: summary.lng,
          lat: summary.lat,
          temperatureLabel: formatTemperatureLabel(summary.temperatureAvg),
          temperatureSeverity: severity,
        } satisfies TemperatureLabelOverlay,
      ];
    });
    return selectSpacedOverlays(labels, 0, labels.length || 1);
  }, [overlay, provinceSummaries, temperatureRange]);

  const cloudLabels = useMemo<CloudLabelOverlay[]>(() => {
    if (overlay !== "cloud") return [];
    const labels = provinceSummaries.flatMap((summary) => {
      if (summary.cloudCoverAvg === null || !Number.isFinite(summary.cloudCoverAvg)) return [];
      const severity = linearMetricSeverity(summary.cloudCoverAvg, CLOUD_COVER_RANGE);
      if (severity === null) return [];
      return [
        {
          municipalityId: summary.provinceId,
          lng: summary.lng,
          lat: summary.lat,
          cloudLabel: formatCloudCoverPercentLabel(summary.cloudCoverAvg),
          cloudDetailLabel: formatCloudCoverLabel(summary.cloudCoverAvg),
          cloudSeverity: severity,
        } satisfies CloudLabelOverlay,
      ];
    });
    return selectSpacedOverlays(labels, 0, labels.length || 1);
  }, [overlay, provinceSummaries]);

  const precipitationLabels = useMemo<PrecipitationLabelOverlay[]>(() => {
    if (overlay !== "precipitation") return [];
    const labels = provinceSummaries.flatMap((summary) => {
      if (summary.precipitationAvg === null || !Number.isFinite(summary.precipitationAvg)) return [];
      const severity = linearMetricSeverity(summary.precipitationAvg, precipitationRange);
      if (severity === null) return [];
      return [
        {
          municipalityId: summary.provinceId,
          lng: summary.lng,
          lat: summary.lat,
          precipitationLabel: formatPrecipitationLabel(summary.precipitationAvg),
          precipitationSeverity: severity,
        } satisfies PrecipitationLabelOverlay,
      ];
    });
    return selectSpacedOverlays(labels, 0, labels.length || 1);
  }, [overlay, precipitationRange, provinceSummaries]);

  const droughtLabels = useMemo<DroughtLabelOverlay[]>(() => {
    if (overlay !== "drought") return [];
    const labels = provinceSummaries.flatMap((summary) => {
      if (summary.droughtAvg === null || !Number.isFinite(summary.droughtAvg)) return [];
      const spi = summary.droughtAvg;
      return [
        {
          municipalityId: summary.provinceId,
          lng: summary.lng,
          lat: summary.lat,
          droughtLabel: formatDroughtLabel(spi),
          dry: spi < 0,
          intensity: Math.min(Math.abs(spi) / 2, 1),
        } satisfies DroughtLabelOverlay,
      ];
    });
    return selectSpacedOverlays(labels, 0, labels.length || 1);
  }, [overlay, provinceSummaries]);

  const windArrowOverlays = useMemo<WindArrowOverlay[]>(() => {
    if (overlay !== "wind") return [];
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
      const labelLng =
        headLng +
        (Math.sin(directionRadians) * labelOffsetAlong) / longitudeScale +
        (Math.sin(perpendicularRadians) * labelOffsetSide) / longitudeScale;
      const labelLat =
        headLat +
        Math.cos(directionRadians) * labelOffsetAlong +
        Math.cos(perpendicularRadians) * labelOffsetSide;
      overlays.push({
        feature: {
          type: "Feature",
          geometry: {
            type: "MultiLineString",
            coordinates: [
              [[tailLng, tailLat], [headLng, headLat]],
              [[headLng, headLat], [leftWingLng, leftWingLat]],
              [[headLng, headLat], [rightWingLng, rightWingLat]],
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
  }, [overlay, windVectors]);

  const windFlashGeojson = useMemo(
    () => ({
      type: "FeatureCollection" as const,
      features: windArrowOverlays.map((o) => o.feature),
    }),
    [windArrowOverlays],
  );

  if (!overlay || !payload) return null;

  return (
    <>
      {overlay === "wind" ? (
        <Source id="live-weather-wind-flashes" type="geojson" data={windFlashGeojson}>
          <Layer {...windFlashGlowLayer} />
          <Layer {...windFlashCasingLayer} />
          <Layer {...windFlashStrokeLayer} />
        </Source>
      ) : null}

      {overlay === "wind"
        ? windArrowOverlays.map((o) => (
            <Marker
              key={`${o.feature.properties.stationId}:${o.feature.properties.observedOn}`}
              longitude={o.labelLng}
              latitude={o.labelLat}
              anchor="left"
            >
              <div
                style={{
                  ...LABEL_PILL_STYLE,
                  pointerEvents: "none",
                  color: "rgba(187,247,208,0.97)",
                  fontSize: 9.5,
                  lineHeight: 1,
                  letterSpacing: "0.04em",
                  whiteSpace: "nowrap",
                  fontWeight: 600,
                  fontVariantNumeric: "tabular-nums",
                  fontFamily: LABEL_FONT,
                }}
              >
                {o.speedLabel}
              </div>
            </Marker>
          ))
        : null}

      {overlay === "temperature"
        ? temperatureLabels.map((o) => (
            <Marker key={o.municipalityId} longitude={o.lng} latitude={o.lat} anchor="center">
              <div
                style={{
                  ...LABEL_PILL_STYLE,
                  pointerEvents: "none",
                  color: "rgba(232,240,247,0.98)",
                  fontSize: 12,
                  lineHeight: 1,
                  letterSpacing: "0.04em",
                  whiteSpace: "nowrap",
                  fontWeight: 700,
                  fontVariantNumeric: "tabular-nums",
                  fontFamily: LABEL_FONT,
                  opacity: 0.9 + o.temperatureSeverity * 0.1,
                }}
              >
                {o.temperatureLabel}
              </div>
            </Marker>
          ))
        : null}

      {overlay === "precipitation"
        ? precipitationLabels.map((o) => (
            <Marker key={o.municipalityId} longitude={o.lng} latitude={o.lat} anchor="center">
              <div
                style={{
                  ...LABEL_PILL_STYLE,
                  pointerEvents: "none",
                  color: "rgba(214,234,247,0.98)",
                  fontSize: 11.5,
                  lineHeight: 1,
                  letterSpacing: "0.04em",
                  whiteSpace: "nowrap",
                  fontWeight: 700,
                  fontVariantNumeric: "tabular-nums",
                  fontFamily: LABEL_FONT,
                  opacity: 0.9 + o.precipitationSeverity * 0.1,
                }}
              >
                {o.precipitationLabel}
              </div>
            </Marker>
          ))
        : null}

      {overlay === "drought"
        ? droughtLabels.map((o) => (
            <Marker key={o.municipalityId} longitude={o.lng} latitude={o.lat} anchor="center">
              <div
                style={{
                  ...LABEL_PILL_STYLE,
                  pointerEvents: "none",
                  color: o.dry ? "rgba(230,176,118,0.98)" : "rgba(150,210,196,0.98)",
                  fontSize: 11,
                  lineHeight: 1,
                  letterSpacing: "0.04em",
                  whiteSpace: "nowrap",
                  fontWeight: 700,
                  fontVariantNumeric: "tabular-nums",
                  fontFamily: LABEL_FONT,
                  opacity: 0.88 + o.intensity * 0.12,
                }}
              >
                {o.droughtLabel}
              </div>
            </Marker>
          ))
        : null}

      {overlay === "cloud"
        ? cloudLabels.map((o) => (
            <Marker key={o.municipalityId} longitude={o.lng} latitude={o.lat} anchor="center">
              <div
                style={{
                  ...LABEL_PILL_STYLE,
                  pointerEvents: "none",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  fontFamily: LABEL_FONT,
                  opacity: 0.9 + o.cloudSeverity * 0.1,
                }}
              >
                <span
                  style={{
                    fontSize: 12,
                    lineHeight: 1,
                    fontWeight: 700,
                    color: "rgba(232,240,247,0.98)",
                    fontVariantNumeric: "tabular-nums",
                    letterSpacing: "0.04em",
                  }}
                >
                  {o.cloudLabel}
                </span>
                <span
                  style={{
                    marginTop: 2,
                    fontSize: 8,
                    lineHeight: 1,
                    fontWeight: 600,
                    color: "rgba(187,247,208,0.82)",
                    letterSpacing: "0.06em",
                  }}
                >
                  {o.cloudDetailLabel}
                </span>
              </div>
            </Marker>
          ))
        : null}
    </>
  );
}
