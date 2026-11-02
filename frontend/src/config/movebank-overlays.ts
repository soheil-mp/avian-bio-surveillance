export type MovebankOverlayId =
  | "corridors"
  | "stopovers"
  | "recent_fixes"
  | "flow_field"
  | "journey_stories"
  | "deployment_origins";

export type MovebankOverlayKind =
  | "density"
  | "hotspot"
  | "point"
  | "vector"
  | "story"
  | "network";

export interface MovebankOverlayMeta {
  id: MovebankOverlayId;
  label: string;
  shortLabel: string;
  description: string;
  kind: MovebankOverlayKind;
  defaultWindowDays: number;
  lowerLabel: string;
  upperLabel: string;
  accent: string;
}

export interface MovebankOverlaySummary {
  featureCount: number;
  individualCount: number;
  speciesCount: number;
  latestTimestamp: string | null;
  windowDays: number;
  metricLabel?: string;
  metricRange?: string;
}

export interface MovebankOverlayResponse {
  overlay: MovebankOverlayId;
  generatedAt: string;
  summary: MovebankOverlaySummary;
  polygons?: GeoJSON.FeatureCollection | null;
  lines?: GeoJSON.FeatureCollection | null;
  points?: GeoJSON.FeatureCollection | null;
  labels?: GeoJSON.FeatureCollection | null;
  vectors?: GeoJSON.FeatureCollection | null;
}

export const MOVEBANK_OVERLAY_ORDER: MovebankOverlayId[] = [
  "corridors",
  "stopovers",
  "recent_fixes",
  "flow_field",
  "journey_stories",
  "deployment_origins",
];

export const MOVEBANK_OVERLAY_META: Record<MovebankOverlayId, MovebankOverlayMeta> = {
  corridors: {
    id: "corridors",
    label: "Movement Corridors",
    shortLabel: "Corridors",
    description: "Aggregates tracking fixes into movement lanes that show where tagged birds most consistently pass through the Dutch surveillance scope.",
    kind: "density",
    defaultWindowDays: 3650,
    lowerLabel: "sporadic passage",
    upperLabel: "dense corridor",
    accent: "rgb(34, 211, 238)",
  },
  stopovers: {
    id: "stopovers",
    label: "Stopover Hotspots",
    shortLabel: "Stopovers",
    description: "Highlights places where tagged birds pause long enough to suggest staging, roosting, or repeated local use.",
    kind: "hotspot",
    defaultWindowDays: 3650,
    lowerLabel: "brief pause",
    upperLabel: "long dwell",
    accent: "rgb(250, 204, 21)",
  },
  recent_fixes: {
    id: "recent_fixes",
    label: "Recent Fixes",
    shortLabel: "Recent",
    description: "Shows the latest in-scope position for each tracked individual, with older fixes fading back.",
    kind: "point",
    defaultWindowDays: 45,
    lowerLabel: "older fix",
    upperLabel: "fresh fix",
    accent: "rgb(196, 181, 253)",
  },
  flow_field: {
    id: "flow_field",
    label: "Direction Field",
    shortLabel: "Flow",
    description: "Aggregates local movement segments into directional glyphs that summarise heading and speed across the Dutch surveillance view.",
    kind: "vector",
    defaultWindowDays: 3650,
    lowerLabel: "weak consensus",
    upperLabel: "coherent flow",
    accent: "rgb(248, 113, 113)",
  },
  journey_stories: {
    id: "journey_stories",
    label: "Journey Excerpts",
    shortLabel: "Journeys",
    description: "Shows a small set of representative in-scope track excerpts so individual journeys stay legible on the map.",
    kind: "story",
    defaultWindowDays: 3650,
    lowerLabel: "short excerpt",
    upperLabel: "long excerpt",
    accent: "rgb(52, 211, 153)",
  },
  deployment_origins: {
    id: "deployment_origins",
    label: "Origins to Scope",
    shortLabel: "Origins",
    description: "Connects deployment sites to the most recent in-scope fixes, revealing how far tracked birds project into the Dutch surveillance picture.",
    kind: "network",
    defaultWindowDays: 1095,
    lowerLabel: "near deployment",
    upperLabel: "far from deployment",
    accent: "rgb(251, 146, 60)",
  },
};

export const MOVEBANK_OVERLAYS = MOVEBANK_OVERLAY_ORDER.map(
  (id) => MOVEBANK_OVERLAY_META[id],
);

export function isMovebankOverlayId(value: string | null | undefined): value is MovebankOverlayId {
  return Boolean(value && value in MOVEBANK_OVERLAY_META);
}
