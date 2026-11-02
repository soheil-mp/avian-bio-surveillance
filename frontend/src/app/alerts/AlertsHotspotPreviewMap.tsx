"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { LayerSpecification, StyleSpecification } from "maplibre-gl";
import MapView, { Layer, Source, type MapRef } from "react-map-gl/maplibre";

import { SATELLITE_STYLE } from "@/config/map";

type HotspotPreviewGridCell = {
  gridCellId: string;
  bboxMinLat: number;
  bboxMinLon: number;
  bboxMaxLat: number;
  bboxMaxLon: number;
  cellGeojson: unknown | null;
};

const PREVIEW_SATELLITE_STYLE: StyleSpecification = {
  ...SATELLITE_STYLE,
  name: "Alerts Hotspot Satellite Preview",
  layers: SATELLITE_STYLE.layers.filter((layer) => layer.type !== "symbol"),
};

const PREVIEW_MASK_LAYER: LayerSpecification = {
  id: "hotspot-preview-mask",
  type: "fill",
  source: "hotspot-preview-mask",
  paint: {
    "fill-color": "rgba(12,12,12,0.96)",
    "fill-opacity": 0.92,
  },
};

const PREVIEW_OUTLINE_LAYER: LayerSpecification = {
  id: "hotspot-preview-outline",
  type: "line",
  source: "hotspot-preview-cell",
  paint: {
    "line-color": "rgba(255,255,255,0.74)",
    "line-width": 1.4,
    "line-opacity": 0.84,
  },
};

type PreviewLinearRing = Array<[number, number]>;
type PreviewPolygonRings = Array<PreviewLinearRing>;

function bboxPolygonGeometry(cell: HotspotPreviewGridCell): GeoJSON.Polygon {
  return {
    type: "Polygon",
    coordinates: [[
      [cell.bboxMinLon, cell.bboxMinLat],
      [cell.bboxMaxLon, cell.bboxMinLat],
      [cell.bboxMaxLon, cell.bboxMaxLat],
      [cell.bboxMinLon, cell.bboxMaxLat],
      [cell.bboxMinLon, cell.bboxMinLat],
    ]],
  };
}

function resolveCellGeometry(cell: HotspotPreviewGridCell): GeoJSON.Geometry {
  const raw = cell.cellGeojson;

  if (raw && typeof raw === "object" && "type" in raw) {
    const candidate = raw as {
      type: string;
      geometry?: GeoJSON.Geometry;
      features?: Array<{ geometry?: GeoJSON.Geometry }>;
    };

    if (candidate.type === "Polygon" || candidate.type === "MultiPolygon") {
      return candidate as unknown as GeoJSON.Geometry;
    }

    if (candidate.type === "Feature" && candidate.geometry) {
      return candidate.geometry;
    }

    if (candidate.type === "FeatureCollection" && Array.isArray(candidate.features)) {
      const firstGeometry = candidate.features[0]?.geometry;
      if (firstGeometry) return firstGeometry;
    }
  }

  return bboxPolygonGeometry(cell);
}

function visitCoordinates(
  coordinates: unknown,
  visitor: (lng: number, lat: number) => void,
) {
  if (!Array.isArray(coordinates) || coordinates.length === 0) return;

  if (
    coordinates.length >= 2 &&
    typeof coordinates[0] === "number" &&
    typeof coordinates[1] === "number"
  ) {
    visitor(coordinates[0], coordinates[1]);
    return;
  }

  for (const child of coordinates) {
    visitCoordinates(child, visitor);
  }
}

function closeLinearRing(ring: PreviewLinearRing): PreviewLinearRing {
  if (ring.length === 0) return ring;

  const [firstLng, firstLat] = ring[0];
  const [lastLng, lastLat] = ring[ring.length - 1];

  if (firstLng === lastLng && firstLat === lastLat) {
    return ring;
  }

  return [...ring, [firstLng, firstLat]];
}

function ringArea(ring: PreviewLinearRing): number {
  if (ring.length < 3) return 0;

  let area = 0;
  for (let index = 0; index < ring.length; index += 1) {
    const [x1, y1] = ring[index];
    const [x2, y2] = ring[(index + 1) % ring.length];
    area += x1 * y2 - x2 * y1;
  }

  return Math.abs(area) / 2;
}

function normalizeLinearRing(value: unknown): PreviewLinearRing {
  if (!Array.isArray(value)) return [];

  const ring = value.filter(
    (point): point is [number, number] =>
      Array.isArray(point) &&
      point.length >= 2 &&
      typeof point[0] === "number" &&
      Number.isFinite(point[0]) &&
      typeof point[1] === "number" &&
      Number.isFinite(point[1]),
  );

  return ring.length >= 3 ? closeLinearRing(ring) : [];
}

function resolvePolygonRings(geometry: GeoJSON.Geometry): PreviewPolygonRings | null {
  if (geometry.type === "Polygon") {
    const rings = geometry.coordinates
      .map((ring) => normalizeLinearRing(ring))
      .filter((ring) => ring.length >= 4);

    return rings.length > 0 ? rings : null;
  }

  if (geometry.type === "MultiPolygon") {
    const polygons = geometry.coordinates
      .map((polygon) => polygon
        .map((ring) => normalizeLinearRing(ring))
        .filter((ring) => ring.length >= 4))
      .filter((rings) => rings.length > 0);

    if (polygons.length === 0) return null;

    return [...polygons].sort((left, right) => ringArea(right[0]) - ringArea(left[0]))[0] ?? null;
  }

  if (geometry.type === "GeometryCollection") {
    for (const member of geometry.geometries) {
      const rings = resolvePolygonRings(member);
      if (rings) return rings;
    }
  }

  return null;
}

function buildPreviewMaskFeatureCollection(
  geometry: GeoJSON.Geometry,
  bounds: readonly [number, number, number, number],
): GeoJSON.FeatureCollection<GeoJSON.Geometry, GeoJSON.GeoJsonProperties> | null {
  const rings = resolvePolygonRings(geometry);
  if (!rings) return null;

  const lngPadding = Math.max((bounds[2] - bounds[0]) * 3, 0.06);
  const latPadding = Math.max((bounds[3] - bounds[1]) * 3, 0.04);
  const outerRing = closeLinearRing([
    [bounds[0] - lngPadding, bounds[1] - latPadding],
    [bounds[2] + lngPadding, bounds[1] - latPadding],
    [bounds[2] + lngPadding, bounds[3] + latPadding],
    [bounds[0] - lngPadding, bounds[3] + latPadding],
  ]);

  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: {
          type: "Polygon",
          coordinates: [outerRing, ...rings],
        },
        properties: {},
      },
    ],
  };
}

function geometryBounds(
  geometry: GeoJSON.Geometry | null | undefined,
): readonly [number, number, number, number] | null {
  if (!geometry) return null;

  if (geometry.type === "GeometryCollection") {
    const memberBounds: Array<readonly [number, number, number, number]> = geometry.geometries
      .map((member) => geometryBounds(member))
      .filter((value): value is readonly [number, number, number, number] => value !== null);

    if (memberBounds.length === 0) return null;

    return memberBounds.reduce(
      (accumulator: readonly [number, number, number, number], current: readonly [number, number, number, number]) => [
        Math.min(accumulator[0], current[0]),
        Math.min(accumulator[1], current[1]),
        Math.max(accumulator[2], current[2]),
        Math.max(accumulator[3], current[3]),
      ] as const,
    );
  }

  let minLng = Number.POSITIVE_INFINITY;
  let minLat = Number.POSITIVE_INFINITY;
  let maxLng = Number.NEGATIVE_INFINITY;
  let maxLat = Number.NEGATIVE_INFINITY;

  visitCoordinates(geometry.coordinates, (lng, lat) => {
    minLng = Math.min(minLng, lng);
    minLat = Math.min(minLat, lat);
    maxLng = Math.max(maxLng, lng);
    maxLat = Math.max(maxLat, lat);
  });

  if (![minLng, minLat, maxLng, maxLat].every(Number.isFinite)) {
    return null;
  }

  return [minLng, minLat, maxLng, maxLat] as const;
}

export default function AlertsHotspotPreviewMap({
  gridCell,
}: {
  gridCell: HotspotPreviewGridCell;
}) {
  const mapRef = useRef<MapRef | null>(null);
  const [loaded, setLoaded] = useState(false);

  const geometry = useMemo(() => resolveCellGeometry(gridCell), [gridCell]);
  const bounds = useMemo<readonly [number, number, number, number]>(
    () => geometryBounds(geometry) ?? [
      gridCell.bboxMinLon,
      gridCell.bboxMinLat,
      gridCell.bboxMaxLon,
      gridCell.bboxMaxLat,
    ],
    [geometry, gridCell],
  );

  const featureCollection = useMemo<
    GeoJSON.FeatureCollection<GeoJSON.Geometry, { gridCellId: string }>
  >(
    () => ({
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry,
          properties: { gridCellId: gridCell.gridCellId },
        },
      ],
    }),
    [geometry, gridCell.gridCellId],
  );

  const maskFeatureCollection = useMemo(
    () => buildPreviewMaskFeatureCollection(geometry, bounds),
    [bounds, geometry],
  );

  useEffect(() => {
    if (!loaded) return;

    mapRef.current?.fitBounds(
      [
        [bounds[0], bounds[1]],
        [bounds[2], bounds[3]],
      ],
      {
        padding: 12,
        duration: 0,
        maxZoom: 16,
      },
    );
  }, [bounds, loaded]);

  return (
    <div className="h-[132px] w-full max-w-[220px] overflow-hidden rounded-lg" style={{ border: "1px solid rgba(255,255,255,0.10)" }}>
      <MapView
        ref={mapRef}
        initialViewState={{
          longitude: (bounds[0] + bounds[2]) / 2,
          latitude: (bounds[1] + bounds[3]) / 2,
          zoom: 13,
          bearing: 0,
          pitch: 0,
        }}
        mapStyle={PREVIEW_SATELLITE_STYLE}
        attributionControl={false}
        reuseMaps
        interactive={false}
        dragPan={false}
        dragRotate={false}
        scrollZoom={false}
        boxZoom={false}
        doubleClickZoom={false}
        keyboard={false}
        touchZoomRotate={false}
        touchPitch={false}
        onLoad={() => setLoaded(true)}
        style={{ width: "100%", height: "100%" }}
      >
        {maskFeatureCollection ? (
          <Source id="hotspot-preview-mask" type="geojson" data={maskFeatureCollection}>
            <Layer {...PREVIEW_MASK_LAYER} />
          </Source>
        ) : null}

        <Source id="hotspot-preview-cell" type="geojson" data={featureCollection}>
          <Layer {...PREVIEW_OUTLINE_LAYER} />
        </Source>
      </MapView>
    </div>
  );
}