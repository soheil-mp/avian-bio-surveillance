export type StationTypeId = "puc" | "birdnetpi" | "app" | "stream_youtube" | "other";

export interface StationTypeMeta {
  id: StationTypeId;
  label: string;
  shortLabel: string;
  description: string;
  accent: string;
  aliases?: string[];
}

export const STATION_TYPE_OPTIONS: readonly StationTypeMeta[] = [
  {
    id: "puc",
    label: "PUC Network",
    shortLabel: "PUC",
    description: "Permanent biosurveillance nodes with the deepest coverage across the network.",
    accent: "#34d399",
  },
  {
    id: "birdnetpi",
    label: "BirdNET-Pi",
    shortLabel: "BPI",
    description: "Community BirdNET-Pi stations contributing automated acoustic detections.",
    accent: "#fbbf24",
    aliases: ["birdnet-pi"],
  },
  {
    id: "app",
    label: "Mobile App",
    shortLabel: "APP",
    description: "App-linked stations and lighter field deployments with smaller footprints.",
    accent: "#38bdf8",
  },
  {
    id: "stream_youtube",
    label: "Live Streams",
    shortLabel: "LIVE",
    description: "Stations inferred from continuous streams such as YouTube-based live audio feeds.",
    accent: "#f87171",
    aliases: ["youtube", "youtube_stream", "stream", "stream-youtube"],
  },
  {
    id: "other",
    label: "Other Sources",
    shortLabel: "OTHER",
    description: "Fallback bucket for future or uncommon station types outside the main catalog.",
    accent: "#a78bfa",
    aliases: ["unknown", "other"],
  },
] as const;

export const STATION_TYPE_IDS = STATION_TYPE_OPTIONS.map((option) => option.id);

const DIRECT_STATION_TYPES = new Set<StationTypeId>([
  "puc",
  "birdnetpi",
  "app",
  "stream_youtube",
  "other",
]);

const STATION_TYPE_BY_ALIAS = new Map<string, StationTypeId>(
  STATION_TYPE_OPTIONS.flatMap((option) =>
    [option.id, ...(option.aliases ?? [])].map((value) =>
      [value.toLowerCase(), option.id] as const,
    ),
  ),
);

export function normalizeStationType(
  type: string | null | undefined,
): StationTypeId {
  const normalized = type?.trim().toLowerCase();
  if (!normalized) return "other";
  if (DIRECT_STATION_TYPES.has(normalized as StationTypeId)) {
    return normalized as StationTypeId;
  }
  return STATION_TYPE_BY_ALIAS.get(normalized) ?? "other";
}

export function getStationTypeMeta(
  type: string | null | undefined,
): StationTypeMeta {
  const normalized = normalizeStationType(type);
  return (
    STATION_TYPE_OPTIONS.find((option) => option.id === normalized) ??
    STATION_TYPE_OPTIONS[STATION_TYPE_OPTIONS.length - 1]
  );
}

export function areAllStationTypesSelected(
  activeTypes: string[] | undefined,
): boolean {
  if (!activeTypes || activeTypes.length === 0) return true;
  return STATION_TYPE_IDS.every((id) => activeTypes.includes(id));
}

export function matchesStationTypeFilter(
  type: string | null | undefined,
  activeTypes: string[] | undefined,
): boolean {
  if (
    !activeTypes ||
    activeTypes.length === 0 ||
    areAllStationTypesSelected(activeTypes)
  ) {
    return true;
  }
  return activeTypes.includes(normalizeStationType(type));
}

export const STATION_QUALITY_LEVELS = [
  {
    value: 0,
    label: "Any lifetime availability",
    shortLabel: "All life",
    description:
      "Show the full station network regardless of lifetime availability.",
  },
  {
    value: 25,
    label: "Monitor · 25%+",
    shortLabel: "25%+",
    description:
      "Keep stations with at least light lifetime coverage across their observed span.",
  },
  {
    value: 50,
    label: "Reliable · 50%+",
    shortLabel: "50%+",
    description:
      "Focus on stations that are active on at least half of the days in their observed span.",
  },
  {
    value: 70,
    label: "Strong · 70%+",
    shortLabel: "70%+",
    description:
      "Prioritise stations with strong long-run continuity and fewer gaps.",
  },
  {
    value: 85,
    label: "Near-continuous · 85%+",
    shortLabel: "85%+",
    description:
      "Keep only stations that are active for most days across their observed lifespan.",
  },
] as const;

export function formatStationQualitySummary(minQuality: number): string {
  if (!minQuality || minQuality <= 0) return "All lifetime availability";
  return `${Math.round(minQuality)}%+ lifetime availability`;
}