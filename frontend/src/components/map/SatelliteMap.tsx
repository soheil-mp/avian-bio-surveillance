"use client";

import {
    EARTH_ENGINE_OVERLAY_ORDER,
    type EarthEngineOverlayId,
    type EarthEngineVisualizationMode,
} from "@/config/earth-engine-overlays";
  import {
    MAP_SKIN_META,
    getMapSkinStyle,
    type MapSkinId,
  } from "@/config/map-skins";
import {
    DEFAULT_BEARING,
    DEFAULT_PITCH,
    DEFAULT_ZOOM,
    NETHERLANDS_BOUNDS,
    NETHERLANDS_CENTER,
} from "@/config/map";

import {
    STATION_TYPE_OPTIONS,
    getStationTypeMeta,
    matchesStationTypeFilter,
} from "@/config/stations";
import type {
  HotspotGrain,
  HotspotSource,
  HotspotSummaryResponse,
} from "@/app/alerts/hotspot-summary";
import {
    buildEarthEngineOverlayBundle,
    type EarthEngineOverlayBundle,
    type EarthEngineOverlayResponse,
    type OverlayFeature,
    type OverlayFeatureCollection,
    type OverlayRegionCollection,
} from "@/lib/earth-engine-overlays";
import { GridCellPopup, PointSatelliteThumb } from "@/components/map/GridCellPopup";
import type { GridCellOverviewResponse } from "@/app/api/grid-cell/overview/route";
import { bbox } from "@turf/bbox";
import { booleanPointInPolygon } from "@turf/boolean-point-in-polygon";
import { centroid } from "@turf/centroid";
import { featureCollection, point as turfPoint } from "@turf/helpers";
import { intersect } from "@turf/intersect";
import type { LayerSpecification } from "maplibre-gl";
import {
    forwardRef,
    useCallback,
    useEffect,
    useImperativeHandle,
    useMemo,
    useRef,
    useState,
} from "react";
import Map, {
    Layer,
    Marker,
    Popup,
    Source,
    type MapRef,
    type ViewStateChangeEvent
} from "react-map-gl/maplibre";
import { buildHotspotOverlayFeatureCollection } from "./hotspot-overlay";
import { WeatherOverlay, type LiveWeatherOverlay } from "./WeatherOverlay";

export interface MapViewState {
  latitude: number;
  longitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}

export interface TopSpecies {
  name: string;
  count: number;
  thumbnail_url: string | null;
}

export interface StationStats {
  total_detections: number;
  unique_species: number;
  unique_days: number;
  first_detection: string;
  last_detection: string;
  avg_confidence: number;
  monthly_detections?: Record<string, number>;
  top_species?: TopSpecies[];
  high_confidence_detections?: number;
  detections_per_day?: number;
  coverage_span_days?: number;
  lifetime_availability_pct?: number;
  availability_90d_pct?: number;
  days_since_last?: number | null;
  station_status?: string;
  gee_ndvi?: number | null;
  gee_elevation_m?: number | null;
  gee_land_cover?: string | null;
}

function parseJsonValue<T>(value: unknown): T | null {
  if (typeof value !== "string") return (value as T) ?? null;
  try {
    return JSON.parse(value) as T;
  } catch {
    return null;
  }
}

function normalizeMonthlyDetections(
  value: unknown,
): Record<string, number> | undefined {
  const parsed = parseJsonValue<Record<string, unknown>>(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return undefined;
  }

  const normalized = Object.fromEntries(
    Object.entries(parsed).flatMap(([month, count]) => {
      const numeric = Number(count);
      return Number.isFinite(numeric) ? [[month, numeric]] : [];
    }),
  );

  return Object.keys(normalized).length > 0 ? normalized : undefined;
}

function normalizeStationStats(value: unknown): StationStats {
  const parsed = parseJsonValue<Record<string, unknown>>(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {} as StationStats;
  }

  return {
    ...(parsed as unknown as StationStats),
    monthly_detections: normalizeMonthlyDetections(parsed.monthly_detections),
  };
}

export interface Station {
  id: string;
  name: string;
  type: string;
  country: string;
  state: string;
  coords: { lat: number; lon: number };
  stats: StationStats;
  in_database?: boolean;
}

interface CanonicalGridCell {
  grid_cell_id: string;
  grid_system: string;
  lat: number;
  lon: number;
  bbox_min_lat: number;
  bbox_min_lon: number;
  bbox_max_lat: number;
  bbox_max_lon: number;
  grid_size_km: number;
  cell_geojson?: {
    type: string;
    coordinates: unknown;
  } | null;
  station_count: number;
  total_detections: number;
}

interface CanonicalGridResponse {
  geometryClippedToCountry?: boolean;
  cells?: CanonicalGridCell[];
}

interface GridOverlayBundle {
  anchorLabels: GeoJSON.FeatureCollection | null;
  detailedLabels: GeoJSON.FeatureCollection | null;
  intersections: GeoJSON.FeatureCollection | null;
  subGrid: GeoJSON.FeatureCollection | null;
}

interface GridOverlayBuildOptions {
  includeAnchorLabels: boolean;
  includeDetailedLabels: boolean;
  includeIntersections: boolean;
  includeSubGrid: boolean;
}

interface GridCellLookupEntry {
  latitude: number;
  longitude: number;
  gridSystem: string;
  gridSizeKm: number | null;
  stationCount: number;
  totalDetections: number;
}

interface SelectedGridCell {
  gridCellId: string;
  referenceLabel: string;
  latitude: number;
  longitude: number;
  gridSystem: string;
  gridSizeKm: number | null;
  stationCount: number;
  totalDetections: number;
}

type HoverInfo = {
  x: number;
  y: number;
  name: string;
  type: "province" | "municipality" | "grid";
  meta?: string | null;
};

/** Properties on each individual bird track feature */
interface MovebankTrackProps {
  individual: string;
  species: string;
  study_id: string;
  start_ts: string;
  end_ts: string;
  point_count: number;
  raw_point_count: number;
  start_point: [number, number];
  end_point: [number, number];
}

interface SatelliteMapProps {
  onViewStateChange?: (vs: MapViewState) => void;
  activeStationTypes?: string[];
  activeSpecies?: string[];
  minQuality?: number;
  showStations?: boolean;
  focusedStationId?: string | null;
  showMovebank?: boolean;
  movebankSpeciesFilter?: string[];
  movebankWindowDays?: number | null;
  skin?: MapSkinId;
  earthEngineOverlay?: EarthEngineOverlayId | null;
  spatialPartitionMode?: EarthEngineVisualizationMode;
  hotspotSource?: HotspotSource | null;
  /**
   * Grain the hotspot overlay is drawn at. Decoupled from
   * `spatialPartitionMode` so the overlay can use the grain that actually
   * has data for the chosen source (e.g. bioacoustic only exists at grid).
   */
  hotspotGrain?: HotspotGrain;
  /** Single week to pin the overlay to ("latest" or ISO date). */
  hotspotWeek?: string;
  /** Live weather overlay mode (over the satellite basemap). */
  weatherOverlay?: LiveWeatherOverlay | null;
  /** Reports the weeks/selection the API resolved, so the UI can build a picker. */
  onHotspotMetaChange?: (meta: {
    availableWeeks: string[];
    selectedWeek: string | null;
  }) => void;
  onStationsLoaded?: (stations: Station[]) => void;
  /** Called once tracks load with the list of species present in the data */
  onTracksLoaded?: (species: string[]) => void;
  /** Reports the selected station id (popup open) or null when it closes. */
  onStationSelect?: (stationId: string | null) => void;
  /** Reports the selected grid cell id (popup open) or null when it closes. */
  onGridSelect?: (gridCellId: string | null) => void;
  /** Fly the camera to this point (e.g. a feed detail); null restores the prior view. */
  flyFocus?: { lat: number; lon: number } | null;
}

const DEFAULT_GEE_DISPLAY_GRID_SIZE_KM = 5;

/* ── Species → color palette for Movebank animated tracks ────── */
export const MOVEBANK_TRACK_COLORS: Record<string, string> = {
  "Larus fuscus": "#f59e0b",
  "Ciconia ciconia": "#ef4444",
  "Larus argentatus": "#3b82f6",
  "Chroicocephalus ridibundus": "#a855f7",
  "Grus grus": "#ec4899",
  "Milvus milvus": "#f97316",
  "Gyps africanus": "#14b8a6",
  "Buteo jamaicensis": "#84cc16",
  "Chen caerulescens": "#06b6d4",
  "Falco naumanni": "#e879f9",
};
const MOVEBANK_TRACK_DEFAULT_COLOR = "#94a3b8";

/** English common name lookup for display in UI */
export const MOVEBANK_SPECIES_COMMON_NAMES: Record<string, string> = {
  "Larus fuscus": "Lesser Black-backed Gull",
  "Ciconia ciconia": "White Stork",
  "Larus argentatus": "Herring Gull",
  "Chroicocephalus ridibundus": "Black-headed Gull",
  "Grus grus": "Common Crane",
  "Milvus milvus": "Red Kite",
  "Gyps africanus": "African White-backed Vulture",
  "Buteo jamaicensis": "Red-tailed Hawk",
  "Chen caerulescens": "Snow Goose",
  "Falco naumanni": "Lesser Kestrel",
};

/** Single lime-green used for all Movebank track visuals — distinct from teal station dots */
const MOVEBANK_LIME = "rgba(163,230,53,1)";

/* ── Chaikin corner-cutting: smooths polylines while preserving shape ── */
function smoothLine(coords: number[][], iterations = 2): number[][] {
  if (coords.length < 3) return coords;
  let result = coords;
  for (let iter = 0; iter < iterations; iter++) {
    const out: number[][] = [result[0]];
    for (let i = 0; i < result.length - 1; i++) {
      const a = result[i],
        b = result[i + 1];
      out.push(a.map((v, j) => 0.75 * v + 0.25 * b[j]));
      out.push(a.map((v, j) => 0.25 * v + 0.75 * b[j]));
    }
    out.push(result[result.length - 1]);
    result = out;
  }
  return result;
}

function dedupeSequentialCoords(coords: number[][]): number[][] {
  if (coords.length <= 1) return coords;

  const deduped: number[][] = [coords[0]];
  for (let index = 1; index < coords.length; index += 1) {
    const previous = deduped[deduped.length - 1];
    const current = coords[index];
    if (previous[0] === current[0] && previous[1] === current[1]) {
      continue;
    }
    deduped.push(current);
  }

  return deduped;
}

/* ── Province boundary layer styles ─────────────────────────────── */

const provinceFill: LayerSpecification = {
  id: "province-fill",
  type: "fill",
  source: "provinces",
  paint: {
    "fill-color": "transparent",
    "fill-opacity": 0.001, // Invisible but still clickable/hoverable
  },
};

// Create a separate highlighted layer using filter logic rather than feature-state
// This guarantees that all multipolygon parts of a single province highlight at once
const provinceHighlight: LayerSpecification = {
  id: "province-highlight",
  type: "fill",
  source: "provinces",
  paint: {
    "fill-color": "#34d399", // brand-400
    "fill-opacity": 0.15,
  },
};

const provinceGlow: LayerSpecification = {
  id: "province-glow",
  type: "line",
  source: "provinces",
  paint: {
    "line-color": "rgba(52, 211, 153, 0.35)",
    "line-width": 8,
    "line-blur": 6,
  },
};

const provinceLine: LayerSpecification = {
  id: "province-line",
  type: "line",
  source: "provinces",
  paint: {
    "line-color": "rgba(52, 211, 153, 0.60)",
    "line-width": 1.5,
    "line-dasharray": [6, 4],
  },
};

const provinceLabels: LayerSpecification = {
  id: "province-labels",
  type: "symbol",
  source: "provinces",
  maxzoom: 7.5, // Hide when city names appear
  layout: {
    "text-field": ["get", "naam"],
    "text-size": 9,
    "text-transform": "uppercase",
    "text-letter-spacing": 0.15,
    "text-allow-overlap": false,
    "text-padding": 10,
    "text-font": ["Open Sans Regular"],
  },
  paint: {
    "text-color": "rgba(52, 211, 153, 0.40)",
    "text-halo-color": "rgba(17, 17, 17, 0.9)",
    "text-halo-width": 2,
  },
};

/* ── Municipality boundary layer styles ─────────────────────────────── */

const municipalityFill: LayerSpecification = {
  id: "municipality-fill",
  type: "fill",
  source: "municipalities",
  minzoom: 7.5,
  paint: {
    "fill-color": "transparent",
    "fill-opacity": 0.001,
  },
};

const municipalityHighlight: LayerSpecification = {
  id: "municipality-highlight",
  type: "fill",
  source: "municipalities",
  minzoom: 7.5,
  paint: {
    "fill-color": "#38bdf8", // sky-400
    "fill-opacity": 0.15,
  },
};

const municipalityGlow: LayerSpecification = {
  id: "municipality-glow",
  type: "line",
  source: "municipalities",
  minzoom: 7.4,
  paint: {
    "line-color": "rgba(56, 189, 248, 0.26)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      7.4,
      2.4,
      9,
      3.4,
      12,
      4.8,
      15,
      6.2,
    ],
    "line-blur": 2.3,
    "line-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      7.4,
      0,
      8.2,
      0.2,
      10.5,
      0.28,
      15,
      0.34,
    ],
  },
  layout: {
    "line-cap": "round",
    "line-join": "round",
  },
};

const municipalityLine: LayerSpecification = {
  id: "municipality-line",
  type: "line",
  source: "municipalities",
  minzoom: 7.5, // Only fade in after zooming into a province
  paint: {
    "line-color": "rgba(125, 211, 252, 0.94)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      7.5,
      0.9,
      9,
      1.2,
      12,
      1.8,
      15,
      2.4,
    ],
    "line-dasharray": [2.4, 1.8],
    "line-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      7.5,
      0,
      8.3,
      0.74,
      10.5,
      0.84,
      15,
      0.92,
    ],
  },
  layout: {
    "line-cap": "round",
    "line-join": "round",
  },
};

/* ── Country mask layer style ─────────────────────────────── */

const countryMask: LayerSpecification = {
  id: "country-mask-layer",
  type: "fill",
  source: "country-mask",
  paint: {
    "fill-color": "#111111",
    "fill-opacity": 1.0, // Fully opaque — water and outside areas are consistently dark
  },
};

const countryBorder: LayerSpecification = {
  id: "country-border-layer",
  type: "line",
  source: "country-border",
  paint: {
    "line-color": "rgba(16, 185, 129, 0.4)",
    "line-width": 2,
    "line-blur": 1,
  },
};

/* ── Stations (WebGL GPU rendered) ─────────────────────────────── */

/** Sonar-like pulse ring — delicate, animated via requestAnimationFrame */
const stationsPulse: LayerSpecification = {
  id: "stations-pulse",
  type: "circle",
  source: "stations",
  paint: {
    "circle-color": "transparent",
    "circle-radius": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      6,
      12,
      12,
      18,
      20,
    ],
    "circle-blur": 0,
    "circle-opacity": 0.6,
    "circle-stroke-color": "rgba(52, 211, 153, 0.25)",
    "circle-stroke-width": 1,
  },
};

const stationsOuterGlow: LayerSpecification = {
  id: "stations-outer-glow",
  type: "circle",
  source: "stations",
  paint: {
    "circle-color": "rgba(52, 211, 153, 0.4)",
    "circle-radius": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      6,
      12,
      14,
      18,
      28,
    ],
    "circle-blur": 0.8,
  },
};

const stationsInnerDot: LayerSpecification = {
  id: "stations-inner-dot",
  type: "circle",
  source: "stations",
  paint: {
    "circle-color": "#34d399",
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 2, 12, 4, 18, 6],
    "circle-stroke-color": "rgba(17, 17, 17, 0.5)",
    "circle-stroke-width": 1,
  },
};

/* ── Regular partition grid overlay ───────────────────────────── */

/** Regular 5 km fishnet lines for the grid partitioning mode */
const partitionGridGlow: LayerSpecification = {
  id: "partition-grid-glow",
  type: "line",
  source: "partition-grid",
  paint: {
    "line-color": "rgba(52, 211, 153, 0.28)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      3.2,
      10,
      4.4,
      15,
      5.8,
    ],
    "line-blur": 2.8,
    "line-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      0.54,
      10,
      0.42,
      15,
      0.32,
    ],
  },
};

const partitionGridLine: LayerSpecification = {
  id: "partition-grid-line",
  type: "line",
  source: "partition-grid",
  paint: {
    "line-color": "rgba(74, 222, 128, 0.88)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      0.85,
      10,
      1.05,
      15,
      1.3,
    ],
    "line-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      0.42,
      10,
      0.34,
      15,
      0.28,
    ],
  },
};

const partitionGridHitbox: LayerSpecification = {
  id: "partition-grid-hitbox",
  type: "fill",
  source: "partition-grid",
  paint: {
    "fill-color": "rgba(255,255,255,0.01)",
    "fill-opacity": 0.01,
  },
};

const partitionGridHoverFill: LayerSpecification = {
  id: "partition-grid-hover-fill",
  type: "fill",
  source: "partition-grid",
  paint: {
    "fill-color": "rgba(110, 231, 183, 0.12)",
    "fill-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      0.08,
      10,
      0.11,
      15,
      0.14,
    ],
  },
};

const partitionGridHoverLine: LayerSpecification = {
  id: "partition-grid-hover-line",
  type: "line",
  source: "partition-grid",
  paint: {
    "line-color": "rgba(167, 243, 208, 0.95)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      1.2,
      10,
      1.8,
      15,
      2.6,
    ],
    "line-opacity": 0.98,
  },
};

const partitionGridSelectedFill: LayerSpecification = {
  id: "partition-grid-selected-fill",
  type: "fill",
  source: "partition-grid",
  paint: {
    "fill-color": "rgba(52, 211, 153, 0.16)",
    "fill-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      0.11,
      10,
      0.15,
      15,
      0.18,
    ],
  },
};

const partitionGridSelectedLine: LayerSpecification = {
  id: "partition-grid-selected-line",
  type: "line",
  source: "partition-grid",
  paint: {
    "line-color": "rgba(167, 243, 208, 0.98)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      1.45,
      10,
      2.2,
      15,
      3.1,
    ],
    "line-opacity": 1,
  },
};

const partitionSubGridLine: LayerSpecification = {
  id: "partition-subgrid-line",
  type: "line",
  source: "partition-subgrid",
  minzoom: 9,
  paint: {
    "line-color": "rgba(74, 222, 128, 0.18)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      9,
      0.28,
      12,
      0.42,
      15,
      0.6,
    ],
    "line-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      9,
      0.28,
      12,
      0.38,
      15,
      0.48,
    ],
  },
};

const partitionGridAnchorLabel: LayerSpecification = {
  id: "partition-grid-anchor-label",
  type: "symbol",
  source: "partition-grid-anchor-labels",
  minzoom: 6.2,
  layout: {
    "text-field": ["get", "text"],
    "text-font": ["Open Sans Bold"],
    "text-size": ["interpolate", ["linear"], ["zoom"], 6.2, 9, 10, 10, 15, 12],
    "text-letter-spacing": 0.12,
    "text-allow-overlap": false,
    "text-padding": 4,
  },
  paint: {
    "text-color": "rgba(110, 231, 183, 0.55)",
    "text-halo-color": "rgba(4, 15, 10, 0.95)",
    "text-halo-width": 1.6,
  },
};

const partitionGridDetailedLabel: LayerSpecification = {
  id: "partition-grid-detailed-label",
  type: "symbol",
  source: "partition-grid-detailed-labels",
  minzoom: 11,
  layout: {
    "text-field": ["get", "text"],
    "text-font": ["Open Sans Regular"],
    "text-size": ["interpolate", ["linear"], ["zoom"], 11, 7, 13, 8, 15, 9],
    "text-letter-spacing": 0.08,
    "text-allow-overlap": false,
    "text-padding": 2,
  },
  paint: {
    "text-color": "rgba(148, 226, 213, 0.38)",
    "text-halo-color": "rgba(4, 15, 10, 0.92)",
    "text-halo-width": 1.2,
  },
};

const partitionIntersectionOuter: LayerSpecification = {
  id: "partition-intersection-outer",
  type: "symbol",
  source: "partition-grid-intersections",
  minzoom: 8,
  layout: {
    "text-field": "□",
    "text-font": ["Open Sans Bold"],
    "text-size": ["interpolate", ["linear"], ["zoom"], 8, 10, 12, 12, 15, 14],
    "text-allow-overlap": true,
    "text-ignore-placement": true,
  },
  paint: {
    "text-color": "rgba(74, 222, 128, 0.24)",
    "text-halo-color": "rgba(4, 15, 10, 0.75)",
    "text-halo-width": 0.8,
  },
};

const partitionIntersectionInner: LayerSpecification = {
  id: "partition-intersection-inner",
  type: "symbol",
  source: "partition-grid-intersections",
  minzoom: 8,
  layout: {
    "text-field": "□",
    "text-font": ["Open Sans Bold"],
    "text-size": ["interpolate", ["linear"], ["zoom"], 8, 5.5, 12, 7, 15, 8],
    "text-allow-overlap": true,
    "text-ignore-placement": true,
  },
  paint: {
    "text-color": "rgba(110, 231, 183, 0.65)",
    "text-halo-color": "rgba(4, 15, 10, 0.88)",
    "text-halo-width": 0.5,
  },
};

const partitionIntersectionCore: LayerSpecification = {
  id: "partition-intersection-core",
  type: "circle",
  source: "partition-grid-intersections",
  minzoom: 8,
  paint: {
    "circle-radius": [
      "interpolate",
      ["linear"],
      ["zoom"],
      8,
      1.1,
      12,
      1.4,
      15,
      1.8,
    ],
    "circle-color": "rgba(110, 231, 183, 0.92)",
    "circle-opacity": 0.86,
    "circle-stroke-color": "rgba(4, 15, 10, 0.9)",
    "circle-stroke-width": 0.6,
  },
};

/* ── Earth Engine overlay layer styles ───────────────────────── */

const earthEngineOverlayFill: LayerSpecification = {
  id: "earth-engine-overlay-fill",
  type: "fill",
  source: "earth-engine-overlay-cells",
  paint: {
    "fill-color": ["coalesce", ["get", "fill_color"], "rgba(0,0,0,0)"] as never,
    "fill-opacity": ["coalesce", ["get", "fill_opacity"], 0] as never,
  },
};

const earthEngineOverlayGlow: LayerSpecification = {
  id: "earth-engine-overlay-glow",
  type: "line",
  source: "earth-engine-overlay-cells",
  paint: {
    "line-color": [
      "coalesce",
      ["get", "line_color"],
      "rgba(142,163,154,0.18)",
    ] as never,
    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 4, 10, 6.5, 15, 9],
    "line-blur": 6,
    "line-opacity": ["coalesce", ["get", "glow_opacity"], 0.05] as never,
  },
  layout: {
    "line-cap": "round",
    "line-join": "round",
  },
};

const earthEngineOverlayLine: LayerSpecification = {
  id: "earth-engine-overlay-line",
  type: "line",
  source: "earth-engine-overlay-cells",
  paint: {
    "line-color": [
      "coalesce",
      ["get", "line_color"],
      "rgba(142,163,154,0.26)",
    ] as never,
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      0.55,
      10,
      0.9,
      15,
      1.3,
    ],
    "line-opacity": ["coalesce", ["get", "line_opacity"], 0.22] as never,
  },
  layout: {
    "line-cap": "round",
    "line-join": "round",
  },
};

const earthEngineOverlayScan: LayerSpecification = {
  id: "earth-engine-overlay-scan",
  type: "line",
  source: "earth-engine-overlay-cells",
  paint: {
    "line-color": [
      "coalesce",
      ["get", "line_color"],
      "rgba(142,163,154,0.24)",
    ] as never,
    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.4, 10, 0.7, 15, 1],
    "line-opacity": ["coalesce", ["get", "scan_opacity"], 0.08] as never,
    "line-dasharray": [1.2, 2.6],
  },
  layout: {
    "line-cap": "round",
    "line-join": "round",
  },
};

const earthEngineOverlayHoverFill: LayerSpecification = {
  id: "earth-engine-overlay-hover-fill",
  type: "fill",
  source: "earth-engine-overlay-cells",
  paint: {
    "fill-color": "rgba(248,250,252,0.06)",
    "fill-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      0.05,
      10,
      0.06,
      15,
      0.08,
    ],
  },
};

const earthEngineOverlayHoverLine: LayerSpecification = {
  id: "earth-engine-overlay-hover-line",
  type: "line",
  source: "earth-engine-overlay-cells",
  paint: {
    "line-color": "rgba(235, 240, 242, 0.9)",
    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1, 10, 1.4, 15, 2],
    "line-opacity": 0.92,
  },
};

const earthEngineOverlaySelectedFill: LayerSpecification = {
  id: "earth-engine-overlay-selected-fill",
  type: "fill",
  source: "earth-engine-overlay-cells",
  paint: {
    "fill-color": "rgba(216,205,171,0.14)",
    "fill-opacity": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      0.08,
      10,
      0.1,
      15,
      0.12,
    ],
  },
};

const earthEngineOverlaySelectedLine: LayerSpecification = {
  id: "earth-engine-overlay-selected-line",
  type: "line",
  source: "earth-engine-overlay-cells",
  paint: {
    "line-color": "rgba(222, 212, 186, 0.94)",
    "line-width": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      1.2,
      10,
      1.7,
      15,
      2.4,
    ],
    "line-opacity": 0.96,
  },
};

const earthEngineOverlayPointGlow: LayerSpecification = {
  id: "earth-engine-overlay-point-glow",
  type: "circle",
  source: "earth-engine-overlay-points",
  paint: {
    "circle-color": [
      "coalesce",
      ["get", "point_color"],
      "rgba(142,163,154,0.38)",
    ] as never,
    "circle-radius": ["coalesce", ["get", "point_radius"], 4] as never,
    "circle-blur": 1.25,
    "circle-opacity": ["coalesce", ["get", "point_glow"], 0.12] as never,
  },
};

const earthEngineOverlayPointCore: LayerSpecification = {
  id: "earth-engine-overlay-point-core",
  type: "circle",
  source: "earth-engine-overlay-points",
  paint: {
    "circle-color": [
      "coalesce",
      ["get", "point_color"],
      "rgba(142,163,154,0.72)",
    ] as never,
    "circle-radius": ["coalesce", ["get", "point_radius"], 3] as never,
    "circle-opacity": ["coalesce", ["get", "point_opacity"], 0.76] as never,
    "circle-stroke-color": "rgba(255,255,255,0.12)",
    "circle-stroke-width": 0.6,
  },
};

const earthEngineOverlayLabel: LayerSpecification = {
  id: "earth-engine-overlay-label",
  type: "symbol",
  source: "earth-engine-overlay-labels",
  layout: {
    "text-field": ["get", "text"],
    "text-font": ["Open Sans Regular"],
    "text-size": ["interpolate", ["linear"], ["zoom"], 6, 8, 10, 9, 15, 11],
    "text-anchor": "center",
    "text-letter-spacing": 0.03,
    "text-padding": 3,
    "text-allow-overlap": false,
    "text-ignore-placement": false,
  },
  paint: {
    "text-color": [
      "coalesce",
      ["get", "text_color"],
      "rgba(156,174,168,0.6)",
    ] as never,
    "text-halo-color": "rgba(4,15,10,0.92)",
    "text-halo-width": 1.1,
  },
};

const hotspotOverlayFill: LayerSpecification = {
  id: "hotspot-overlay-fill",
  type: "fill",
  source: "hotspot-overlay-cells",
  paint: {
    "fill-color": [
      "coalesce",
      ["get", "hotspot_fill_color"],
      "rgba(248,113,113,0.62)",
    ] as never,
    "fill-opacity": [
      "case",
      ["==", ["get", "hotspot_covered"], true],
      ["interpolate", ["linear"], ["get", "hotspot_score"], 0, 0.3, 1, 0.72],
      0.45,
    ] as never,
  },
};

const hotspotOverlayGlow: LayerSpecification = {
  id: "hotspot-overlay-glow",
  type: "line",
  source: "hotspot-overlay-cells",
  paint: {
    "line-color": [
      "coalesce",
      ["get", "hotspot_line_color"],
      "rgba(252,165,165,0.9)",
    ] as never,
    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 4, 10, 6.5, 15, 9],
    "line-blur": 6,
    "line-opacity": [
      "interpolate",
      ["linear"],
      ["get", "hotspot_score"],
      0,
      0.08,
      1,
      0.38,
    ],
  },
  layout: {
    "line-cap": "round",
    "line-join": "round",
  },
};

const hotspotOverlayLine: LayerSpecification = {
  id: "hotspot-overlay-line",
  type: "line",
  source: "hotspot-overlay-cells",
  paint: {
    "line-color": [
      "coalesce",
      ["get", "hotspot_line_color"],
      "rgba(252,165,165,0.9)",
    ] as never,
    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.9, 10, 1.4, 15, 2.2],
    "line-opacity": [
      "interpolate",
      ["linear"],
      ["get", "hotspot_score"],
      0,
      0.26,
      1,
      0.92,
    ],
  },
  layout: {
    "line-cap": "round",
    "line-join": "round",
  },
};

/* ── Movebank animated track layer styles ────────────────────── */

/** Soft glow aura — subtle depth behind the track path */
const movebankGlowLayer: LayerSpecification = {
  id: "movebank-glow",
  type: "line",
  source: "movebank-tracks",
  paint: {
    "line-color": MOVEBANK_LIME,
    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 4, 10, 6, 15, 8],
    "line-blur": 6,
    "line-opacity": 0.03,
  },
  layout: { "line-cap": "round", "line-join": "round" },
};

/** Faint dashed ghost path — shows the full route history at low opacity */
const movebankTrackLayer: LayerSpecification = {
  id: "movebank-track",
  type: "line",
  source: "movebank-tracks",
  paint: {
    "line-color": "rgba(255,255,255,1)",
    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.4, 10, 0.7, 15, 1.0],
    "line-opacity": 0.08,
    "line-dasharray": [2, 5],
  },
  layout: { "line-cap": "round", "line-join": "round" },
};

/**
 * Directional age-fade — transparent at origin, bright lime at tip.
 * Dominant visual cue for flight direction. Requires lineMetrics: true on the Source.
 */
const movebankFadeLayer: LayerSpecification = {
  id: "movebank-fade",
  type: "line",
  source: "movebank-tracks",
  paint: {
    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.9, 10, 1.5, 15, 2.2],
    "line-gradient": [
      "interpolate",
      ["linear"],
      ["line-progress"],
      0,    "rgba(163,230,53,0)",
      0.55, "rgba(163,230,53,0)",
      0.75, "rgba(163,230,53,0.14)",
      1,    "rgba(163,230,53,0.55)",
    ] as never,
  },
  layout: { "line-cap": "round", "line-join": "round" },
};

/** Station-style sonar pulse rendered at the origin (start) of each track */
const movebankPulseLayer: LayerSpecification = {
  id: "movebank-pulse",
  type: "circle",
  source: "movebank-origins",
  paint: {
    "circle-color": "transparent",
    "circle-radius": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      6,
      12,
      12,
      18,
      20,
    ],
    "circle-blur": 0,
    "circle-opacity": 0.6,
    "circle-stroke-color": "rgba(163, 230, 53, 0.25)",
    "circle-stroke-width": 1,
  },
};

/** Soft outer glow matching the station dot treatment */
const movebankOuterGlowLayer: LayerSpecification = {
  id: "movebank-outer-glow",
  type: "circle",
  source: "movebank-origins",
  paint: {
    "circle-color": "rgba(163, 230, 53, 0.4)",
    "circle-radius": [
      "interpolate",
      ["linear"],
      ["zoom"],
      6,
      6,
      12,
      14,
      18,
      28,
    ],
    "circle-blur": 0.8,
  },
};

/** Solid lime origin dot so the track start remains visible under the bird head */
const movebankOriginLayer: LayerSpecification = {
  id: "movebank-origin",
  type: "circle",
  source: "movebank-origins",
  paint: {
    "circle-color": "#a3e635",
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 2, 12, 4, 18, 6],
    "circle-stroke-color": "rgba(17, 17, 17, 0.5)",
    "circle-stroke-width": 1,
  },
};

export interface SatelliteMapRef {
  resetView: () => void;
}

interface ViewportBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

type MapFeature = GeoJSON.Feature<
  GeoJSON.Geometry,
  GeoJSON.GeoJsonProperties
>;
type MapFeatureCollection = GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  GeoJSON.GeoJsonProperties
>;
type PolygonMapFeature = GeoJSON.Feature<
  GeoJSON.Polygon | GeoJSON.MultiPolygon,
  GeoJSON.GeoJsonProperties
>;
type MapInteractionEvent = {
  point: { x: number; y: number };
  lngLat: { lat: number; lng: number };
};

function readFeatureProperties(
  feature: Pick<MapFeature, "properties"> | null | undefined,
): Record<string, unknown> {
  return feature?.properties && typeof feature.properties === "object"
    ? (feature.properties as Record<string, unknown>)
    : {};
}

function isPolygonMapFeature(
  feature: MapFeature | null | undefined,
): feature is PolygonMapFeature {
  return (
    feature?.geometry?.type === "Polygon" ||
    feature?.geometry?.type === "MultiPolygon"
  );
}

function readFeatureName(feature: MapFeature | null | undefined): string | null {
  const name = readFeatureProperties(feature).naam;
  return typeof name === "string" ? name : null;
}

function findFeatureByName(
  featureCollectionData: MapFeatureCollection | null | undefined,
  name: string,
): MapFeature | undefined {
  return featureCollectionData?.features.find(
    (feature) => readFeatureName(feature) === name,
  );
}

function clipFeatureCollectionToCountry(
  featureCollectionData: MapFeatureCollection | null | undefined,
  countryData: MapFeatureCollection | null | undefined,
): MapFeatureCollection | null | undefined {
  const countryFeature = countryData?.features?.[0];
  if (
    !featureCollectionData?.features?.length ||
    !isPolygonMapFeature(countryFeature)
  ) {
    return featureCollectionData;
  }

  const clippedFeatures = featureCollectionData.features
    .map((feature): MapFeature | null => {
      const geometryType = feature?.geometry?.type;
      if (!geometryType) {
        return null;
      }

      if (geometryType === "Point") {
        try {
          const pointGeometry = feature.geometry as GeoJSON.Point;
          return booleanPointInPolygon(
            turfPoint(pointGeometry.coordinates),
            countryFeature,
          )
            ? feature
            : null;
        } catch {
          return null;
        }
      }

      if (!isPolygonMapFeature(feature)) {
        return feature;
      }

      try {
        const clipped = intersect(featureCollection([feature, countryFeature]));
        if (!clipped?.geometry) return null;
        return {
          ...feature,
          geometry: clipped.geometry,
        };
      } catch (error) {
        console.warn("Failed to clip feature to Netherlands boundary:", error);
        return null;
      }
    })
    .filter((feature): feature is MapFeature => Boolean(feature));

  return {
    ...featureCollectionData,
    features: clippedFeatures,
  };
}

function filterGridFeatureCollectionToCountry(
  featureCollectionData: MapFeatureCollection | null | undefined,
  countryData: MapFeatureCollection | null | undefined,
): MapFeatureCollection | null | undefined {
  const countryFeature = countryData?.features?.[0];
  if (
    !featureCollectionData?.features?.length ||
    !isPolygonMapFeature(countryFeature)
  ) {
    return featureCollectionData;
  }

  const filteredFeatures = featureCollectionData.features.filter((feature) => {
    const properties = readFeatureProperties(feature);
    const centerLon = toFiniteNumber(properties.lon);
    const centerLat = toFiniteNumber(properties.lat);

    if (centerLon == null || centerLat == null) {
      return true;
    }

    try {
      return booleanPointInPolygon(
        turfPoint([centerLon, centerLat]),
        countryFeature,
      );
    } catch {
      return true;
    }
  });

  return {
    ...featureCollectionData,
    features: filteredFeatures,
  };
}

function toSpreadsheetLabel(index: number): string {
  let value = index + 1;
  let label = "";

  while (value > 0) {
    value -= 1;
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26);
  }

  return label;
}

function parseGridReference(gridCellId: string | undefined) {
  const match = gridCellId?.match(/-r(\d+)-c(\d+)$/);
  if (!match) {
    return {
      label: gridCellId ?? "GRID",
      anchor: false,
    };
  }

  const row = Number(match[1]);
  const col = Number(match[2]);
  return {
    label: `${toSpreadsheetLabel(row)}${String(col + 1).padStart(2, "0")}`,
    anchor: row % 5 === 0 && col % 5 === 0,
  };
}

function interpolateCoord(
  a: number[],
  b: number[],
  t: number,
): [number, number] {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function deriveGridSelection(
  feature: MapFeature | null | undefined,
  gridLookup: Record<string, GridCellLookupEntry>,
): SelectedGridCell | null {
  if (!feature) return null;

  const props = readFeatureProperties(feature);
  const gridCellIdRaw = props.grid_cell_id ?? props.station_id;
  if (!gridCellIdRaw) return null;

  const gridCellId = String(gridCellIdRaw);
  const lookup = gridLookup[gridCellId];
  let longitude: number | null = lookup?.longitude ?? toFiniteNumber(props.lon);
  let latitude: number | null = lookup?.latitude ?? toFiniteNumber(props.lat);

  if (longitude == null || latitude == null) {
    try {
      const center = centroid(feature);
      longitude = toFiniteNumber(center.geometry.coordinates?.[0]);
      latitude = toFiniteNumber(center.geometry.coordinates?.[1]);
    } catch {
      return null;
    }
  }

  if (longitude == null || latitude == null) return null;

  return {
    gridCellId,
    referenceLabel: parseGridReference(gridCellId).label,
    longitude,
    latitude,
    gridSystem:
      lookup?.gridSystem ?? String(props.grid_system ?? "nl_geodesic_5km"),
    gridSizeKm: lookup?.gridSizeKm ?? toFiniteNumber(props.grid_size_km),
    stationCount: lookup?.stationCount ?? 0,
    totalDetections: lookup?.totalDetections ?? 0,
  };
}

function expandViewportBounds(
  bounds: ViewportBounds,
  paddingDegrees = 0.15,
): ViewportBounds {
  return {
    west: bounds.west - paddingDegrees,
    south: bounds.south - paddingDegrees,
    east: bounds.east + paddingDegrees,
    north: bounds.north + paddingDegrees,
  };
}

function featureIntersectsViewportBounds(
  feature: MapFeature,
  bounds: ViewportBounds,
): boolean {
  const props = readFeatureProperties(feature);
  const minLon = toFiniteNumber(props.bbox_min_lon);
  const minLat = toFiniteNumber(props.bbox_min_lat);
  const maxLon = toFiniteNumber(props.bbox_max_lon);
  const maxLat = toFiniteNumber(props.bbox_max_lat);

  if (
    minLon != null &&
    minLat != null &&
    maxLon != null &&
    maxLat != null
  ) {
    return !(
      maxLon < bounds.west ||
      minLon > bounds.east ||
      maxLat < bounds.south ||
      minLat > bounds.north
    );
  }

  try {
    const [featureWest, featureSouth, featureEast, featureNorth] = bbox(feature);
    return !(
      featureEast < bounds.west ||
      featureWest > bounds.east ||
      featureNorth < bounds.south ||
      featureSouth > bounds.north
    );
  } catch {
    return true;
  }
}

function filterGridDetailsToViewport(
  gridData: MapFeatureCollection | null,
  bounds: ViewportBounds | null,
) {
  if (!gridData?.features?.length || !bounds) return gridData;

  const expandedBounds = expandViewportBounds(bounds);
  const features = gridData.features.filter((feature) =>
    featureIntersectsViewportBounds(feature, expandedBounds),
  );

  return {
    ...gridData,
    features,
  };
}

function buildGridOverlayBundle(
  gridData: MapFeatureCollection | null,
  options: GridOverlayBuildOptions,
): GridOverlayBundle | null {
  if (!gridData?.features?.length) return null;
  if (
    !options.includeAnchorLabels &&
    !options.includeDetailedLabels &&
    !options.includeIntersections &&
    !options.includeSubGrid
  ) {
    return null;
  }

  const anchorLabels: GeoJSON.Feature[] = [];
  const detailedLabels: GeoJSON.Feature[] = [];
  const intersectionsByKey: Record<
    string,
    { coord: [number, number]; count: number }
  > = {};
  const subGridFeatures: GeoJSON.Feature[] = [];

  for (const feature of gridData.features) {
    if (options.includeAnchorLabels || options.includeDetailedLabels) {
      const reference = parseGridReference(feature?.properties?.grid_cell_id);

      try {
        const center = centroid(feature);
        const labelFeature: GeoJSON.Feature = {
          type: "Feature",
          geometry: {
            type: "Point",
            coordinates: center.geometry.coordinates,
          },
          properties: {
            text: reference.label,
          },
        };
        if (options.includeDetailedLabels) {
          detailedLabels.push(labelFeature);
        }
        if (options.includeAnchorLabels && reference.anchor) {
          anchorLabels.push(labelFeature);
        }
      } catch {
        // Ignore malformed geometries when building labels.
      }
    }

    if (
      feature?.geometry?.type !== "Polygon" ||
      (!options.includeIntersections && !options.includeSubGrid)
    ) {
      continue;
    }

    const ring = feature.geometry.coordinates?.[0];
    if (!Array.isArray(ring) || ring.length !== 5) continue;

    const southWest = ring[0] as number[];
    const southEast = ring[1] as number[];
    const northEast = ring[2] as number[];
    const northWest = ring[3] as number[];

    if (options.includeIntersections) {
      for (const corner of [southWest, southEast, northEast, northWest]) {
        const key = `${corner[0].toFixed(6)},${corner[1].toFixed(6)}`;
        const existing = intersectionsByKey[key];
        if (existing) {
          existing.count += 1;
        } else {
          intersectionsByKey[key] = {
            coord: [corner[0], corner[1]],
            count: 1,
          };
        }
      }
    }

    if (options.includeSubGrid) {
      for (let step = 1; step < 5; step += 1) {
        const t = step / 5;
        const southPoint = interpolateCoord(southWest, southEast, t);
        const northPoint = interpolateCoord(northWest, northEast, t);
        const westPoint = interpolateCoord(southWest, northWest, t);
        const eastPoint = interpolateCoord(southEast, northEast, t);

        subGridFeatures.push({
          type: "Feature",
          geometry: {
            type: "LineString",
            coordinates: [southPoint, northPoint],
          },
          properties: { axis: "column" },
        });

        subGridFeatures.push({
          type: "Feature",
          geometry: {
            type: "LineString",
            coordinates: [westPoint, eastPoint],
          },
          properties: { axis: "row" },
        });
      }
    }
  }

  const intersectionFeatures: GeoJSON.Feature[] = options.includeIntersections
    ? Object.values(intersectionsByKey)
        .filter((node) => node.count >= 2)
        .map((node) => ({
          type: "Feature",
          geometry: {
            type: "Point",
            coordinates: node.coord,
          },
          properties: {
            count: node.count,
          },
        }))
    : [];

  const anchorLabelCollection = options.includeAnchorLabels
    ? {
        type: "FeatureCollection" as const,
        features: anchorLabels,
      }
    : null;
  const detailedLabelCollection = options.includeDetailedLabels
    ? {
        type: "FeatureCollection" as const,
        features: detailedLabels,
      }
    : null;
  const intersectionCollection = options.includeIntersections
    ? {
        type: "FeatureCollection" as const,
        features: intersectionFeatures,
      }
    : null;
  const subGridCollection = options.includeSubGrid
    ? {
        type: "FeatureCollection" as const,
        features: subGridFeatures,
      }
    : null;

  if (
    !anchorLabelCollection &&
    !detailedLabelCollection &&
    !intersectionCollection &&
    !subGridCollection
  ) {
    return null;
  }

  return {
    anchorLabels: anchorLabelCollection,
    detailedLabels: detailedLabelCollection,
    intersections: intersectionCollection,
    subGrid: subGridCollection,
  };
}

/**
 * Satellite map of the Netherlands with 3D perspective and province overlays.
 *
 * Province boundaries are fetched at mount from PDOK WFS (Kadaster) and
 * rendered as glowing dashed lines. Designed to be embedded inside a
 * circular viewport — all built-in controls are omitted.
 */
export const SatelliteMap = forwardRef<SatelliteMapRef, SatelliteMapProps>(
  function SatelliteMap(
    {
      onViewStateChange,
      activeStationTypes,
      activeSpecies,
      minQuality,
      showStations = true,
      focusedStationId,
      showMovebank = false,
      movebankSpeciesFilter,
      movebankWindowDays,
      skin = "satellite",
      earthEngineOverlay,
      spatialPartitionMode = "municipality",
      hotspotSource = null,
      hotspotGrain = "grid",
      hotspotWeek = "latest",
      weatherOverlay = null,
      onHotspotMetaChange,
      onStationsLoaded,
      onTracksLoaded,
      onStationSelect,
      onGridSelect,
      flyFocus,
    },
    ref,
  ) {
    const mapRef = useRef<MapRef>(null);
    const mapContainerRef = useRef<HTMLDivElement | null>(null);
    const [renderedSkin, setRenderedSkin] = useState<MapSkinId>(skin);
    const isLczSkin = renderedSkin === "lcz";
    const isStandardSkin = renderedSkin === "standard";
    const mapSkin = useMemo(() => getMapSkinStyle(renderedSkin), [renderedSkin]);
    const activeSkinMeta = MAP_SKIN_META[renderedSkin];
    const requestedSkinMeta = MAP_SKIN_META[skin];
    const stationLayers = useMemo(
      () => {
        if (!isLczSkin) {
          return {
            pulse: stationsPulse,
            outerGlow: stationsOuterGlow,
            innerDot: stationsInnerDot,
          };
        }

        return {
          pulse: {
            ...stationsPulse,
            paint: {
              ...(stationsPulse.paint ?? {}),
              "circle-radius": [
                "interpolate",
                ["linear"],
                ["zoom"],
                6,
                4.6,
                12,
                8.6,
                18,
                14,
              ],
              "circle-opacity": 0.34,
              "circle-stroke-color": "rgba(184, 239, 255, 0.28)",
              "circle-stroke-width": 1,
            },
          } as LayerSpecification,
          outerGlow: {
            ...stationsOuterGlow,
            paint: {
              ...(stationsOuterGlow.paint ?? {}),
              "circle-color": "rgba(160, 232, 255, 0.34)",
              "circle-radius": [
                "interpolate",
                ["linear"],
                ["zoom"],
                6,
                4.8,
                12,
                10.5,
                18,
                18,
              ],
              "circle-opacity": 0.42,
            },
          } as LayerSpecification,
          innerDot: {
            ...stationsInnerDot,
            paint: {
              ...(stationsInnerDot.paint ?? {}),
              "circle-color": "rgba(248, 254, 255, 0.98)",
              "circle-radius": [
                "interpolate",
                ["linear"],
                ["zoom"],
                6,
                1.8,
                12,
                3.2,
                18,
                4.8,
              ],
              "circle-opacity": 0.96,
              "circle-stroke-color": "rgba(5, 8, 6, 0.64)",
              "circle-stroke-width": 1,
            },
          } as LayerSpecification,
        };
      },
      [isLczSkin],
    );
    const administrativeLayers = useMemo(
      () => {
        if (!isLczSkin) {
          if (isStandardSkin) {
            return {
              provinceGlow: {
                ...provinceGlow,
                paint: {
                  ...(provinceGlow.paint ?? {}),
                  "line-color": "rgba(14, 66, 46, 0.18)",
                  "line-width": 7,
                  "line-blur": 4,
                },
              } as LayerSpecification,
              provinceLine: {
                ...provinceLine,
                paint: {
                  ...(provinceLine.paint ?? {}),
                  "line-color": "rgba(17, 94, 68, 0.82)",
                  "line-width": 1.7,
                  "line-opacity": 0.92,
                },
              } as LayerSpecification,
              provinceLabels: {
                ...provinceLabels,
                paint: {
                  ...(provinceLabels.paint ?? {}),
                  "text-color": "rgba(15, 68, 49, 0.48)",
                  "text-halo-color": "rgba(246, 245, 241, 0.96)",
                  "text-halo-width": 2.2,
                },
              } as LayerSpecification,
              municipalityGlow: {
                ...municipalityGlow,
                paint: {
                  ...(municipalityGlow.paint ?? {}),
                  "line-color": "rgba(21, 94, 117, 0.18)",
                  "line-opacity": [
                    "interpolate",
                    ["linear"],
                    ["zoom"],
                    7.4,
                    0,
                    8.2,
                    0.16,
                    10.5,
                    0.24,
                    15,
                    0.3,
                  ],
                },
              } as LayerSpecification,
              municipalityLine: {
                ...municipalityLine,
                paint: {
                  ...(municipalityLine.paint ?? {}),
                  "line-color": "rgba(36, 121, 167, 0.84)",
                  "line-width": [
                    "interpolate",
                    ["linear"],
                    ["zoom"],
                    7.5,
                    1.1,
                    9,
                    1.4,
                    12,
                    2,
                    15,
                    2.7,
                  ],
                  "line-opacity": [
                    "interpolate",
                    ["linear"],
                    ["zoom"],
                    7.5,
                    0,
                    8.3,
                    0.64,
                    10.5,
                    0.74,
                    15,
                    0.82,
                  ],
                },
              } as LayerSpecification,
            };
          }

          return {
            provinceGlow,
            provinceLine,
            provinceLabels,
            municipalityGlow,
            municipalityLine,
          };
        }

        return {
          provinceGlow: {
            ...provinceGlow,
            paint: {
              ...(provinceGlow.paint ?? {}),
              "line-color": "rgba(218, 250, 234, 0.24)",
              "line-width": 6,
              "line-blur": 4,
            },
          } as LayerSpecification,
          provinceLine: {
            ...provinceLine,
            paint: {
              ...(provinceLine.paint ?? {}),
              "line-color": "rgba(224, 252, 239, 0.86)",
              "line-width": 1.8,
              "line-opacity": 0.88,
            },
          } as LayerSpecification,
          provinceLabels: {
            ...provinceLabels,
            paint: {
              ...(provinceLabels.paint ?? {}),
              "text-color": "rgba(228, 245, 235, 0.42)",
              "text-halo-color": "rgba(5, 8, 6, 0.72)",
              "text-halo-width": 1.6,
            },
          } as LayerSpecification,
          municipalityGlow: {
            ...municipalityGlow,
            paint: {
              ...(municipalityGlow.paint ?? {}),
              "line-color": "rgba(200, 235, 252, 0.24)",
              "line-opacity": [
                "interpolate",
                ["linear"],
                ["zoom"],
                7.4,
                0,
                8.2,
                0.16,
                10.5,
                0.22,
                15,
                0.28,
              ],
            },
          } as LayerSpecification,
          municipalityLine: {
            ...municipalityLine,
            paint: {
              ...(municipalityLine.paint ?? {}),
              "line-color": "rgba(232, 243, 248, 0.82)",
              "line-width": [
                "interpolate",
                ["linear"],
                ["zoom"],
                7.5,
                1.1,
                9,
                1.5,
                12,
                2,
                15,
                2.7,
              ],
              "line-opacity": [
                "interpolate",
                ["linear"],
                ["zoom"],
                7.5,
                0,
                8.3,
                0.48,
                10.5,
                0.62,
                15,
                0.72,
              ],
            },
          } as LayerSpecification,
        };
      },
      [isLczSkin, isStandardSkin],
    );

    useImperativeHandle(ref, () => ({
      resetView: () => {
        mapRef.current?.flyTo({
          center: [NETHERLANDS_CENTER.lng, NETHERLANDS_CENTER.lat],
          zoom: DEFAULT_ZOOM,
          pitch: DEFAULT_PITCH,
          bearing: DEFAULT_BEARING,
          duration: 1500,
        });
      },
    }));

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [provinces, setProvinces] = useState<any>(null);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [municipalities, setMunicipalities] = useState<any>(null);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [countryMaskGeojson, setCountryMaskGeojson] = useState<any>(null);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [countryBorderGeojson, setCountryBorderGeojson] = useState<any>(null);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    // Optimize mouse hover event to reduce React re-renders which cause lag
    const [hoveredProvinceName, setHoveredProvinceName] = useState<
      string | null
    >(null);
    const [hoveredMunicipalityName, setHoveredMunicipalityName] = useState<
      string | null
    >(null);
    const [hoveredGridCellId, setHoveredGridCellId] = useState<string | null>(
      null,
    );
    const [hoverInfo, setHoverInfo] = useState<HoverInfo | null>(null);

    // We use refs to track coordinates without triggering a React render every single pixel the mouse moves
    const tooltipPosRef = useRef({ x: 0, y: 0 });

    const [stations, setStations] = useState<Station[]>([]);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [stationsGeojson, setStationsGeojson] = useState<any>(null);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [partitionGridGeojson, setPartitionGridGeojson] = useState<any>(null);
    const [partitionGridNeedsCountryClip, setPartitionGridNeedsCountryClip] =
      useState(true);
    const [dataReady, setDataReady] = useState(false);

    /** GeoJSON FeatureCollection of pre-decimated per-individual bird tracks */
    const [movebankTracksGeo, setMovebankTracksGeo] =
      useState<GeoJSON.FeatureCollection | null>(null);
    /** Selected individual track for the click popup */
    const [selectedMovebankTrack, setSelectedMovebankTrack] =
      useState<MovebankTrackProps | null>(null);
    const [earthEngineOverlayData, setEarthEngineOverlayData] =
      useState<EarthEngineOverlayResponse | null>(null);
    const [hotspotSummary, setHotspotSummary] =
      useState<HotspotSummaryResponse | null>(null);
    const [selectedGridOverview, setSelectedGridOverview] =
      useState<GridCellOverviewResponse | null>(null);
    const [selectedGridOverviewLoading, setSelectedGridOverviewLoading] =
      useState(false);
    const selectedGridOverviewCacheRef = useRef<
      Record<string, GridCellOverviewResponse | null>
    >({});
    const earthEngineBundleCacheRef = useRef<
      Record<string, EarthEngineOverlayBundle | null>
    >({});
    const earthEngineVisibleBundleCacheRef = useRef<
      Record<string, EarthEngineOverlayBundle | null>
    >({});
    const [
      earthEngineVisibleBundleCacheVersion,
      setEarthEngineVisibleBundleCacheVersion,
    ] = useState(0);

    /* ── Boot sequence state ── */
    const [bootMessages, setBootMessages] = useState<string[]>([
      "AVIAN BIOSURVEILLANCE SYSTEM v2.4.1",
    ]);
    const [bootFading, setBootFading] = useState(false);
    const [bootHidden, setBootHidden] = useState(false);
    const [skinTransitioning, setSkinTransitioning] = useState(false);

    // Filtered stations + GeoJSON based on active filters from the status bar
    const filteredGeojson = useMemo(() => {
      let result = stations;
      if (
        activeStationTypes &&
        activeStationTypes.length > 0 &&
        activeStationTypes.length < STATION_TYPE_OPTIONS.length
      ) {
        result = result.filter((station) =>
          matchesStationTypeFilter(station.type, activeStationTypes),
        );
      }
      if (activeSpecies && activeSpecies.length > 0) {
        const spSet = new Set(activeSpecies);
        result = result.filter((s) =>
          s.stats.top_species?.some((sp) => spSet.has(sp.name)),
        );
      }
      if (minQuality != null && minQuality > 0) {
        result = result.filter(
          (s) => (s.stats.lifetime_availability_pct ?? 0) >= minQuality,
        );
      }
      if (result.length === 0) return stationsGeojson; // fallback to all if nothing matches
      return {
        type: "FeatureCollection",
        features: result.map((st) => ({
          type: "Feature",
          geometry: {
            type: "Point",
            coordinates: [st.coords.lon, st.coords.lat],
          },
          properties: {
            ...st,
            total_detections: st.stats.total_detections ?? 0,
            unique_species: st.stats.unique_species ?? 0,
          },
        })),
      };
    }, [
      stations,
      stationsGeojson,
      activeStationTypes,
      activeSpecies,
      minQuality,
    ]);

    const [selectedStation, setSelectedStation] = useState<Station | null>(
      null,
    );
    const [selectedGridCell, setSelectedGridCell] =
      useState<SelectedGridCell | null>(null);

    useEffect(() => {
      if (!showStations) {
        setSelectedStation(null);
      }
    }, [showStations]);

    useEffect(() => {
      setHoveredProvinceName(null);
      setHoveredMunicipalityName(null);
      setHoveredGridCellId(null);
      setHoverInfo(null);
      if (spatialPartitionMode !== "grid") {
        setSelectedGridCell(null);
      }
    }, [spatialPartitionMode]);

    const [viewState, setViewState] = useState({
      longitude: NETHERLANDS_CENTER.lng as number,
      latitude: NETHERLANDS_CENTER.lat as number,
      zoom: DEFAULT_ZOOM,
      pitch: DEFAULT_PITCH,
      bearing: DEFAULT_BEARING,
    });

    // Latest viewState (read without re-triggering effects) + saved view for fly-and-return.
    const viewStateRef = useRef(viewState);
    viewStateRef.current = viewState;
    const detailReturnViewRef = useRef<{
      longitude: number; latitude: number; zoom: number; pitch: number; bearing: number;
    } | null>(null);

    useEffect(() => {
      if (!showStations) return;

      if (!focusedStationId) {
        setSelectedStation(null);
        return;
      }

      const nextStation = stations.find((station) => station.id === focusedStationId) ?? null;
      if (!nextStation) return;

      setSelectedStation(nextStation);
      mapRef.current?.flyTo({
        center: [nextStation.coords.lon, nextStation.coords.lat],
        zoom: Math.max(viewState.zoom, 10.5),
        pitch: viewState.pitch,
        bearing: viewState.bearing,
        duration: 1200,
      });
    }, [focusedStationId, showStations, stations, viewState.bearing, viewState.pitch, viewState.zoom]);

    // Mirror the selected station (live-map popup) to the parent so the live
    // feed can spotlight it; null when the popup closes.
    useEffect(() => {
      onStationSelect?.(selectedStation?.id ?? null);
    }, [selectedStation, onStationSelect]);

    // Same for a selected grid cell — the feed spotlights that cell's detections.
    useEffect(() => {
      onGridSelect?.(selectedGridCell?.gridCellId ?? null);
    }, [selectedGridCell, onGridSelect]);

    // Fly to a focus point (feed detail open) and restore the prior view on close.
    useEffect(() => {
      const map = mapRef.current;
      if (!map) return;
      if (flyFocus) {
        if (!detailReturnViewRef.current) {
          const v = viewStateRef.current;
          detailReturnViewRef.current = {
            longitude: v.longitude, latitude: v.latitude, zoom: v.zoom, pitch: v.pitch, bearing: v.bearing,
          };
        }
        map.flyTo({ center: [flyFocus.lon, flyFocus.lat], zoom: Math.max(viewStateRef.current.zoom, 11), duration: 1000 });
      } else if (detailReturnViewRef.current) {
        const v = detailReturnViewRef.current;
        detailReturnViewRef.current = null;
        map.flyTo({ center: [v.longitude, v.latitude], zoom: v.zoom, pitch: v.pitch, bearing: v.bearing, duration: 1000 });
      }
    }, [flyFocus]);

    useEffect(() => {
      const controller = new AbortController();

      // 1. Fetch provinces + country boundary + stations in parallel
      const geoPromise = Promise.all([
        fetch("/api/boundaries?type=provinces", {
          signal: controller.signal,
        }).then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        }),
        fetch("/api/boundaries?type=country", {
          signal: controller.signal,
        }).then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        }),
      ]);

      const stationsPromise = fetch("/api/stations", {
        signal: controller.signal,
      }).then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      });

      // Render map geometry as soon as it arrives (don't wait for stations).
      // Provinces + country arrive pre-simplified from /api/boundaries, and
      // the country payload includes a precomputed `countryMask`. This keeps
      // the heavy turf simplify/mask work off the browser main thread.
      geoPromise
        .then(([provincesData, countryData]) => {
          setProvinces(provincesData);
          setBootMessages((prev) => [
            ...prev,
            `${provincesData?.features?.length ?? 0} PROVINCE SECTORS: LOADED`,
          ]);

          if (
            countryData &&
            countryData.features &&
            countryData.features.length > 0
          ) {
            setCountryBorderGeojson(countryData);
            if (countryData.countryMask) {
              setCountryMaskGeojson(countryData.countryMask);
              setBootMessages((prev) => [
                ...prev,
                "NATIONAL BOUNDARY: SECURED",
              ]);
            }
          }
        })
        .catch((err) => {
          if (err.name !== "AbortError")
            console.warn("WFS boundaries unavailable:", err);
        });

      // Load stations independently — map renders first, stations appear when ready
      stationsPromise
        .then((rawStations: Station[]) => {
          // Hide stations with no detections — nothing to show or spotlight.
          const stationsData = (rawStations ?? []).filter(
            (st) => (st.stats?.total_detections ?? 0) > 0,
          );
          setStations(stationsData);
          onStationsLoaded?.(stationsData);
          if (stationsData && Array.isArray(stationsData)) {
            setStationsGeojson({
              type: "FeatureCollection",
              features: stationsData.map((st: Station) => ({
                type: "Feature",
                geometry: {
                  type: "Point",
                  coordinates: [st.coords.lon, st.coords.lat],
                },
                properties: {
                  ...st,
                  total_detections: st.stats.total_detections ?? 0,
                  unique_species: st.stats.unique_species ?? 0,
                },
              })),
            });
          }
          setBootMessages((prev) => [
            ...prev,
            `${stationsData?.length ?? 0} STATION NODES: ONLINE`,
          ]);
          setDataReady(true);
        })
        .catch((err) => {
          if (err.name !== "AbortError")
            console.warn("Stations API unavailable:", err);
          setDataReady(true); // still allow map interaction
        });

      return () => controller.abort();
    }, [onStationsLoaded]);

    useEffect(() => {
      const container = mapContainerRef.current;
      if (!container) return;

      let frameId = 0;
      const refreshMap = () => {
        if (frameId) {
          cancelAnimationFrame(frameId);
        }
        frameId = requestAnimationFrame(() => {
          const map = mapRef.current?.getMap();
          if (!map) return;
          map.resize();
          map.triggerRepaint();
        });
      };

      refreshMap();

      const observer = new ResizeObserver(() => {
        refreshMap();
      });
      observer.observe(container);

      return () => {
        observer.disconnect();
        if (frameId) {
          cancelAnimationFrame(frameId);
        }
      };
    }, []);

    useEffect(() => {
      const neededForHotspots =
        Boolean(hotspotSource) && hotspotGrain === "admin_area";
      if (
        (spatialPartitionMode !== "municipality" && !neededForHotspots) ||
        municipalities
      )
        return;

      const controller = new AbortController();
      fetch("/api/boundaries?type=municipalities", {
        signal: controller.signal,
        cache: "force-cache",
      })
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json();
        })
        .then((payload) => setMunicipalities(payload))
        .catch((err) => {
          if (err.name !== "AbortError") {
            console.warn("Municipality boundaries unavailable:", err);
          }
        });

      return () => controller.abort();
    }, [municipalities, spatialPartitionMode, hotspotSource, hotspotGrain]);

    useEffect(() => {
      if (spatialPartitionMode !== "municipality" || !provinces) return;

      const frameId = requestAnimationFrame(() => {
        const map = mapRef.current?.getMap();
        if (!map) return;
        map.resize();
        map.triggerRepaint();
      });

      return () => {
        cancelAnimationFrame(frameId);
      };
    }, [provinces, spatialPartitionMode]);

    useEffect(() => {
      const neededForHotspots =
        Boolean(hotspotSource) && hotspotGrain === "grid";
      if (
        partitionGridGeojson ||
        (spatialPartitionMode !== "grid" && !neededForHotspots)
      )
        return;

      const controller = new AbortController();
      fetch(`/api/grid-cells?grid_size_km=${DEFAULT_GEE_DISPLAY_GRID_SIZE_KM}`, {
        signal: controller.signal,
        cache: "force-cache",
      })
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json();
        })
        .then((payload: CanonicalGridResponse) => {
          const features = (payload.cells ?? []).map((cell) => {
            const geometry =
              cell.cell_geojson?.type && cell.cell_geojson?.coordinates
                ? {
                    type: cell.cell_geojson.type,
                    coordinates: cell.cell_geojson.coordinates,
                  }
                : {
                    type: "Polygon",
                    coordinates: [
                      [
                        [cell.bbox_min_lon, cell.bbox_min_lat],
                        [cell.bbox_max_lon, cell.bbox_min_lat],
                        [cell.bbox_max_lon, cell.bbox_max_lat],
                        [cell.bbox_min_lon, cell.bbox_max_lat],
                        [cell.bbox_min_lon, cell.bbox_min_lat],
                      ],
                    ],
                  };

            return {
              type: "Feature",
              geometry,
              properties: {
                grid_cell_id: cell.grid_cell_id,
                grid_system: cell.grid_system,
                lat: cell.lat,
                lon: cell.lon,
                grid_size_km: cell.grid_size_km,
                station_count: cell.station_count,
                total_detections: cell.total_detections,
              },
            };
          });

          setPartitionGridGeojson({
            type: "FeatureCollection",
            features,
          });
          setPartitionGridNeedsCountryClip(
            payload.geometryClippedToCountry !== true,
          );
        })
        .catch((err) => {
          if (err.name !== "AbortError")
            console.warn("Canonical grid unavailable:", err);
        });

      return () => controller.abort();
    }, [
      partitionGridGeojson,
      spatialPartitionMode,
      hotspotSource,
      hotspotGrain,
    ]);

    // Fetch pre-decimated Movebank tracks when the overlay is enabled
    useEffect(() => {
      if (!showMovebank) {
        setMovebankTracksGeo(null);
        setSelectedMovebankTrack(null);
        return;
      }
      const controller = new AbortController();
      const params = new URLSearchParams();
      for (const sp of movebankSpeciesFilter ?? []) {
        if (sp) params.append("species", sp);
      }
      if (movebankWindowDays != null) {
        params.set("windowDays", String(movebankWindowDays));
      }
      fetch(`/api/migration-tracks?${params.toString()}`, {
        signal: controller.signal,
      })
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        })
        .then((data: GeoJSON.FeatureCollection) => {
          setMovebankTracksGeo(data);
          const species = [
            ...new Set(
              data.features
                .map((feature) => {
                  const candidate = readFeatureProperties(feature).species;
                  return typeof candidate === "string" ? candidate : undefined;
                })
                .filter((s): s is string => Boolean(s)),
            ),
          ].sort();
          onTracksLoaded?.(species);
        })
        .catch((err) => {
          if (err.name !== "AbortError")
            console.warn("Movebank tracks unavailable:", err);
        });
      return () => controller.abort();
    }, [showMovebank, movebankSpeciesFilter, movebankWindowDays, onTracksLoaded]);

    useEffect(() => {
      if (!dataReady || earthEngineOverlayData) return;
      if (!earthEngineOverlay) return;

      const controller = new AbortController();
      const timerId = window.setTimeout(() => {
        fetch(`/api/earth-engine-overlays?grid_size_km=${DEFAULT_GEE_DISPLAY_GRID_SIZE_KM}`, {
          signal: controller.signal,
          cache: "force-cache",
        })
          .then((r) => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
          })
          .then((data: EarthEngineOverlayResponse) =>
            setEarthEngineOverlayData(data),
          )
          .catch((err) => {
            if (err.name !== "AbortError")
              console.warn("Earth Engine overlays unavailable:", err);
          });
      }, 0);

      return () => {
        window.clearTimeout(timerId);
        controller.abort();
      };
    }, [
      dataReady,
      earthEngineOverlay,
      earthEngineOverlayData,
    ]);

    const buildCachedEarthEngineBundle = useCallback(
      (overlayId: EarthEngineOverlayId, mode: EarthEngineVisualizationMode) => {
        if (!earthEngineOverlayData?.samples) return null;
        if (mode === "municipality" && !municipalities) return null;

        return buildEarthEngineOverlayBundle(
          earthEngineOverlayData.samples,
          overlayId,
          {
            samplingMode: earthEngineOverlayData.samplingMode,
            visualizationMode: mode,
            regions:
              mode === "municipality"
                ? (municipalities as OverlayRegionCollection | null)
                : null,
          },
        );
      },
      [earthEngineOverlayData, municipalities],
    );

    const clippedPartitionGridGeojson = useMemo(
      () => {
        if (!countryBorderGeojson) {
          return partitionGridGeojson;
        }

        if (!partitionGridNeedsCountryClip) {
          return filterGridFeatureCollectionToCountry(
            partitionGridGeojson,
            countryBorderGeojson,
          );
        }

        return clipFeatureCollectionToCountry(
          partitionGridGeojson,
          countryBorderGeojson,
        );
      },
      [
        countryBorderGeojson,
        partitionGridGeojson,
        partitionGridNeedsCountryClip,
      ],
    );

    useEffect(() => {
      if (!hotspotSource) {
        setHotspotSummary(null);
        return;
      }

      const regionsReady =
        hotspotGrain === "grid"
          ? Boolean(clippedPartitionGridGeojson?.features?.length)
          : Boolean(municipalities?.features?.length);
      if (!regionsReady) {
        setHotspotSummary(null);
        return;
      }

      const controller = new AbortController();
      const params = new URLSearchParams({
        source: hotspotSource,
        grain: hotspotGrain,
        week: hotspotWeek,
      });
      fetch(`/api/alerts/hotspots?${params.toString()}`, {
        signal: controller.signal,
      })
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json() as Promise<HotspotSummaryResponse>;
        })
        .then((payload) => {
          setHotspotSummary(payload);
          onHotspotMetaChange?.({
            availableWeeks: payload.coverage?.availableWeeks ?? [],
            selectedWeek: payload.coverage?.selectedWeek ?? null,
          });
        })
        .catch((err) => {
          if (err.name === "AbortError") return;
          console.warn("Hotspot overlay unavailable:", err);
          setHotspotSummary(null);
        });

      return () => controller.abort();
    }, [
      clippedPartitionGridGeojson,
      hotspotGrain,
      hotspotSource,
      hotspotWeek,
      municipalities,
      onHotspotMetaChange,
    ]);

    const clippedGridGeometryById = useMemo(() => {
      const lookup: Record<string, OverlayFeature["geometry"]> = {};
      for (const feature of clippedPartitionGridGeojson?.features ?? []) {
        const gridCellId = feature?.properties?.grid_cell_id;
        if (!gridCellId || !feature?.geometry) continue;
        lookup[String(gridCellId)] =
          feature.geometry as OverlayFeature["geometry"];
      }
      return lookup;
    }, [clippedPartitionGridGeojson]);

    const buildVisibleEarthEngineBundle = useCallback(
      (overlayId: EarthEngineOverlayId, mode: EarthEngineVisualizationMode) => {
        const rawBundle = buildCachedEarthEngineBundle(overlayId, mode);
        if (!rawBundle) return null;
        if (mode !== "grid") return rawBundle;

        const hasClippedGrid = Object.keys(clippedGridGeometryById).length > 0;
        if (!hasClippedGrid) return rawBundle;

        const visibleCells: OverlayFeatureCollection | null = rawBundle.cells
          ? {
              ...rawBundle.cells,
              features: rawBundle.cells.features
                .map((feature) => {
                  const gridCellId = String(
                    feature.properties.grid_cell_id ??
                      feature.properties.station_id ??
                      "",
                  );
                  const geometry = clippedGridGeometryById[gridCellId];
                  if (!geometry) return null;
                  return {
                    ...feature,
                    geometry,
                  } satisfies OverlayFeature;
                })
                .filter((feature): feature is OverlayFeature =>
                  Boolean(feature),
                ),
            }
          : null;

        const hasVisibleGridId = (
          properties: Record<string, string | number | boolean | null>,
        ) => {
          const gridCellId = properties.grid_cell_id ?? properties.station_id;
          return (
            typeof gridCellId === "string" &&
            Boolean(clippedGridGeometryById[gridCellId])
          );
        };

        const visiblePoints = rawBundle.points
          ? {
              ...rawBundle.points,
              features: rawBundle.points.features.filter((feature) =>
                hasVisibleGridId(feature.properties),
              ),
            }
          : null;

        const visibleLabels = rawBundle.labels
          ? {
              ...rawBundle.labels,
              features: rawBundle.labels.features.filter((feature) =>
                hasVisibleGridId(feature.properties),
              ),
            }
          : null;

        return {
          ...rawBundle,
          cells:
            visibleCells && visibleCells.features.length > 0
              ? visibleCells
              : null,
          points:
            visiblePoints && visiblePoints.features.length > 0
              ? visiblePoints
              : null,
          labels:
            visibleLabels && visibleLabels.features.length > 0
              ? visibleLabels
              : null,
          summary: {
            ...rawBundle.summary,
            renderedFeatures: visibleCells?.features.length ?? 0,
          },
        };
      },
      [buildCachedEarthEngineBundle, clippedGridGeometryById],
    );

    useEffect(() => {
      earthEngineBundleCacheRef.current = {};
    }, [earthEngineOverlayData, municipalities]);

    useEffect(() => {
      if (!earthEngineOverlayData?.samples) return;
      if (spatialPartitionMode === "municipality" && !municipalities) return;

      const eagerOverlays = EARTH_ENGINE_OVERLAY_ORDER.slice(0, 3);
      const overlays = EARTH_ENGINE_OVERLAY_ORDER.filter(
        (overlayId) => !eagerOverlays.includes(overlayId),
      );
      let cancelled = false;
      let idleHandle: number | null = null;
      let timeoutHandle: ReturnType<typeof globalThis.setTimeout> | null = null;

      for (const overlayId of eagerOverlays) {
        const cacheKey = `${overlayId}:${spatialPartitionMode}`;
        if (!(cacheKey in earthEngineBundleCacheRef.current)) {
          earthEngineBundleCacheRef.current[cacheKey] =
            buildCachedEarthEngineBundle(overlayId, spatialPartitionMode);
        }
      }

      const scheduleNext = () => {
        if (cancelled || overlays.length === 0) return;

        if (
          typeof window !== "undefined" &&
          typeof window.requestIdleCallback === "function"
        ) {
          idleHandle = window.requestIdleCallback(
            () => {
              idleHandle = null;
              runNext();
            },
            { timeout: 240 },
          );
        } else {
          timeoutHandle = globalThis.setTimeout(() => {
            timeoutHandle = null;
            runNext();
          }, 40);
        }
      };

      const runNext = () => {
        if (cancelled) return;
        const overlayId = overlays.shift();
        if (!overlayId) return;

        const cacheKey = `${overlayId}:${spatialPartitionMode}`;
        if (!(cacheKey in earthEngineBundleCacheRef.current)) {
          earthEngineBundleCacheRef.current[cacheKey] =
            buildCachedEarthEngineBundle(overlayId, spatialPartitionMode);
        }
        if (!(cacheKey in earthEngineVisibleBundleCacheRef.current)) {
          earthEngineVisibleBundleCacheRef.current[cacheKey] =
            buildVisibleEarthEngineBundle(overlayId, spatialPartitionMode);
        }
        if (earthEngineOverlay === overlayId) {
          setEarthEngineVisibleBundleCacheVersion((version) => version + 1);
        }

        scheduleNext();
      };

      scheduleNext();

      return () => {
        cancelled = true;
        if (
          idleHandle != null &&
          typeof window !== "undefined" &&
          typeof window.cancelIdleCallback === "function"
        ) {
          window.cancelIdleCallback(idleHandle);
        }
        if (timeoutHandle != null) {
          globalThis.clearTimeout(timeoutHandle);
        }
      };
    }, [
      buildCachedEarthEngineBundle,
      buildVisibleEarthEngineBundle,
      earthEngineOverlay,
      earthEngineOverlayData,
      municipalities,
      spatialPartitionMode,
    ]);

    const earthEngineOverlayBundle = useMemo(() => {
      if (!earthEngineOverlay) return null;

      const cacheKey = `${earthEngineOverlay}:${spatialPartitionMode}`;
      const cached = earthEngineVisibleBundleCacheRef.current[cacheKey];
      if (cached !== undefined) return cached;

      const built = buildVisibleEarthEngineBundle(
        earthEngineOverlay,
        spatialPartitionMode,
      );
      earthEngineVisibleBundleCacheRef.current[cacheKey] = built;
      return built;
    }, [
      buildVisibleEarthEngineBundle,
      earthEngineOverlay,
      earthEngineVisibleBundleCacheVersion,
      spatialPartitionMode,
    ]);

    const gridOverlayBundle = null;

    const hotspotOverlayGeojson = useMemo(
      () =>
        buildHotspotOverlayFeatureCollection({
          regions:
            hotspotGrain === "grid"
              ? clippedPartitionGridGeojson
              : municipalities,
          hotspots: hotspotSummary?.hotspots ?? [],
          grain: hotspotGrain,
          source: hotspotSource ?? "bioacoustic",
        }),
      [
        clippedPartitionGridGeojson,
        hotspotGrain,
        hotspotSource,
        hotspotSummary,
        municipalities,
      ],
    );

    useEffect(() => {
      earthEngineVisibleBundleCacheRef.current = {};
      setEarthEngineVisibleBundleCacheVersion((version) => version + 1);
    }, [earthEngineOverlayData, municipalities, clippedPartitionGridGeojson]);

    const gridLookup = useMemo(() => {
      const lookup: Record<string, GridCellLookupEntry> = {};
      for (const feature of partitionGridGeojson?.features ?? []) {
        const gridCellId = feature?.properties?.grid_cell_id;
        if (!gridCellId) continue;
        lookup[String(gridCellId)] = {
          latitude: toFiniteNumber(feature?.properties?.lat) ?? 0,
          longitude: toFiniteNumber(feature?.properties?.lon) ?? 0,
          gridSystem: String(
            feature?.properties?.grid_system ?? "nl_geodesic_5km",
          ),
          gridSizeKm: toFiniteNumber(feature?.properties?.grid_size_km),
          stationCount: toFiniteNumber(feature?.properties?.station_count) ?? 0,
          totalDetections:
            toFiniteNumber(feature?.properties?.total_detections) ?? 0,
        };
      }
      return lookup;
    }, [partitionGridGeojson]);

    useEffect(() => {
      if (!selectedGridCell || spatialPartitionMode !== "grid") {
        setSelectedGridOverview(null);
        setSelectedGridOverviewLoading(false);
        return;
      }

      const gridCellId = selectedGridCell.gridCellId;
      const cached = selectedGridOverviewCacheRef.current[gridCellId];
      if (cached !== undefined) {
        setSelectedGridOverview(cached);
        setSelectedGridOverviewLoading(false);
        return;
      }

      const controller = new AbortController();
      setSelectedGridOverview(null);
      setSelectedGridOverviewLoading(true);

      fetch(
        `/api/grid-cell/overview?grid_cell_id=${encodeURIComponent(gridCellId)}`,
        { signal: controller.signal, cache: "no-store" },
      )
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json() as Promise<GridCellOverviewResponse>;
        })
        .then((payload) => {
          if (controller.signal.aborted) return;
          selectedGridOverviewCacheRef.current[gridCellId] = payload;
          setSelectedGridOverview(payload);
        })
        .catch((error) => {
          if ((error as { name?: string })?.name !== "AbortError") {
            console.warn("Grid cell overview unavailable:", error);
            selectedGridOverviewCacheRef.current[gridCellId] = null;
            setSelectedGridOverview(null);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setSelectedGridOverviewLoading(false);
        });

      return () => controller.abort();
    }, [selectedGridCell, spatialPartitionMode]);

    /** Apply Chaikin smoothing to each track for visual quality */
    const smoothedTracksGeo = useMemo<GeoJSON.FeatureCollection | null>(() => {
      if (!movebankTracksGeo) return null;
      return {
        ...movebankTracksGeo,
        features: movebankTracksGeo.features.map((f) => {
          if (f.geometry.type !== "LineString") return f;
          const coords = dedupeSequentialCoords(
            f.geometry.coordinates as number[][],
          );
          return {
            ...f,
            geometry: { ...f.geometry, coordinates: smoothLine(coords, 1) },
          };
        }),
      };
    }, [movebankTracksGeo]);

    /** Point feature at the latest position of each track (for pulsing heads) */
    const movebankHeadsGeo = useMemo<GeoJSON.FeatureCollection | null>(() => {
      if (!movebankTracksGeo) return null;
      const features = movebankTracksGeo.features
        .filter(
          (f) =>
            f.geometry.type === "LineString" &&
            (f.geometry as GeoJSON.LineString).coordinates.length > 0,
        )
        .map((f) => {
          const coords = (f.geometry as GeoJSON.LineString).coordinates;
          const last = coords[coords.length - 1];
          // Compute approximate bearing from second-to-last → last coordinate
          let bearing = 0;
          if (coords.length >= 2) {
            const prev = coords[coords.length - 2];
            const dLon = last[0] - prev[0];
            const dLat = last[1] - prev[1];
            const midLat = ((prev[1] + last[1]) / 2) * (Math.PI / 180);
            bearing = Math.atan2(dLon * Math.cos(midLat), dLat) * (180 / Math.PI);
          }
          return {
            type: "Feature" as const,
            geometry: { type: "Point" as const, coordinates: last },
            properties: { ...(f.properties as MovebankTrackProps), bearing },
          };
        });
      return { type: "FeatureCollection", features };
    }, [movebankTracksGeo]);

    /** Point feature at the first (origin) position of each track */
    const movebankOriginsGeo = useMemo<GeoJSON.FeatureCollection | null>(() => {
      if (!movebankTracksGeo) return null;
      const features = movebankTracksGeo.features
        .filter(
          (f) =>
            f.geometry.type === "LineString" &&
            (f.geometry as GeoJSON.LineString).coordinates.length > 0,
        )
        .map((f) => {
          const first = (f.geometry as GeoJSON.LineString).coordinates[0];
          return {
            type: "Feature" as const,
            geometry: { type: "Point" as const, coordinates: first },
            properties: { ...(f.properties as MovebankTrackProps) },
          };
        });
      return { type: "FeatureCollection", features };
    }, [movebankTracksGeo]);

    /** Grouped species counts derived from the track data — used for the info panel */
    const movebankSpeciesCounts = useMemo<Record<string, number>>(() => {
      if (!movebankTracksGeo) return {};
      const counts: Record<string, number> = {};
      for (const f of movebankTracksGeo.features) {
        const sp = (f.properties as MovebankTrackProps)?.species ?? "Unknown";
        counts[sp] = (counts[sp] ?? 0) + 1;
      }
      return counts;
    }, [movebankTracksGeo]);

    /** Latest end timestamp across all tracks */
    const movebankLatestTs = useMemo<string | null>(() => {
      if (!movebankTracksGeo?.features.length) return null;
      let latest = "";
      for (const f of movebankTracksGeo.features) {
        const ts = (f.properties as MovebankTrackProps)?.end_ts ?? "";
        if (ts > latest) latest = ts;
      }
      if (!latest) return null;
      return new Intl.DateTimeFormat("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      }).format(new Date(latest));
    }, [movebankTracksGeo]);

    // Station-style pulse animation for Movebank origin dots
    useEffect(() => {
      if (!showMovebank || !movebankOriginsGeo?.features.length) return;
      let rafId: number;
      const animate = () => {
        const t = (Date.now() % 4000) / 4000;
        const pulse = Math.sin(t * Math.PI * 2) * 0.5 + 0.5;
        const map = mapRef.current?.getMap();
        if (map) {
          try {
            if (map.getLayer("movebank-pulse")) {
              map.setPaintProperty(
                "movebank-pulse",
                "circle-stroke-color",
                `rgba(163, 230, 53, ${0.08 + pulse * 0.18})`,
              );
              map.setPaintProperty("movebank-pulse", "circle-radius", [
                "interpolate",
                ["linear"],
                ["zoom"],
                6,
                4 + pulse * 4,
                12,
                8 + pulse * 8,
                18,
                14 + pulse * 14,
              ]);
            }
          } catch {
            /* layer not yet ready */
          }
        }
        rafId = requestAnimationFrame(animate);
      };

      const timer = setTimeout(animate, 500);
      return () => {
        clearTimeout(timer);
        cancelAnimationFrame(rafId);
      };
    }, [showMovebank, movebankOriginsGeo]);

    const handleMove = useCallback(
      (evt: ViewStateChangeEvent) => {
        setViewState(evt.viewState);
        onViewStateChange?.(evt.viewState);

        // Clear hover state while moving the camera significantly
        setHoveredProvinceName(null);
        setHoveredMunicipalityName(null);
        setHoverInfo(null);
      },
      [onViewStateChange],
    );

    const handleZoomEnd = useCallback(() => {
      const map = mapRef.current?.getMap();
      if (!map) return;

      // If the map ends up at a zoomed-out level (State/Country view)
      // and is still tilted from a previous city 3D dive, elegantly flatten it back out.
      // This safely runs after manual scrolls and won't conflict with our programmatic flyTo's.
      if (map.getZoom() <= 9.5 && map.getPitch() > 35) {
        map.easeTo({
          pitch: DEFAULT_PITCH,
          duration: 1200,
        });
      }
    }, []);

    /* ── Station pulse animation (subtle sonar ring) ── */
    useEffect(() => {
      const map = mapRef.current?.getMap();
      if (!map || !dataReady) return;
      let animFrame: number;
      const animate = () => {
        const t = (Date.now() % 4000) / 4000;
        const pulse = Math.sin(t * Math.PI * 2) * 0.5 + 0.5;
        try {
          if (map.getLayer("stations-pulse")) {
            map.setPaintProperty(
              "stations-pulse",
              "circle-stroke-color",
              `rgba(52, 211, 153, ${0.08 + pulse * 0.18})`,
            );
            map.setPaintProperty("stations-pulse", "circle-radius", [
              "interpolate",
              ["linear"],
              ["zoom"],
              6,
              4 + pulse * 4,
              12,
              8 + pulse * 8,
              18,
              14 + pulse * 14,
            ]);
          }
        } catch {
          /* layer not yet ready */
        }
        animFrame = requestAnimationFrame(animate);
      };
      const timer = setTimeout(animate, 500);
      return () => {
        clearTimeout(timer);
        cancelAnimationFrame(animFrame);
      };
    }, [dataReady]);

    /* ── Boot sequence fade-out after data arrives ── */
    useEffect(() => {
      if (!dataReady || bootFading) return;
      const t1 = setTimeout(
        () =>
          setBootMessages((prev) => [...prev, "DETECTION GRID: OPERATIONAL"]),
        400,
      );
      const t2 = setTimeout(() => setBootFading(true), 1200);
      const t3 = setTimeout(() => setBootHidden(true), 2200);
      return () => {
        clearTimeout(t1);
        clearTimeout(t2);
        clearTimeout(t3);
      };
    }, [dataReady, bootFading]);

    useEffect(() => {
      if (renderedSkin === skin) {
        return;
      }

      setSkinTransitioning(true);
      const frameId = requestAnimationFrame(() => {
        setRenderedSkin(skin);
      });

      return () => {
        cancelAnimationFrame(frameId);
      };
    }, [renderedSkin, skin]);

    const handleMapIdle = useCallback(() => {
      if (renderedSkin !== skin) {
        return;
      }

      setSkinTransitioning(false);
    }, [renderedSkin, skin]);

    const onMouseMove = useCallback(
      (event: MapInteractionEvent) => {
        if (!mapRef.current) return;
        const map = mapRef.current.getMap();
        if (!map) return;
        const queryPoint: [number, number] = [event.point.x, event.point.y];

        // Check if we are hovering over a station marker first (they sit above provinces)
        let stationFeatures: MapFeature[] = [];
        try {
          if (map.getLayer("stations-inner-dot")) {
            stationFeatures = map.queryRenderedFeatures(queryPoint, {
              layers: ["stations-inner-dot", "stations-outer-glow"],
            }) as MapFeature[];
          }
        } catch {
          // Layer not yet loaded
        }

        if (stationFeatures.length > 0) {
          map.getCanvas().style.cursor = "pointer";
          setHoveredProvinceName((prev) => (prev !== null ? null : prev));
          setHoveredMunicipalityName((prev) => (prev !== null ? null : prev));
          setHoveredGridCellId((prev) => (prev !== null ? null : prev));
          setHoverInfo((prev) => (prev !== null ? null : prev));
          return;
        }

        if (spatialPartitionMode === "grid") {
          let gridFeatures: MapFeature[] = [];
          try {
            if (
              earthEngineOverlay &&
              map.getLayer("earth-engine-overlay-fill")
            ) {
              gridFeatures = map.queryRenderedFeatures(queryPoint, {
                layers: ["earth-engine-overlay-fill"],
              }) as MapFeature[];
            }
            if (
              gridFeatures.length === 0 &&
              map.getLayer("partition-grid-hitbox")
            ) {
              gridFeatures = map.queryRenderedFeatures(queryPoint, {
                layers: ["partition-grid-hitbox"],
              }) as MapFeature[];
            }
          } catch {
            gridFeatures = [];
          }

          if (gridFeatures.length > 0) {
            const gridSelection = deriveGridSelection(
              gridFeatures[0],
              gridLookup,
            );
            if (gridSelection) {
              setHoveredProvinceName((prev) => (prev !== null ? null : prev));
              setHoveredMunicipalityName((prev) =>
                prev !== null ? null : prev,
              );
              setHoveredGridCellId((prev) =>
                prev !== gridSelection.gridCellId
                  ? gridSelection.gridCellId
                  : prev,
              );
              setHoverInfo({
                x: event.point.x,
                y: event.point.y,
                name: gridSelection.referenceLabel,
                type: "grid",
                meta: `${gridSelection.stationCount} station${gridSelection.stationCount === 1 ? "" : "s"} · ${gridSelection.totalDetections.toLocaleString()} detections`,
              });
              map.getCanvas().style.cursor = "pointer";
              return;
            }
          }

          setHoveredProvinceName((prev) => (prev !== null ? null : prev));
          setHoveredMunicipalityName((prev) => (prev !== null ? null : prev));
          setHoveredGridCellId((prev) => (prev !== null ? null : prev));
          setHoverInfo((prev) => (prev !== null ? null : prev));
          map.getCanvas().style.cursor = "";
          return;
        }

        if (spatialPartitionMode !== "municipality") {
          setHoveredProvinceName((prev) => (prev !== null ? null : prev));
          setHoveredMunicipalityName((prev) => (prev !== null ? null : prev));
          setHoveredGridCellId((prev) => (prev !== null ? null : prev));
          setHoverInfo((prev) => (prev !== null ? null : prev));
          map.getCanvas().style.cursor = "";
          return;
        }

        // Check municipality first (only active when zoomed in and layer is rendered)
        // We must try/catch because queryRenderedFeatures throws if the layer isn't added to the map yet
        let municipalityFeatures: MapFeature[] = [];
        try {
          if (map.getLayer("municipality-fill") && map.getZoom() >= 7.5) {
            municipalityFeatures = map.queryRenderedFeatures(queryPoint, {
              layers: ["municipality-fill"],
            }) as MapFeature[];
          }
        } catch {
          // Layer not yet loaded/rendered, safe to ignore
        }

        if (municipalityFeatures.length > 0) {
          const feature = municipalityFeatures[0];
          const municipalityName = readFeatureName(feature);

          if (municipalityName) {
            setHoveredMunicipalityName((prev) => {
              if (prev !== municipalityName) {
                setHoveredProvinceName(null);
                setHoverInfo({
                  x: event.point.x,
                  y: event.point.y,
                  name: municipalityName,
                  type: "municipality",
                });
                map.getCanvas().style.cursor = "pointer";
              } else {
                // Update tooltip DOM element directly to bypass React render cycle lag
                const tooltip = document.getElementById("map-tooltip");
                if (tooltip) {
                  tooltip.style.left = `${event.point.x}px`;
                  tooltip.style.top = `${event.point.y}px`;
                }
              }
              return municipalityName;
            });
            return; // Return early so we don't also highlight the province underneath
          }
        } else {
          setHoveredMunicipalityName((prev) => (prev !== null ? null : prev));
        }

        let provinceFeatures: MapFeature[] = [];
        try {
          if (map.getLayer("province-fill")) {
            provinceFeatures = map.queryRenderedFeatures(queryPoint, {
              layers: ["province-fill"],
            }) as MapFeature[];
          }
        } catch {
          // Layer not yet loaded/rendered
        }

        if (provinceFeatures.length > 0) {
          const feature = provinceFeatures[0];
          const provinceName = readFeatureName(feature);

          if (provinceName) {
            setHoveredProvinceName((prev) => {
              if (prev !== provinceName) {
                setHoverInfo({
                  x: event.point.x,
                  y: event.point.y,
                  name: provinceName,
                  type: "province",
                });
                map.getCanvas().style.cursor = "pointer";
              } else {
                // Update tooltip DOM element directly to bypass React render cycle lag
                const tooltip = document.getElementById("map-tooltip");
                if (tooltip) {
                  tooltip.style.left = `${event.point.x}px`;
                  tooltip.style.top = `${event.point.y}px`;
                }
              }
              return provinceName;
            });
          }
        } else {
          setHoveredProvinceName((prev) => (prev !== null ? null : prev));
          if (!municipalityFeatures.length) {
            setHoverInfo((prev) => (prev !== null ? null : prev));
            map.getCanvas().style.cursor = "";
          }
        }
      },
      [earthEngineOverlay, gridLookup, spatialPartitionMode],
    );

    const onMouseLeave = useCallback(() => {
      if (!mapRef.current) return;
      const map = mapRef.current.getMap();
      if (!map) return;

      setHoveredProvinceName((prev) => (prev !== null ? null : prev));
      setHoveredMunicipalityName((prev) => (prev !== null ? null : prev));
      setHoveredGridCellId((prev) => (prev !== null ? null : prev));
      setHoverInfo((prev) => (prev !== null ? null : prev));
      map.getCanvas().style.cursor = "";
    }, []);

    const onClick = useCallback(
      (event: MapInteractionEvent) => {
        const map = mapRef.current?.getMap();
        if (!map) return;
        const queryPoint: [number, number] = [event.point.x, event.point.y];

        // Movebank head/track click — check before stations so heads get priority
        if (showMovebank) {
          let mbFeatures: MapFeature[] = [];
          try {
            const checkLayers = (["movebank-track"] as string[])
              .filter((id) => map.getLayer(id));
            if (checkLayers.length > 0) {
              mbFeatures = map.queryRenderedFeatures(queryPoint, {
                layers: checkLayers,
              }) as MapFeature[];
            }
          } catch {
            mbFeatures = [];
          }
          if (mbFeatures.length > 0) {
            const props = mbFeatures[0].properties as MovebankTrackProps;
            // Track geometry coords are stored in the LineString, reconstruct end_point
            setSelectedMovebankTrack({
              ...props,
              // MapLibre flattens array properties as JSON strings
              start_point:
                typeof props.start_point === "string"
                  ? JSON.parse(props.start_point)
                  : props.start_point,
              end_point:
                typeof props.end_point === "string"
                  ? JSON.parse(props.end_point)
                  : props.end_point,
            });
            setSelectedStation(null);
            setSelectedGridCell(null);
            return;
          }
        }

        // Dismiss the movebank popup if the user clicks elsewhere
        setSelectedMovebankTrack(null);

        // Check if we clicked a station first
        let stationFeatures: MapFeature[] = [];
        try {
          if (map.getLayer("stations-inner-dot")) {
            stationFeatures = map.queryRenderedFeatures(queryPoint, {
              layers: ["stations-inner-dot", "stations-outer-glow"],
            }) as MapFeature[];
          }
        } catch {
          // Layer not yet loaded
        }

        if (stationFeatures.length > 0) {
          const properties = readFeatureProperties(stationFeatures[0]);
          const matchedStation = stations.find(
            (station) => station.id === String(properties.id),
          );

          setSelectedStation(
            matchedStation ?? {
              id: String(properties.id ?? ""),
              name: String(properties.name ?? "Unknown station"),
              type: String(properties.type ?? "unknown"),
              country: String(properties.country ?? ""),
              state: String(properties.state ?? ""),
              coords: {
                lat: Number(properties.lat) || event.lngLat.lat,
                lon: Number(properties.lon) || event.lngLat.lng,
              },
              stats: normalizeStationStats(properties.stats),
            },
          );
          setSelectedGridCell(null);
          return;
        }

        if (spatialPartitionMode === "grid") {
          let gridFeatures: MapFeature[] = [];
          try {
            if (
              earthEngineOverlay &&
              map.getLayer("earth-engine-overlay-fill")
            ) {
              gridFeatures = map.queryRenderedFeatures(queryPoint, {
                layers: ["earth-engine-overlay-fill"],
              }) as MapFeature[];
            }
            if (
              gridFeatures.length === 0 &&
              map.getLayer("partition-grid-hitbox")
            ) {
              gridFeatures = map.queryRenderedFeatures(queryPoint, {
                layers: ["partition-grid-hitbox"],
              }) as MapFeature[];
            }
          } catch {
            gridFeatures = [];
          }

          if (gridFeatures.length > 0) {
            const gridSelection = deriveGridSelection(
              gridFeatures[0],
              gridLookup,
            );
            if (gridSelection) {
              setSelectedStation(null);
              setSelectedGridCell(gridSelection);
              return;
            }
          }

          setSelectedStation(null);
          setSelectedGridCell(null);
          return;
        }

        if (spatialPartitionMode !== "municipality") {
          setSelectedStation(null);
          setSelectedGridCell(null);
          return;
        }

        // Check if we clicked a municipality first
        let municipalityFeatures: MapFeature[] = [];
        try {
          if (map.getLayer("municipality-fill") && map.getZoom() >= 7.5) {
            municipalityFeatures = map.queryRenderedFeatures(queryPoint, {
              layers: ["municipality-fill"],
            }) as MapFeature[];
          }
        } catch {
          // Layer not yet loaded/rendered
        }

        if (municipalityFeatures.length > 0) {
          const feature = municipalityFeatures[0];
          const municipalityName = readFeatureName(feature);
          if (
            municipalities &&
            municipalities.features &&
            municipalityName
          ) {
            const fullFeature = findFeatureByName(municipalities, municipalityName);

            if (fullFeature) {
              const [minLng, minLat, maxLng, maxLat] = bbox(fullFeature);

              mapRef.current?.fitBounds(
                [
                  [minLng, minLat],
                  [maxLng, maxLat],
                ],
                {
                  padding: 40,
                  duration: 2000,
                  pitch: 60, // Dramatic 3D perspective
                  bearing: 0,
                  maxZoom: 14, // Cap the fitBounds zoom so it doesn't dive too deep
                },
              );
            }
          }
          return; // Stop here so we don't zoom out to the whole province
        }

        let provinceFeatures: MapFeature[] = [];
        try {
          if (map.getLayer("province-fill")) {
            provinceFeatures = map.queryRenderedFeatures(queryPoint, {
              layers: ["province-fill"],
            }) as MapFeature[];
          }
        } catch {
          // Layer not yet loaded
        }

        // If we clicked on a province feature and not on a marker
        if (provinceFeatures.length > 0) {
          const feature = provinceFeatures[0];
          const provinceName = readFeatureName(feature);
          // Note: queryRenderedFeatures only gives us the visible slice of the geometry.
          // To get the true bounding box, we should find the feature in our original `provinces` dataset.
          if (provinces && provinces.features && provinceName) {
            const fullFeature = findFeatureByName(provinces, provinceName);

            if (fullFeature) {
              const [minLng, minLat, maxLng, maxLat] = bbox(fullFeature);
              mapRef.current?.fitBounds(
                [
                  [minLng, minLat],
                  [maxLng, maxLat],
                ],
                {
                  padding: 40,
                  duration: 1500,
                  pitch: DEFAULT_PITCH, // Keep the state map flat so city names are perfectly legible
                  bearing: 0,
                },
              );
            }
          }
        } else {
          // Clicked on empty space (or rather, the mask) - deselect station
          setSelectedStation(null);
          setSelectedGridCell(null);
        }
      },
      [
        earthEngineOverlay,
        gridLookup,
        municipalities,
        provinces,
        spatialPartitionMode,
        stations,
        showMovebank,
      ],
    );

    return (
      <div ref={mapContainerRef} className="relative w-full h-full">
        {/* ── Boot sequence terminal overlay ── */}
        {!bootHidden && (
          <div
            className={`absolute inset-0 z-50 bg-surface-primary/95 backdrop-blur-sm flex flex-col justify-end p-6 transition-opacity duration-1000 ${bootFading ? "opacity-0 pointer-events-none" : "opacity-100"}`}
          >
            <div className="mb-5 flex items-center gap-3 text-brand-200/70">
              <span className="h-5 w-5 animate-spin rounded-full border-2 border-brand-500/25 border-t-brand-400" />
              <span className="font-mono text-[10px] uppercase tracking-[0.22em]">
                Loading live map
              </span>
            </div>
            <div className="font-mono text-[10px] leading-loose tracking-wider">
              {bootMessages.map((msg, i) => (
                <div
                  key={`boot-${i}`}
                  className="flex items-center gap-2"
                  style={{
                    opacity: 0,
                    animation: "boot-fade-in 0.3s ease-out forwards",
                    animationDelay: `${i * 0.18}s`,
                  }}
                >
                  <span className="text-brand-500/50">▸</span>
                  <span
                    className={
                      msg.includes("OPERATIONAL")
                        ? "text-brand-400 font-semibold"
                        : "text-brand-200/70"
                    }
                  >
                    {msg}
                  </span>
                </div>
              ))}
              <span
                className="inline-block w-1.5 h-3 bg-brand-400/80 ml-4 mt-1"
                style={{
                  animation: "boot-cursor-blink 0.8s step-end infinite",
                }}
              />
            </div>
          </div>
        )}

        {skinTransitioning && bootHidden && (
          <div className="absolute inset-0 z-40 flex items-center justify-center bg-surface-primary/88 backdrop-blur-sm pointer-events-none">
            <div className="flex items-center gap-3 rounded-2xl border border-white/10 bg-black/25 px-4 py-3 shadow-[0_18px_48px_rgba(0,0,0,0.38)]">
              <span className="h-5 w-5 animate-spin rounded-full border-2 border-brand-500/25 border-t-brand-400" />
              <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-brand-100/80">
                Applying {requestedSkinMeta.label}
              </span>
            </div>
          </div>
        )}

        {/* ── Signal noise (faint static over entire map) ── */}
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none z-[5]"
          style={{ opacity: 0.025, mixBlendMode: "overlay" }}
          aria-hidden="true"
        >
          <filter id="map-noise">
            <feTurbulence
              type="fractalNoise"
              baseFrequency="0.65"
              numOctaves="3"
              stitchTiles="stitch"
            />
          </filter>
          <rect width="100%" height="100%" filter="url(#map-noise)" />
        </svg>
        <Map
          ref={mapRef}
          {...viewState}
          onMove={handleMove}
          onZoomEnd={handleZoomEnd}
          interactiveLayerIds={[
            ...(spatialPartitionMode === "grid"
              ? ["earth-engine-overlay-fill", "partition-grid-hitbox"]
              : ["province-fill", "municipality-fill"]),
            ...(showMovebank ? ["movebank-track"] : []),
          ]}
          onMouseMove={onMouseMove}
          onMouseLeave={onMouseLeave}
          onClick={onClick}
          onIdle={handleMapIdle}
          mapStyle={mapSkin}
          styleDiffing={false}
          maxBounds={[
            NETHERLANDS_BOUNDS[0] - 2,
            NETHERLANDS_BOUNDS[1] - 1,
            NETHERLANDS_BOUNDS[2] + 2,
            NETHERLANDS_BOUNDS[3] + 1,
          ]}
          minZoom={6}
          maxZoom={15}
          attributionControl={false}
          style={{ width: "100%", height: "100%" }}
        >
          {/* 1. Regular partition grid overlay */}
          {clippedPartitionGridGeojson && spatialPartitionMode === "grid" && (
            <Source
              id="partition-grid"
              type="geojson"
              data={clippedPartitionGridGeojson}
            >
              <Layer {...partitionGridHitbox} />
              <Layer
                {...partitionGridSelectedFill}
                filter={[
                  "==",
                  ["get", "grid_cell_id"],
                  selectedGridCell?.gridCellId ?? "",
                ]}
              />
              <Layer
                {...partitionGridHoverFill}
                filter={[
                  "==",
                  ["get", "grid_cell_id"],
                  hoveredGridCellId ?? "",
                ]}
              />
              {!earthEngineOverlay && (
                <Layer {...partitionGridLine} />
              )}
              <Layer
                {...partitionGridHoverLine}
                filter={[
                  "==",
                  ["get", "grid_cell_id"],
                  hoveredGridCellId ?? "",
                ]}
              />
              <Layer
                {...partitionGridSelectedLine}
                filter={[
                  "==",
                  ["get", "grid_cell_id"],
                  selectedGridCell?.gridCellId ?? "",
                ]}
              />
            </Source>
          )}

          {/* 1.5 Earth Engine environmental overlays */}
          {earthEngineOverlay && earthEngineOverlayBundle?.cells && (
            <Source
              id="earth-engine-overlay-cells"
              type="geojson"
              data={earthEngineOverlayBundle.cells}
            >
              <Layer {...earthEngineOverlayFill} />
              <Layer
                {...earthEngineOverlaySelectedFill}
                filter={[
                  "==",
                  ["get", "grid_cell_id"],
                  selectedGridCell?.gridCellId ?? "",
                ]}
              />
              <Layer
                {...earthEngineOverlayHoverFill}
                filter={[
                  "==",
                  ["get", "grid_cell_id"],
                  hoveredGridCellId ?? "",
                ]}
              />
              <Layer {...earthEngineOverlayLine} />
              <Layer
                {...earthEngineOverlayHoverLine}
                filter={[
                  "==",
                  ["get", "grid_cell_id"],
                  hoveredGridCellId ?? "",
                ]}
              />
              <Layer
                {...earthEngineOverlaySelectedLine}
                filter={[
                  "==",
                  ["get", "grid_cell_id"],
                  selectedGridCell?.gridCellId ?? "",
                ]}
              />
            </Source>
          )}
          {earthEngineOverlay && earthEngineOverlayBundle?.points && (
            <Source
              id="earth-engine-overlay-points"
              type="geojson"
              data={earthEngineOverlayBundle.points}
            >
              <Layer {...earthEngineOverlayPointGlow} />
              <Layer {...earthEngineOverlayPointCore} />
            </Source>
          )}
          {earthEngineOverlay && earthEngineOverlayBundle?.labels && (
            <Source
              id="earth-engine-overlay-labels"
              type="geojson"
              data={earthEngineOverlayBundle.labels}
            >
              <Layer {...earthEngineOverlayLabel} />
            </Source>
          )}

          {hotspotOverlayGeojson && (
            <Source
              id="hotspot-overlay-cells"
              type="geojson"
              data={hotspotOverlayGeojson}
            >
              <Layer {...hotspotOverlayGlow} />
              <Layer {...hotspotOverlayFill} />
              <Layer {...hotspotOverlayLine} />
            </Source>
          )}

          {/* 1.9 Weather overlay (explore-weather visual, over satellite) */}
          <WeatherOverlay overlay={weatherOverlay} provinces={provinces} />

          {/* 2. Province overlays */}
          {spatialPartitionMode === "municipality" && provinces && (
            <Source id="provinces" type="geojson" data={provinces}>
              <Layer {...provinceFill} />
              <Layer
                {...provinceHighlight}
                filter={["==", ["get", "naam"], hoveredProvinceName || ""]}
              />
              <Layer {...administrativeLayers.provinceGlow} />
              <Layer {...administrativeLayers.provinceLine} />
              <Layer {...administrativeLayers.provinceLabels} />
            </Source>
          )}

          {/* 2.5 Municipality boundaries (fade in at zoom > 8.5) */}
          {spatialPartitionMode === "municipality" && municipalities && (
            <Source id="municipalities" type="geojson" data={municipalities}>
              <Layer {...municipalityFill} />
              <Layer
                {...municipalityHighlight}
                filter={["==", ["get", "naam"], hoveredMunicipalityName || ""]}
              />
              <Layer {...administrativeLayers.municipalityGlow} />
              <Layer {...administrativeLayers.municipalityLine} />
            </Source>
          )}

          {/* 3. Live Detection Nodes (WebGL rendered for 60fps) */}
          {showStations && filteredGeojson && (
            <Source id="stations" type="geojson" data={filteredGeojson}>
              <Layer {...stationLayers.pulse} />
              <Layer {...stationLayers.outerGlow} />
              <Layer {...stationLayers.innerDot} />
            </Source>
          )}

          {/* 4. Movebank animated bird tracks — rendered AFTER country mask so they show outside NL */}

          {/* Country mask + border — migration tracks render above this so they're visible globally */}
          {countryMaskGeojson && (
            <Source id="country-mask" type="geojson" data={countryMaskGeojson}>
              <Layer {...countryMask} />
            </Source>
          )}
          {countryBorderGeojson && (
            <Source
              id="country-border"
              type="geojson"
              data={countryBorderGeojson}
            >
              <Layer {...countryBorder} />
            </Source>
          )}

          {/* Migration track lines, origin dots and ripple rings above the mask */}
          {showMovebank && smoothedTracksGeo && (
            <Source
              id="movebank-tracks"
              type="geojson"
              data={smoothedTracksGeo}
              lineMetrics={true}
            >
              <Layer {...movebankGlowLayer} />
              <Layer {...movebankTrackLayer} />
              <Layer {...movebankFadeLayer} />
            </Source>
          )}
          {showMovebank && movebankOriginsGeo && (
            <Source
              id="movebank-origins"
              type="geojson"
              data={movebankOriginsGeo}
            >
              <Layer {...movebankPulseLayer} />
              <Layer {...movebankOuterGlowLayer} />
              <Layer {...movebankOriginLayer} />
            </Source>
          )}

          {/* Animated bird Markers at the latest position of each track */}
          {showMovebank &&
            movebankHeadsGeo?.features.map((f, idx) => {
              const [lon, lat] = (f.geometry as GeoJSON.Point).coordinates;
              const props = f.properties as MovebankTrackProps & { bearing?: number };
              // CSS rotation: bird SVG faces right (east); rotate so it faces the flight direction
              const cssRotation = (props.bearing ?? 0) - 90;
              return (
                <Marker key={idx} longitude={lon} latitude={lat} anchor="center">
                  <div
                    style={{ transform: `rotate(${cssRotation}deg)`, cursor: "pointer" }}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelectedMovebankTrack({
                        ...props,
                        start_point:
                          typeof props.start_point === "string"
                            ? JSON.parse(props.start_point)
                            : props.start_point,
                        end_point:
                          typeof props.end_point === "string"
                            ? JSON.parse(props.end_point)
                            : props.end_point,
                      });
                      setSelectedStation(null);
                      setSelectedGridCell(null);
                    }}
                  >
                    {/*
                     * Bird SVG faces RIGHT (east) in its original artwork.
                     * The outer div rotates by (bearing - 90) so it points
                     * toward the actual flight direction.
                     */}
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src="/icons/bird-flying.svg"
                      width={18}
                      height={28}
                      alt=""
                      aria-hidden="true"
                      className="movebank-bird-marker"
                    />
                  </div>
                </Marker>
              );
            })}

          {/* Custom Hover Tooltip for Provinces and Municipalities */}
          {hoverInfo && !selectedStation && !selectedGridCell && (
            <div
              id="map-tooltip"
              className="absolute z-50 pointer-events-none transform -translate-x-1/2 -translate-y-[calc(100%+12px)] px-3 py-2 bg-surface-primary/80 backdrop-blur-md border border-brand-500/30 rounded-md shadow-[0_0_15px_rgba(16,185,129,0.15)] will-change-transform"
              style={{ left: hoverInfo.x, top: hoverInfo.y }}
            >
              <div className="text-[10px] font-mono text-text-muted/70 uppercase tracking-widest mb-1">
                {hoverInfo.type === "grid"
                  ? "Grid Cell"
                  : hoverInfo.type === "municipality"
                    ? "Municipality Sector"
                    : "Province Sector"}
              </div>
              <div className="text-sm font-semibold tracking-wide text-brand-50">
                {hoverInfo.name}
              </div>
              <div className="mt-1.5 flex items-center gap-2">
                <div className="w-1.5 h-1.5 bg-brand-400 rounded-full shadow-[0_0_5px_#34d399] animate-pulse"></div>
                <span className="text-[10px] text-brand-300/80 font-mono tracking-wider">
                  {hoverInfo.meta ?? "SECURE"}
                </span>
              </div>
            </div>
          )}
          {/* Custom Station Popup */}
          {selectedStation && (
            <Popup
              longitude={selectedStation.coords.lon}
              latitude={selectedStation.coords.lat}
              closeButton={true}
              closeOnClick={false}
              onClose={() => setSelectedStation(null)}
              anchor="bottom"
              maxWidth="340px"
              offset={14}
              className="station-popup"
            >
              <div className="w-[320px] rounded-md border border-white/10 bg-[rgba(20,20,20,0.94)] p-3.5 text-text-primary shadow-[0_8px_32px_rgba(0,0,0,0.8)] backdrop-blur-xl">
                {(() => {
                  const stats = selectedStation.stats;
                  const unique = stats.unique_days ?? 0;
                  const span = stats.coverage_span_days ?? 0;
                  // Window-based availability: active days over the station's own reporting
                  // window (first→last detection), NOT to today — so the empty stretch into
                  // 2026 (no data yet) doesn't drag a 2024–2025 station down to ~0%.
                  const quality =
                    span > 0 && unique > 0 ? Math.min(100, Math.round((100 * unique) / span)) : 0;
                  const qColor = quality >= 70 ? "#34d399" : quality >= 40 ? "#fbbf24" : "#f87171";
                  const filled = Math.max(quality > 0 ? 1 : 0, Math.round(quality / 20));

                  const monthlyEntries = Object.entries(stats.monthly_detections ?? {})
                    .map(([m, v]) => [m, Number(v)] as const)
                    .filter(([, v]) => Number.isFinite(v))
                    .sort(([a], [b]) => a.localeCompare(b));
                  const availableMonths = monthlyEntries.length;
                  const monthlyAvg =
                    availableMonths > 0
                      ? Math.round(monthlyEntries.reduce((s, [, v]) => s + v, 0) / availableMonths)
                      : null;
                  const totalDet = stats.total_detections ?? 0;
                  const confidence =
                    stats.avg_confidence != null ? `${(stats.avg_confidence * 100).toFixed(0)}%` : "—";

                  const metrics = [
                    { label: "Total detections", value: totalDet.toLocaleString() },
                    { label: "Avg. monthly", value: monthlyAvg != null ? monthlyAvg.toLocaleString() : "—" },
                    { label: "Active days", value: unique.toLocaleString() },
                    { label: "Span (days)", value: span > 0 ? span.toLocaleString() : "—" },
                    { label: "Confidence", value: confidence },
                    { label: "Species", value: (stats.unique_species ?? 0).toLocaleString() },
                  ];

                  return (
                    <>
                      {/* identity */}
                      <div className="flex items-stretch gap-3">
                        <PointSatelliteThumb
                          lat={selectedStation.coords.lat}
                          lon={selectedStation.coords.lon}
                          size={76}
                        />
                        <div className="flex min-w-0 flex-1 flex-col justify-center pr-5">
                          <div className="flex items-center gap-2">
                            <h3 className="truncate font-mono text-[13px] font-semibold tracking-[0.04em] text-brand-100">
                              {selectedStation.name}
                            </h3>
                            <span className="shrink-0 rounded border border-brand-500/20 bg-brand-500/10 px-1 py-0.5 text-[8px] font-mono uppercase tracking-wider text-brand-300">
                              {getStationTypeMeta(selectedStation.type).shortLabel}
                            </span>
                          </div>
                          <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-[9px] font-mono uppercase tracking-wider text-text-muted/65">
                            <span>#{selectedStation.id}</span>
                            {(selectedStation.state || selectedStation.country) && (
                              <span className="normal-case tracking-normal text-text-muted/55">
                                {[selectedStation.state, selectedStation.country].filter(Boolean).join(" · ")}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>

                      {/* reliability strip — window availability + live status */}
                      <div className="mt-3 flex items-center gap-2">
                        <span className="shrink-0 text-[8px] font-mono uppercase tracking-[0.16em] text-text-muted/55">
                          Quality
                        </span>
                        <div className="flex flex-1 gap-px">
                          {[0, 1, 2, 3, 4].map((i) => (
                            <div
                              key={i}
                              className="h-1.5 flex-1 rounded-sm"
                              style={{ background: i < filled ? qColor : "rgba(255,255,255,0.06)" }}
                            />
                          ))}
                        </div>
                        <span className="shrink-0 font-mono text-[11px] font-bold tabular-nums" style={{ color: qColor }}>
                          {quality}%
                        </span>
                      </div>

                      {/* metric grid */}
                      <div className="mt-3 grid grid-cols-3 overflow-hidden rounded-md border border-white/8 bg-white/[0.02]">
                        {metrics.map((m, i) => (
                          <div
                            key={m.label}
                            className={`px-2 py-1.5 ${i % 3 !== 0 ? "border-l border-white/[0.06]" : ""} ${i >= 3 ? "border-t border-white/[0.06]" : ""}`}
                          >
                            <div className="text-[8px] font-mono uppercase tracking-[0.1em] text-text-muted/50">{m.label}</div>
                            <div className="mt-0.5 font-mono text-[12px] tabular-nums text-brand-100">{m.value}</div>
                          </div>
                        ))}
                      </div>


                      {/* top species */}
                      {stats.top_species && stats.top_species.length > 0 && (
                        <div className="mt-3 flex items-center gap-2">
                          <span className="shrink-0 text-[8px] font-mono uppercase tracking-[0.18em] text-text-muted/60">
                            Top species
                          </span>
                          <div className="flex -space-x-1.5">
                            {stats.top_species.slice(0, 5).map((sp) =>
                              sp.thumbnail_url ? (
                                // eslint-disable-next-line @next/next/no-img-element
                                <img
                                  key={sp.name}
                                  src={sp.thumbnail_url}
                                  alt={sp.name}
                                  title={`${sp.name} (${sp.count})`}
                                  className="h-6 w-6 rounded-full border border-surface-elevated object-cover ring-1 ring-brand-500/20 transition-transform hover:z-10 hover:scale-125"
                                />
                              ) : (
                                <div
                                  key={sp.name}
                                  title={`${sp.name} (${sp.count})`}
                                  className="h-6 w-6 rounded-full border border-surface-elevated bg-brand-500/10 ring-1 ring-brand-500/20"
                                />
                              ),
                            )}
                          </div>
                        </div>
                      )}

                      {/* footer */}
                      <div className="mt-3 flex items-center justify-between gap-3 border-t border-border-subtle pt-2.5">
                        <span className="font-mono text-[9px] text-text-muted/65">
                          {stats.first_detection ?? "—"} → {stats.last_detection ?? "—"}
                        </span>
                        {selectedStation.in_database === false ? (
                          <span
                            className="font-mono text-[8px] italic text-text-muted/50"
                            title="Detection data not yet imported into the database"
                          >
                            Not in DB
                          </span>
                        ) : null}
                      </div>
                    </>
                  );
                })()}
              </div>
            </Popup>
          )}

          {selectedGridCell && spatialPartitionMode === "grid" && (
            <Popup
              longitude={selectedGridCell.longitude}
              latitude={selectedGridCell.latitude}
              closeButton={true}
              closeOnClick={false}
              onClose={() => setSelectedGridCell(null)}
              anchor="bottom"
              maxWidth="340px"
              offset={14}
              className="station-popup"
            >
              <GridCellPopup
                overview={selectedGridOverview}
                loading={selectedGridOverviewLoading}
                fallbackLabel={selectedGridCell.referenceLabel}
              />
            </Popup>
          )}

          {/* Movebank individual bird popup */}
          {showMovebank && selectedMovebankTrack && (() => {
            const [popLon, popLat] = selectedMovebankTrack.end_point ?? [0, 0];
            const commonName =
              MOVEBANK_SPECIES_COMMON_NAMES[selectedMovebankTrack.species] ??
              selectedMovebankTrack.species;
            const fmtDate = (ts: string) => {
              try {
                return new Intl.DateTimeFormat("en-GB", {
                  day: "2-digit",
                  month: "short",
                  year: "numeric",
                }).format(new Date(ts));
              } catch {
                return ts ?? "—";
              }
            };
            return (
              <Popup
                longitude={popLon}
                latitude={popLat}
                closeButton={true}
                closeOnClick={false}
                onClose={() => setSelectedMovebankTrack(null)}
                anchor="bottom"
                maxWidth="300px"
                offset={14}
                className="station-popup"
              >
                <div className="min-w-[260px] rounded-md border border-brand-500/20 bg-[rgba(4,15,10,0.94)] p-4 text-text-primary shadow-[0_8px_32px_rgba(0,0,0,0.8)] backdrop-blur-xl">
                  <div className="mb-2.5 border-b border-brand-500/10 pb-2 pr-5">
                    <div className="flex items-center justify-between gap-3">
                      <h3
                        className="text-sm font-semibold tracking-wide text-brand-300 truncate"
                      >
                        {commonName}
                      </h3>
                      <div
                        className="w-2 h-2 rounded-full shrink-0"
                        style={{
                          background: "rgb(52,211,153)",
                          boxShadow: "0 0 8px rgba(52,211,153,0.7)",
                        }}
                      />
                    </div>
                    <div className="mt-0.5 text-[10px] font-mono italic text-text-muted/65 truncate">
                      {selectedMovebankTrack.species}
                    </div>
                    <div className="mt-0.5 text-[10px] font-mono text-text-muted/55 truncate">
                      {selectedMovebankTrack.individual}
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
                    <div>
                      <span className="text-text-muted/55 text-[8px] font-mono uppercase block tracking-[0.14em]">
                        Study
                      </span>
                      <span className="font-mono text-brand-100 text-[10px]">
                        {selectedMovebankTrack.study_id}
                      </span>
                    </div>
                    <div>
                      <span className="text-text-muted/55 text-[8px] font-mono uppercase block tracking-[0.14em]">
                        Fix count
                      </span>
                      <span className="font-mono text-brand-100">
                        {selectedMovebankTrack.point_count.toLocaleString()}
                      </span>
                    </div>
                    <div>
                      <span className="text-text-muted/55 text-[8px] font-mono uppercase block tracking-[0.14em]">
                        First fix
                      </span>
                      <span className="font-mono text-brand-100 text-[10px]">
                        {fmtDate(selectedMovebankTrack.start_ts)}
                      </span>
                    </div>
                    <div>
                      <span className="text-text-muted/55 text-[8px] font-mono uppercase block tracking-[0.14em]">
                        Latest fix
                      </span>
                      <span className="font-mono text-brand-100 text-[10px]">
                        {fmtDate(selectedMovebankTrack.end_ts)}
                      </span>
                    </div>
                  </div>
                </div>
              </Popup>
            );
          })()}
        </Map>

        <div className="absolute top-4 left-4 z-40 flex max-w-[280px] flex-col gap-3 pointer-events-none">
        </div>
        {renderedSkin === "lcz" && activeSkinMeta.attribution ? (
          <div
            className="pointer-events-none absolute bottom-2 right-2 z-20 max-w-[42%] rounded-sm px-2 py-1 text-[9px] leading-tight text-white/60 backdrop-blur-md"
            style={{
              background: "rgba(5, 8, 6, 0.72)",
              border: "1px solid rgba(255,255,255,0.08)",
            }}
          >
            {activeSkinMeta.attribution}
          </div>
        ) : null}
      </div>
    );
  },
);
