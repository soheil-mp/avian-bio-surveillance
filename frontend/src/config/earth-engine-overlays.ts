export type EarthEngineOverlayId =
  | "vegetation"
  | "wetness"
  | "human_pressure"
  | "habitat"
  | "thermal"
  | "air_quality"
  | "forest"
  | "cloud"
  | "fire"
  | "sst"
  | "terrain";

export type EarthEngineOverlayKind = "continuous" | "categorical" | "hotspot";

export type EarthEngineVisualizationMode = "grid" | "municipality";

export interface EarthEngineOverlayMeta {
  id: EarthEngineOverlayId;
  label: string;
  shortLabel: string;
  description: string;
  kind: EarthEngineOverlayKind;
  unit?: string;
  lowerLabel: string;
  upperLabel: string;
  accent: string;
}

export interface EarthEngineVisualizationMeta {
  id: EarthEngineVisualizationMode;
  label: string;
  shortLabel: string;
  description: string;
}

export const EARTH_ENGINE_OVERLAY_ORDER: EarthEngineOverlayId[] = [
  "vegetation",
  "wetness",
  "human_pressure",
  "habitat",
  "thermal",
  "air_quality",
  "forest",
  "cloud",
  "fire",
  "sst",
  "terrain",
];

export const EARTH_ENGINE_OVERLAY_META: Record<EarthEngineOverlayId, EarthEngineOverlayMeta> = {
  vegetation: {
    id: "vegetation",
    label: "Vegetation",
    shortLabel: "NDVI",
    description: "Greenness and canopy productivity across the landscape.",
    kind: "continuous",
    unit: "NDVI",
    lowerLabel: "sparse",
    upperLabel: "lush",
    accent: "rgb(123, 160, 131)",
  },
  wetness: {
    id: "wetness",
    label: "Wetness",
    shortLabel: "Wetness",
    description: "Surface water persistence and soil moisture.",
    kind: "continuous",
    unit: "score",
    lowerLabel: "dry",
    upperLabel: "wet",
    accent: "rgb(117, 148, 164)",
  },
  human_pressure: {
    id: "human_pressure",
    label: "Human Footprint",
    shortLabel: "Pressure",
    description: "Human modification, density, and night lights.",
    kind: "continuous",
    unit: "score",
    lowerLabel: "quiet",
    upperLabel: "urban",
    accent: "rgb(156, 147, 131)",
  },
  habitat: {
    id: "habitat",
    label: "Habitat",
    shortLabel: "Habitat",
    description: "Dominant land-cover class by grid cell.",
    kind: "categorical",
    lowerLabel: "natural",
    upperLabel: "built",
    accent: "rgb(141, 152, 128)",
  },
  thermal: {
    id: "thermal",
    label: "Surface Heat",
    shortLabel: "Thermal",
    description: "Land-surface or near-surface temperature.",
    kind: "continuous",
    unit: "°C",
    lowerLabel: "cooler",
    upperLabel: "warmer",
    accent: "rgb(171, 135, 109)",
  },
  air_quality: {
    id: "air_quality",
    label: "Air Burden",
    shortLabel: "Air",
    description: "NO₂ and aerosol load in the lower atmosphere.",
    kind: "continuous",
    unit: "score",
    lowerLabel: "clearer",
    upperLabel: "more burdened",
    accent: "rgb(136, 132, 159)",
  },
  forest: {
    id: "forest",
    label: "Forest Cover",
    shortLabel: "Forest",
    description: "Forest cover with recent loss signals where present.",
    kind: "continuous",
    unit: "%",
    lowerLabel: "open",
    upperLabel: "dense",
    accent: "rgb(101, 139, 111)",
  },
  cloud: {
    id: "cloud",
    label: "Cloud Cover",
    shortLabel: "Cloud",
    description: "Mean cloud fraction over recent observations.",
    kind: "continuous",
    unit: "%",
    lowerLabel: "clear",
    upperLabel: "overcast",
    accent: "rgb(171, 177, 185)",
  },
  fire: {
    id: "fire",
    label: "Fire Signals",
    shortLabel: "Fire",
    description: "Thermal anomalies and recent burned-area signals.",
    kind: "hotspot",
    unit: "score",
    lowerLabel: "quiet",
    upperLabel: "active hotspot",
    accent: "rgb(171, 114, 99)",
  },
  sst: {
    id: "sst",
    label: "Sea Surface Temp",
    shortLabel: "SST",
    description: "Marine thermal context for coastal grid cells.",
    kind: "continuous",
    unit: "°C",
    lowerLabel: "cool water",
    upperLabel: "warm water",
    accent: "rgb(116, 147, 170)",
  },
  terrain: {
    id: "terrain",
    label: "Terrain",
    shortLabel: "Relief",
    description: "Elevation and relief across the Dutch landscape.",
    kind: "continuous",
    unit: "m",
    lowerLabel: "lowland",
    upperLabel: "higher ground",
    accent: "rgb(150, 139, 117)",
  },
};

export const EARTH_ENGINE_OVERLAYS = EARTH_ENGINE_OVERLAY_ORDER.map(
  (id) => EARTH_ENGINE_OVERLAY_META[id],
);

export const EARTH_ENGINE_VISUALIZATION_META: Record<
  EarthEngineVisualizationMode,
  EarthEngineVisualizationMeta
> = {
  grid: {
    id: "grid",
    label: "Grid (5x5 km)",
    shortLabel: "Grid",
    description: "Uniform 5 km cells for direct spatial comparison.",
  },
  municipality: {
    id: "municipality",
    label: "Administrative Areas",
    shortLabel: "Admin",
    description: "Regional aggregation using municipal boundaries.",
  },
};

export const EARTH_ENGINE_VISUALIZATIONS = ([
  "grid",
  "municipality",
] as const).map((id) => EARTH_ENGINE_VISUALIZATION_META[id]);
