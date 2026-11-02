"use client";

import {
  DEFAULT_ZOOM,
  NETHERLANDS_BOUNDS,
  NETHERLANDS_CENTER,
} from "@/config/map";
import type { LayerSpecification, StyleSpecification } from "maplibre-gl";
import {
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
} from "react";
import MapView, { Layer, Source, type MapRef } from "react-map-gl/maplibre";
import type { HotspotCellSummary } from "./hotspot";

type HotspotMapGridCell = {
  gridCellId: string;
  bboxMinLat: number;
  bboxMinLon: number;
  bboxMaxLat: number;
  bboxMaxLon: number;
  cellGeojson: unknown | null;
};

type HotspotMapProps = {
  gridCells: HotspotMapGridCell[];
  hotspotCells: HotspotCellSummary[];
  selectedGridCellId: string | null;
  onSelectCell: (gridCellId: string) => void;
};

type HotspotSignal = "base" | "watch" | "critical";

type HotspotMapFeatureProperties = {
  gridCellId: string;
  signal: HotspotSignal;
  displayScore: number;
};

type BaseGridFeatureProperties = {
  gridCellId: string;
};

type MapMouseEvent = Parameters<
  NonNullable<ComponentProps<typeof MapView>["onMouseMove"]>
>[0];

type MapClickEvent = Parameters<
  NonNullable<ComponentProps<typeof MapView>["onClick"]>
>[0];

const HOTSPOT_STYLE: StyleSpecification = {
  version: 8,
  name: "Alerts Hotspot Light",
  sources: {},
  layers: [
    {
      id: "alerts-hotspot-background",
      type: "background",
      paint: {
        "background-color": "#171717",
      },
    },
  ],
};

const HOTSPOT_INITIAL_ZOOM = DEFAULT_ZOOM;
const HOTSPOT_FIT_MAX_ZOOM = 5.45;
const HOTSPOT_MIN_ZOOM = 4.25;
const HOTSPOT_MAX_ZOOM = 9.75;
const HOTSPOT_ZOOM_STEP = 0.55;
const HOTSPOT_CAMERA_BUFFER_LNG = 0.45;
const HOTSPOT_CAMERA_BUFFER_LAT = 0.32;
const HOTSPOT_PAN_BUFFER_LNG = 0.45;
const HOTSPOT_PAN_BUFFER_LAT = 0.32;

const EMPTY_BASE_GRID: GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  BaseGridFeatureProperties
> = {
  type: "FeatureCollection",
  features: [],
};

const EMPTY_HOTSPOT_GRID: GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  HotspotMapFeatureProperties
> = {
  type: "FeatureCollection",
  features: [],
};

const countryMaskLayer: LayerSpecification = {
  id: "alerts-country-mask",
  type: "fill",
  source: "country-mask",
  paint: {
    "fill-color": "rgba(10,10,10,1)",
    "fill-opacity": 0.88,
  },
};


const gridBaseFillLayer: LayerSpecification = {
  id: "alerts-grid-base-fill",
  type: "fill",
  source: "hotspot-grid-base",
  paint: {
    "fill-color": "rgba(36,36,36,1)",
    "fill-opacity": 0.5,
  },
};

const gridBaseLineLayer: LayerSpecification = {
  id: "alerts-grid-base-line",
  type: "line",
  source: "hotspot-grid-base",
  paint: {
    "line-color": "rgba(88,88,88,0.7)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6.2,
      0.28,
      8.4,
      0.48,
      10,
      0.74,
    ],
    "line-opacity": 0.72,
  },
};

const watchGlowLayer: LayerSpecification = {
  id: "alerts-grid-watch-glow",
  type: "line",
  source: "hotspot-grid-hotspots",
  filter: ["==", ["get", "signal"], "watch"],
  paint: {
    "line-color": "rgba(250,204,21,0.3)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6.2,
      3.8,
      8.4,
      5.6,
      10,
      7.2,
    ],
    "line-blur": 3.3,
    "line-opacity": 0.22,
  },
};

const criticalGlowLayer: LayerSpecification = {
  id: "alerts-grid-critical-glow",
  type: "line",
  source: "hotspot-grid-hotspots",
  filter: ["==", ["get", "signal"], "critical"],
  paint: {
    "line-color": "rgba(138,52,58,0.36)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6.2,
      4.6,
      8.4,
      6.8,
      10,
      8.6,
    ],
    "line-blur": 4.1,
    "line-opacity": 0.24,
  },
};

const watchFillLayer: LayerSpecification = {
  id: "alerts-grid-watch-fill",
  type: "fill",
  source: "hotspot-grid-hotspots",
  filter: ["==", ["get", "signal"], "watch"],
  paint: {
    "fill-color": "rgba(250,204,21,1)",
    "fill-opacity": [
      "interpolate",
      ["linear"],
      ["get", "displayScore"],
      0.34,
      0.14,
      0.5,
      0.24,
    ],
  },
};

const criticalFillLayer: LayerSpecification = {
  id: "alerts-grid-critical-fill",
  type: "fill",
  source: "hotspot-grid-hotspots",
  filter: ["==", ["get", "signal"], "critical"],
  paint: {
    "fill-color": "rgba(138,52,58,1)",
    "fill-opacity": [
      "interpolate",
      ["linear"],
      ["get", "displayScore"],
      0.5,
      0.2,
      0.8,
      0.34,
    ],
  },
};

const watchLineLayer: LayerSpecification = {
  id: "alerts-grid-watch-line",
  type: "line",
  source: "hotspot-grid-hotspots",
  filter: ["==", ["get", "signal"], "watch"],
  paint: {
    "line-color": "rgba(252,211,77,0.96)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6.2,
      0.84,
      8.4,
      1.14,
      10,
      1.52,
    ],
    "line-opacity": 0.94,
  },
};

const criticalLineLayer: LayerSpecification = {
  id: "alerts-grid-critical-line",
  type: "line",
  source: "hotspot-grid-hotspots",
  filter: ["==", ["get", "signal"], "critical"],
  paint: {
    "line-color": "rgba(252,165,165,0.98)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6.2,
      0.98,
      8.4,
      1.34,
      10,
      1.72,
    ],
    "line-opacity": 0.98,
  },
};

const hoveredFillLayer: LayerSpecification = {
  id: "alerts-grid-hovered-fill",
  type: "fill",
  source: "hotspot-grid-hovered",
  paint: {
    "fill-color": "rgba(92,92,92,1)",
    "fill-opacity": 0.12,
  },
};

const hoveredLineLayer: LayerSpecification = {
  id: "alerts-grid-hovered-line",
  type: "line",
  source: "hotspot-grid-hovered",
  paint: {
    "line-color": "rgba(152,152,152,0.9)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6.2,
      1.08,
      8.4,
      1.44,
      10,
      1.76,
    ],
    "line-opacity": 0.95,
  },
};

const selectedGlowLayer: LayerSpecification = {
  id: "alerts-grid-selected-glow",
  type: "line",
  source: "hotspot-grid-selected",
  paint: {
    "line-color": [
      "case",
      ["==", ["get", "signal"], "critical"],
      "rgba(254,202,202,0.72)",
      ["==", ["get", "signal"], "watch"],
      "rgba(254,240,138,0.72)",
      "rgba(150,150,150,0.54)",
    ],
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6.2,
      5.6,
      8.4,
      7.4,
      10,
      9.2,
    ],
    "line-blur": 4.8,
    "line-opacity": 0.26,
  },
};

const selectedFillLayer: LayerSpecification = {
  id: "alerts-grid-selected-fill",
  type: "fill",
  source: "hotspot-grid-selected",
  paint: {
    "fill-color": [
      "case",
      ["==", ["get", "signal"], "critical"],
      "rgba(138,52,58,1)",
      ["==", ["get", "signal"], "watch"],
      "rgba(250,204,21,1)",
      "rgba(98,98,98,1)",
    ],
    "fill-opacity": [
      "case",
      ["==", ["get", "signal"], "critical"],
      0.18,
      ["==", ["get", "signal"], "watch"],
      0.14,
      0.08,
    ],
  },
};

const selectedLineLayer: LayerSpecification = {
  id: "alerts-grid-selected-line",
  type: "line",
  source: "hotspot-grid-selected",
  paint: {
    "line-color": [
      "case",
      ["==", ["get", "signal"], "critical"],
      "rgba(254,226,226,0.98)",
      ["==", ["get", "signal"], "watch"],
      "rgba(254,243,199,0.98)",
      "rgba(174,174,174,0.94)",
    ],
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6.2,
      1.24,
      8.4,
      1.82,
      10,
      2.24,
    ],
    "line-opacity": 1,
  },
};

const hotspotHitboxLayer: LayerSpecification = {
  id: "alerts-grid-hitbox",
  type: "fill",
  source: "hotspot-grid-hotspots",
  paint: {
    "fill-color": "rgba(255,255,255,1)",
    "fill-opacity": 0.001,
  },
};

function hotspotSignal(score: number): HotspotSignal {
  if (score >= 0.5) return "critical";
  if (score >= 0.34) return "watch";
  return "base";
}

function bboxPolygonGeometry(cell: HotspotMapGridCell): GeoJSON.Polygon {
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

function resolveCellGeometry(cell: HotspotMapGridCell): GeoJSON.Geometry {
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

function buildHotspotFeature(
  summary: HotspotCellSummary,
  geometry: GeoJSON.Geometry,
): GeoJSON.Feature<GeoJSON.Geometry, HotspotMapFeatureProperties> {
  return {
    type: "Feature",
    geometry,
    properties: {
      gridCellId: summary.gridCellId,
      signal: hotspotSignal(summary.displayScore),
      displayScore: summary.displayScore,
    },
  };
}

function geometryCenter(
  geometry: GeoJSON.Geometry | null | undefined,
): { latitude: number; longitude: number } | null {
  const bounds = geometryBounds(geometry);
  if (!bounds) return null;

  return {
    longitude: (bounds[0] + bounds[2]) / 2,
    latitude: (bounds[1] + bounds[3]) / 2,
  };
}

function extendBounds(
  bounds: [number, number, number, number],
  lngBuffer: number,
  latBuffer: number,
): [number, number, number, number] {
  return [
    bounds[0] - lngBuffer,
    bounds[1] - latBuffer,
    bounds[2] + lngBuffer,
    bounds[3] + latBuffer,
  ];
}

function visitCoordinates(
  coordinates: unknown,
  visitor: (lng: number, lat: number) => void,
) {
  if (!Array.isArray(coordinates) || coordinates.length === 0) return;

  const first = coordinates[0];
  if (
    coordinates.length >= 2 &&
    typeof coordinates[0] === "number" &&
    typeof coordinates[1] === "number"
  ) {
    visitor(coordinates[0], coordinates[1]);
    return;
  }

  if (Array.isArray(first)) {
    for (const child of coordinates) {
      visitCoordinates(child, visitor);
    }
  }
}

function geometryBounds(geometry: GeoJSON.Geometry | null | undefined) {
  if (!geometry) return null;

  let minLng = Number.POSITIVE_INFINITY;
  let minLat = Number.POSITIVE_INFINITY;
  let maxLng = Number.NEGATIVE_INFINITY;
  let maxLat = Number.NEGATIVE_INFINITY;

  visitCoordinates((geometry as { coordinates?: unknown }).coordinates, (lng, lat) => {
    minLng = Math.min(minLng, lng);
    minLat = Math.min(minLat, lat);
    maxLng = Math.max(maxLng, lng);
    maxLat = Math.max(maxLat, lat);
  });

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

const AlertsHotspotMap = memo(function AlertsHotspotMap({
  gridCells,
  hotspotCells,
  selectedGridCellId,
  onSelectCell,
}: HotspotMapProps) {
  const mapRef = useRef<MapRef>(null);
  const hoveredGridCellIdRef = useRef<string | null>(null);
  const [hoveredGridCellId, setHoveredGridCellId] = useState<string | null>(null);

  const hotspotCellById = useMemo(
    () => new Map(hotspotCells.map((cell) => [cell.gridCellId, cell])),
    [hotspotCells],
  );
  const gridCellById = useMemo(
    () => new Map(gridCells.map((cell) => [cell.gridCellId, cell])),
    [gridCells],
  );
  const gridGeometryById = useMemo(
    () => new Map(gridCells.map((cell) => [cell.gridCellId, resolveCellGeometry(cell)])),
    [gridCells],
  );
  const countryFrameBounds = NETHERLANDS_BOUNDS;
  const cameraBounds = useMemo(
    () => extendBounds(countryFrameBounds, HOTSPOT_CAMERA_BUFFER_LNG, HOTSPOT_CAMERA_BUFFER_LAT),
    [countryFrameBounds],
  );
  const maxCameraBounds = useMemo(
    () => extendBounds(cameraBounds, HOTSPOT_PAN_BUFFER_LNG, HOTSPOT_PAN_BUFFER_LAT),
    [cameraBounds],
  );

  const fitMapToFrame = () => {
    const map = mapRef.current?.getMap();
    if (!map) return;

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
        maxZoom: HOTSPOT_FIT_MAX_ZOOM,
      },
    );
  };

  const handleMapLoad = () => {
    fitMapToFrame();

    // Run one more fit after layout settles so the map uses the real panel size.
    requestAnimationFrame(() => {
      fitMapToFrame();
    });

    window.setTimeout(() => {
      fitMapToFrame();
    }, 120);
  };

  const syncHoveredCell = (nextGridCellId: string | null) => {
    if (hoveredGridCellIdRef.current === nextGridCellId) return;

    hoveredGridCellIdRef.current = nextGridCellId;
    setHoveredGridCellId(nextGridCellId);

    if (mapRef.current) {
      mapRef.current.getCanvas().style.cursor = nextGridCellId ? "pointer" : "";
    }
  };

  useEffect(() => {
    const currentHoveredId = hoveredGridCellIdRef.current;
    if (currentHoveredId && !hotspotCellById.has(currentHoveredId)) {
      syncHoveredCell(null);
    }
  }, [hotspotCellById]);

  const baseGridGeojson = useMemo<
    GeoJSON.FeatureCollection<GeoJSON.Geometry, BaseGridFeatureProperties>
  >(() => {
    if (gridCells.length === 0) return EMPTY_BASE_GRID;

    return {
      type: "FeatureCollection",
      features: gridCells.map((cell) => ({
        type: "Feature",
        geometry: gridGeometryById.get(cell.gridCellId) ?? bboxPolygonGeometry(cell),
        properties: {
          gridCellId: cell.gridCellId,
        },
      })),
    };
  }, [gridCells, gridGeometryById]);

  const hotspotGridGeojson = useMemo<
    GeoJSON.FeatureCollection<GeoJSON.Geometry, HotspotMapFeatureProperties>
  >(() => {
    if (hotspotCells.length === 0) return EMPTY_HOTSPOT_GRID;

    const features: Array<GeoJSON.Feature<GeoJSON.Geometry, HotspotMapFeatureProperties>> = [];

    for (const summary of hotspotCells) {
      const geometry = gridGeometryById.get(summary.gridCellId);
      if (!geometry) continue;

      features.push(
        buildHotspotFeature(
          summary,
          geometry,
        ),
      );
    }

    return {
      type: "FeatureCollection",
      features,
    };
  }, [gridGeometryById, hotspotCells]);

  const selectedGridGeojson = useMemo<
    GeoJSON.FeatureCollection<GeoJSON.Geometry, HotspotMapFeatureProperties>
  >(() => {
    if (!selectedGridCellId) return EMPTY_HOTSPOT_GRID;

    const summary = hotspotCellById.get(selectedGridCellId);
    const geometry = gridGeometryById.get(selectedGridCellId);
    if (!summary || !geometry) return EMPTY_HOTSPOT_GRID;

    return {
      type: "FeatureCollection",
      features: [
        buildHotspotFeature(
          summary,
          geometry,
        ),
      ],
    };
  }, [gridGeometryById, hotspotCellById, selectedGridCellId]);

  const hoveredGridGeojson = useMemo<
    GeoJSON.FeatureCollection<GeoJSON.Geometry, HotspotMapFeatureProperties>
  >(() => {
    if (!hoveredGridCellId || hoveredGridCellId === selectedGridCellId) {
      return EMPTY_HOTSPOT_GRID;
    }

    const summary = hotspotCellById.get(hoveredGridCellId);
    const geometry = gridGeometryById.get(hoveredGridCellId);
    if (!summary || !geometry) return EMPTY_HOTSPOT_GRID;

    return {
      type: "FeatureCollection",
      features: [
        buildHotspotFeature(
          summary,
          geometry,
        ),
      ],
    };
  }, [gridGeometryById, hotspotCellById, hoveredGridCellId, selectedGridCellId]);

  const handleMouseMove = (event: MapMouseEvent) => {
    const feature = event.features?.[0];
    const gridCellId = typeof feature?.properties?.gridCellId === "string"
      ? feature.properties.gridCellId
      : null;

    if (!gridCellId || !hotspotCellById.has(gridCellId)) {
      syncHoveredCell(null);
      return;
    }

    syncHoveredCell(gridCellId);
  };

  const handleMouseLeave = () => {
    syncHoveredCell(null);
  };

  const handleClick = (event: MapClickEvent) => {
    const feature = event.features?.[0];
    const gridCellId = typeof feature?.properties?.gridCellId === "string"
      ? feature.properties.gridCellId
      : null;

    if (!gridCellId || !hotspotCellById.has(gridCellId)) return;
    onSelectCell(gridCellId);
  };

  const handleZoom = (direction: 1 | -1) => {
    const map = mapRef.current?.getMap();
    if (!map) return;

    const nextZoom = Math.min(
      HOTSPOT_MAX_ZOOM,
      Math.max(HOTSPOT_MIN_ZOOM, map.getZoom() + direction * HOTSPOT_ZOOM_STEP),
    );

    map.easeTo({
      zoom: nextZoom,
      duration: 180,
    });
  };

  return (
    <div
      className="relative flex min-h-0 flex-1 overflow-hidden rounded-xl"
      style={{
        background: "#171717",
        border: "1px solid rgba(255,255,255,0.08)",
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.05), 0 20px 44px rgba(0,0,0,0.16)",
      }}
    >
      <MapView
        ref={mapRef}
        reuseMaps
        renderWorldCopies={false}
        initialViewState={{
          longitude: NETHERLANDS_CENTER.lng,
          latitude: NETHERLANDS_CENTER.lat,
          zoom: HOTSPOT_INITIAL_ZOOM,
        }}
        mapStyle={HOTSPOT_STYLE}
        attributionControl={false}
        dragRotate={false}
        pitchWithRotate={false}
        touchZoomRotate={false}
        scrollZoom
        doubleClickZoom={false}
        interactiveLayerIds={["alerts-grid-hitbox"]}
        maxBounds={maxCameraBounds}
        minZoom={HOTSPOT_MIN_ZOOM}
        maxZoom={HOTSPOT_MAX_ZOOM}
        onLoad={handleMapLoad}
        onResize={fitMapToFrame}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        style={{ width: "100%", height: "100%" }}
      >
        <Source id="hotspot-grid-base" type="geojson" data={baseGridGeojson}>
          <Layer {...gridBaseFillLayer} />
          <Layer {...gridBaseLineLayer} />
        </Source>

        <Source id="hotspot-grid-hotspots" type="geojson" data={hotspotGridGeojson}>
          <Layer {...watchGlowLayer} />
          <Layer {...criticalGlowLayer} />
          <Layer {...watchFillLayer} />
          <Layer {...criticalFillLayer} />
          <Layer {...hotspotHitboxLayer} />
          <Layer {...watchLineLayer} />
          <Layer {...criticalLineLayer} />
        </Source>
      </MapView>

      <div className="absolute bottom-3 right-3 z-10 flex flex-col gap-1.5">
        <button
          type="button"
          aria-label="Zoom in hotspot map"
          onClick={() => handleZoom(1)}
          className="flex h-7 w-7 items-center justify-center rounded-md"
          style={{
            background: "transparent",
            border: "none",
            boxShadow: "none",
          }}
        >
          <span className="relative block h-3.5 w-3.5">
            <span
              className="absolute left-1/2 top-1/2 h-px w-3.5 -translate-x-1/2 -translate-y-1/2"
              style={{ background: "rgba(150,150,150,0.9)" }}
            />
            <span
              className="absolute left-1/2 top-1/2 h-3.5 w-px -translate-x-1/2 -translate-y-1/2"
              style={{ background: "rgba(150,150,150,0.9)" }}
            />
          </span>
        </button>
        <button
          type="button"
          aria-label="Zoom out hotspot map"
          onClick={() => handleZoom(-1)}
          className="flex h-7 w-7 items-center justify-center rounded-md"
          style={{
            background: "transparent",
            border: "none",
            boxShadow: "none",
          }}
        >
          <span className="relative block h-3.5 w-3.5">
            <span
              className="absolute left-1/2 top-1/2 h-px w-3.5 -translate-x-1/2 -translate-y-1/2"
              style={{ background: "rgba(150,150,150,0.9)" }}
            />
          </span>
        </button>
      </div>
    </div>
  );
});

AlertsHotspotMap.displayName = "AlertsHotspotMap";

export default AlertsHotspotMap;
