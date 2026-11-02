"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { Space_Grotesk } from "next/font/google";
import { usePathname, useRouter } from "next/navigation";
import {
  memo,
  useCallback,
  useDeferredValue,
  startTransition as startUiTransition,
  useEffect,
  useMemo,
  useRef,
  useState,
  useTransition,
} from "react";
import { flushSync } from "react-dom";
import {
  ArrowUpRight,
  ChevronDown,
  Filter,
  MapPinned,
  RotateCcw,
  Search,
  SlidersHorizontal,
  Volume2,
  X,
} from "lucide-react";
import AssistedAudioPlayer from "@/components/audio/AssistedAudioPlayer";
import { STATION_QUALITY_LEVELS } from "../../config/stations";
import AlertsLoadingState from "./AlertsLoadingState";
import AlertsSpatialEvaluation from "./AlertsSpatialEvaluation";
import { AlertReviewModal } from "./AlertReviewModal";
import {
  SPATIAL_ALERT_LEVEL_NAMES,
  SPATIAL_VARIANT_NAMES,
} from "./spatial-evaluation";
import type {
  AlertClusterContext,
  AlertReviewContext,
  AlertVisualPoint,
  AlertsModelingSummary,
  AlertsPageData,
  AlertsPageFilters,
  ModelPerformanceReport,
} from "./types";
import {
  buildHotspotCells,
  focusHotspotCells,
  type HotspotLens,
  type HotspotCellSummary,
} from "./hotspot";
import type {
  HotspotGrain,
  HotspotSummaryResponse,
  HotspotSummaryUnit,
} from "./hotspot-summary";
import {
  buildCanvasHitIndex,
  findCanvasHitPoint,
  mergeAlertDetailPoint,
  unpackAlertsEmbeddingMap,
  type CanvasHitPoint,
  type PackedAlertsEmbeddingMapPayload,
} from "./embedding-map";

const alertTitleFont = Space_Grotesk({
  subsets: ["latin"],
  weight: ["500", "700"],
});

const AlertsHotspotMap = dynamic(() => import("./AlertsHotspotMap"), {
  ssr: false,
});

const AlertsHotspotPreviewMap = dynamic(() => import("./AlertsHotspotPreviewMap"), {
  ssr: false,
});

const ANOMALY_RAIL_INITIAL_COUNT = 100;
const ANOMALY_RAIL_BATCH_SIZE = 100;
const EMPTY_STRING_LIST: string[] = [];
const EMPTY_NUMBER_LIST: number[] = [];

const DEFAULT_FILTERS: AlertsPageFilters = {
  species: "",
  station: "",
  background: "",
  backend: "perch_v2",
  dateFrom: "",
  dateTo: "",
  minConfidence: "",
  minStationQuality: "",
  alertLevel: "all",
  viewMode: "anomaly",
  clusterScope: "all",
  clusterKey: "",
  sort: "alert-desc",
};

const BACKEND_OPTIONS = [
  { value: "birdnet", label: "BirdNET" },
  { value: "perch_v2", label: "Perch v2" },
] as const;

const ALERT_LEVEL_OPTIONS = [
  { value: "all", label: "All scored clips" },
  { value: "moderate", label: "Moderate and high" },
  { value: "high", label: "High only" },
] as const;

const CONFIDENCE_OPTIONS = [
  { value: "", label: "Any confidence" },
  { value: "0.25", label: "25%+ confidence" },
  { value: "0.5", label: "50%+ confidence" },
  { value: "0.75", label: "75%+ confidence" },
  { value: "0.9", label: "90%+ confidence" },
] as const;

const STATION_QUALITY_OPTIONS = STATION_QUALITY_LEVELS.map((level) => ({
  value: level.value > 0 ? String(level.value) : "",
  label: level.label,
  detail: level.description,
}));

const CLUSTER_PALETTE = [
  "rgba(56,189,248,1)",
  "rgba(34,197,94,1)",
  "rgba(150,118,62,1)",
  "rgba(138,52,58,1)",
  "rgba(20,184,166,1)",
  "rgba(249,115,22,1)",
  "rgba(132,204,22,1)",
  "rgba(14,165,233,1)",
] as const;

const MONITOR_TABS = [
  { value: "embeddings", label: "Embeddings" },
  { value: "hotspot", label: "Hotspot" },
  { value: "evaluation", label: "Evaluation" },
] as const;

const NEUTRAL_SURFACE = {
  canvas: "#0c0c0c",
  panel: "#141414",
  panelAlt: "#1a1a1a",
  panelDeep: "#101010",
  border: "#2a2a2a",
  borderSoft: "#232323",
} as const;

const BASELINE_HIGHLIGHT_COLOR = "rgba(165,243,252,1)";

const DISPLAY_LOCALE = "en-GB";
const DISPLAY_TIME_ZONE = "Europe/Amsterdam";
const TIME_OF_DAY_FORMATTER = new Intl.DateTimeFormat(DISPLAY_LOCALE, {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: DISPLAY_TIME_ZONE,
});
const DEFAULT_EVALUATION_ANOMALY_TYPE = "bioacoustic_outlier";
const DEFAULT_EVALUATION_ALERT_LEVEL = "optimized";
const EVALUATION_ANOMALY_TYPE_ORDER = [
  "bioacoustic_outlier",
  "bioacoustic_excess",
  "embedding",
] as const;
const EVALUATION_ALERT_LEVEL_ORDER = ["optimized", "amber", "red"] as const;

type ClusterGroup = {
  key: string;
  label: string;
  shortLabel: string;
  count: number;
  exemplarCount: number;
  averageIsolation: number;
  embeddingModel: string;
  weeklyPeriodStart: string | null;
  weeklyPeriodEnd: string | null;
};

type MonitorTab = (typeof MONITOR_TABS)[number]["value"];
type AlertBand = "high" | "moderate" | "normal";

const HOTSPOT_LENS_META = {
  bioacoustic: {
    label: "Bioacoustic",
    shortLabel: "Bio",
    icon: Volume2,
  },
} as const;

type DropdownOption = {
  value: string;
  label: string;
  detail?: string;
  imageUrl?: string | null;
};

type AlertComparisonTarget = {
  key: string;
  title: string;
  description: string;
  point: AlertVisualPoint | null;
  emptyText: string;
};

type HotspotGridCell = {
  gridCellId: string;
  bboxMinLat: number;
  bboxMinLon: number;
  bboxMaxLat: number;
  bboxMaxLon: number;
  stationCount: number;
  totalDetections: number;
  cellGeojson: unknown | null;
};

type IndexedAlertPoint = {
  point: AlertVisualPoint;
  detectedAtMs: number;
  minuteOfDay: number;
  weekStart: string | null;
  hasAudio: boolean;
  speciesBackendKey: string;
  persistedClusterKey: string | null;
};

type ReferencePointIndex = {
  byId: Map<string, IndexedAlertPoint>;
  byBackend: Map<string, IndexedAlertPoint[]>;
  bySpeciesBackend: Map<string, IndexedAlertPoint[]>;
  byPersistedCluster: Map<string, IndexedAlertPoint[]>;
};

function speciesBackendKeyForPoint(point: AlertVisualPoint): string {
  return `${point.embeddingBackend}::${point.species}`;
}

function persistedClusterKeyForPoint(point: AlertVisualPoint): string | null {
  const context = point.clusterContext;
  if (
    context?.clusterQuality !== "clustered" ||
    !context.clusterId ||
    !context.embeddingModel ||
    !context.weeklyPeriodStart ||
    !context.locationType ||
    !context.locationId
  ) {
    return null;
  }

  return [
    context.clusterId,
    context.embeddingModel,
    context.weeklyPeriodStart,
    context.locationType,
    context.locationId,
  ].join("::");
}

function buildReferencePointIndex(referencePoints: AlertVisualPoint[]): ReferencePointIndex {
  const byId = new Map<string, IndexedAlertPoint>();
  const byBackend = new Map<string, IndexedAlertPoint[]>();
  const bySpeciesBackend = new Map<string, IndexedAlertPoint[]>();
  const byPersistedCluster = new Map<string, IndexedAlertPoint[]>();

  for (const point of referencePoints) {
    const indexed: IndexedAlertPoint = {
      point,
      detectedAtMs: Number.isFinite(Date.parse(point.detectedAt)) ? Date.parse(point.detectedAt) : 0,
      minuteOfDay: minutesOfDay(point.detectedAt),
      weekStart: pointWeeklyPeriodStart(point),
      hasAudio: Boolean(preferredAudioUrl(point)),
      speciesBackendKey: speciesBackendKeyForPoint(point),
      persistedClusterKey: persistedClusterKeyForPoint(point),
    };

    byId.set(point.id, indexed);

    const backendBucket = byBackend.get(point.embeddingBackend) ?? [];
    backendBucket.push(indexed);
    byBackend.set(point.embeddingBackend, backendBucket);

    const speciesBackendBucket = bySpeciesBackend.get(indexed.speciesBackendKey) ?? [];
    speciesBackendBucket.push(indexed);
    bySpeciesBackend.set(indexed.speciesBackendKey, speciesBackendBucket);

    if (indexed.persistedClusterKey) {
      const clusterBucket = byPersistedCluster.get(indexed.persistedClusterKey) ?? [];
      clusterBucket.push(indexed);
      byPersistedCluster.set(indexed.persistedClusterKey, clusterBucket);
    }
  }

  return {
    byId,
    byBackend,
    bySpeciesBackend,
    byPersistedCluster,
  };
}

function formatShortDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;

  return new Intl.DateTimeFormat(DISPLAY_LOCALE, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: DISPLAY_TIME_ZONE,
  }).format(date);
}

function formatGeoCoordinate(
  value: number,
  axis: "lat" | "lon",
  digits = 4,
) {
  const direction = axis === "lat"
    ? value >= 0 ? "N" : "S"
    : value >= 0 ? "E" : "W";

  return `${Math.abs(value).toFixed(digits)}°${direction}`;
}

function hotspotGridCoordinateTitle(gridCell: HotspotGridCell | null | undefined) {
  if (!gridCell) return null;

  const centerLat = (gridCell.bboxMinLat + gridCell.bboxMaxLat) / 2;
  const centerLon = (gridCell.bboxMinLon + gridCell.bboxMaxLon) / 2;

  return `${formatGeoCoordinate(centerLat, "lat")}, ${formatGeoCoordinate(centerLon, "lon")}`;
}

function formatCalendarDate(value: string) {
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return value;

  return new Intl.DateTimeFormat(DISPLAY_LOCALE, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    timeZone: DISPLAY_TIME_ZONE,
  }).format(date);
}

function formatModeledWeekRangeLabel(week: { periodStart: string; periodEnd: string }) {
  const startDate = new Date(`${week.periodStart}T00:00:00Z`);
  const endDate = new Date(`${week.periodEnd}T00:00:00Z`);
  if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) {
    return `${week.periodStart} – ${week.periodEnd}`;
  }

  const dayMonth = new Intl.DateTimeFormat(DISPLAY_LOCALE, {
    day: "2-digit",
    month: "short",
    timeZone: DISPLAY_TIME_ZONE,
  });
  const dayMonthYear = new Intl.DateTimeFormat(DISPLAY_LOCALE, {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: DISPLAY_TIME_ZONE,
  });

  if (startDate.getUTCFullYear() === endDate.getUTCFullYear()) {
    return `${dayMonth.format(startDate)} – ${dayMonthYear.format(endDate)}`;
  }
  return `${dayMonthYear.format(startDate)} – ${dayMonthYear.format(endDate)}`;
}

function buildModeledWeekDropdownOptions(
  weeks: Array<{ periodStart: string; periodEnd: string }>,
): DropdownOption[] {
  return weeks.map((week) => ({
    value: week.periodStart,
    label: formatModeledWeekRangeLabel(week),
  }));
}

function scoreColor(score: number): string {
  if (score >= 0.5) return "rgba(138,52,58,1)";
  if (score >= 0.3) return "rgba(150,118,62,1)";
  return "rgba(94,124,74,1)";
}

function scoreLabel(score: number): string {
  if (score >= 0.5) return "High";
  if (score >= 0.3) return "Moderate";
  return "Normal";
}

function alertBand(score: number): AlertBand {
  if (score >= 0.5) return "high";
  if (score >= 0.3) return "moderate";
  return "normal";
}

function pointImageTreatment(isMuted: boolean) {
  if (!isMuted) {
    return {
      filter: "none",
      overlay: "rgba(255,255,255,0)",
      opacity: 0.96,
    };
  }

  return {
    filter: "grayscale(0.96) saturate(0.52) brightness(0.74)",
    overlay: "rgba(120,132,148,0.28)",
    opacity: 0.72,
  };
}

function withAlpha(color: string, alpha: number): string {
  return color.replace("1)", `${alpha})`);
}

function hotspotDisplayLabel(lens: HotspotLens | null): string {
  if (lens === "bioacoustic") return "Bioacoustic hotspot";
  return "All signals";
}

function hotspotSignalItems(cell: HotspotCellSummary): string[] {
  const summary: string[] = [];

  if (cell.overlapSignals.includes("bioacoustic")) {
    summary.push(`Bio ${Math.round(cell.hotspotScore * 100)}%`);
  }

  return summary;
}

function hotspotSignalColor(score: number): string {
  if (score >= 0.5) return "rgba(138,52,58,1)";
  if (score >= 0.34) return "rgba(250,204,21,1)";
  return "rgba(148,148,148,1)";
}

function hotspotDisplayValue(cell: HotspotCellSummary, lens: HotspotLens | null): string {
  if (lens === "bioacoustic") return `${Math.round(cell.hotspotScore * 100)}%`;

  return hotspotSignalItems(cell).slice(0, 2).join(" · ") || "Quiet";
}

function hotspotDisplayCaption(cell: HotspotCellSummary, lens: HotspotLens | null): string {
  if (lens === "bioacoustic") {
    return `${Math.round(cell.maxIsolation * 100)}% peak · ${cell.rowCount} clips`;
  }

  return hotspotSignalItems(cell).join(" · ") || `${cell.rowCount} clips`;
}

function hotspotRankIndicator(cell: HotspotCellSummary, lens: HotspotLens | null) {
  const value = hotspotDisplayValue(cell, lens);
  const detail = hotspotDisplayCaption(cell, lens);

  return {
    text: lens ? (value ? `${HOTSPOT_LENS_META[lens].label} · ${value}` : HOTSPOT_LENS_META[lens].label) : value,
    detail,
  };
}

function hotspotToneColor(score: number): string {
  return hotspotSignalColor(score);
}

function hotspotSummaryText(cell: HotspotCellSummary): string {
  const summary = hotspotSignalItems(cell);
  return summary.length > 0 ? summary.join(" · ") : "Quiet";
}

function hotspotSignalsFromSummaryUnit(unit: HotspotSummaryUnit): HotspotLens[] {
  const signals: HotspotLens[] = [];
  if (unit.bioacoustic_alert_rows > 0 || unit.score >= 0.34) {
    signals.push("bioacoustic");
  }
  return signals;
}

function hotspotCellFromSummaryUnit(unit: HotspotSummaryUnit): HotspotCellSummary {
  const gridCellId = unit.grid_cell_id || unit.location_id;
  const latestDetection = unit.period_start
    ? `${unit.period_start}T00:00:00.000Z`
    : "";
  return {
    gridCellId,
    rowCount: unit.bioacoustic_alert_rows,
    speciesCount: 0,
    stationCount: 0,
    contextHits: 0,
    highCount: unit.score >= 0.5 ? unit.bioacoustic_alert_rows : 0,
    moderateCount: unit.score >= 0.34 ? unit.bioacoustic_alert_rows : 0,
    meanIsolation: unit.score,
    maxIsolation: unit.score,
    hotspotScore: unit.score,
    displayScore: unit.score,
    overlapSignals: hotspotSignalsFromSummaryUnit(unit),
    speciesPreview: [],
    stationPreview: [],
    latestDetection,
  };
}

function hotspotUnitPrimaryValue(unit: HotspotSummaryUnit): string {
  return `${Math.round(unit.score * 100)}%`;
}

function hotspotRowSummary(
  cell: HotspotCellSummary,
  lens: HotspotLens | null,
): string {
  if (lens === "bioacoustic") {
    return `Mean ${Math.round(cell.meanIsolation * 100)}% · Peak ${Math.round(cell.maxIsolation * 100)}%`;
  }

  return hotspotSummaryText(cell);
}

function formatBackendLabel(value: string): string {
  if (!value) return "Unknown";
  if (value === "perch_v2") return "Perch v2";
  if (value === "birdnet") return "BirdNET";

  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function sortPoints(points: AlertVisualPoint[], sort: AlertsPageFilters["sort"]) {
  const next = [...points];

  next.sort((left, right) => {
    const reviewScoreDelta =
      (right.reviewContext?.finalScore ?? -1) -
      (left.reviewContext?.finalScore ?? -1);

    switch (sort) {
      case "date-desc":
        return new Date(right.detectedAt).getTime() - new Date(left.detectedAt).getTime();
      case "date-asc":
        return new Date(left.detectedAt).getTime() - new Date(right.detectedAt).getTime();
      case "confidence-desc":
        return right.confidence - left.confidence;
      case "confidence-asc":
        return left.confidence - right.confidence;
      case "species-asc":
        return left.species.localeCompare(right.species) || new Date(right.detectedAt).getTime() - new Date(left.detectedAt).getTime();
      case "alert-desc":
      default:
        return right.alertScore - left.alertScore || reviewScoreDelta || new Date(right.detectedAt).getTime() - new Date(left.detectedAt).getTime();
    }
  });

  return next;
}

function buildSearchParams(filters: AlertsPageFilters): string {
  const params = new URLSearchParams();

  if (filters.species) params.set("species", filters.species);
  if (filters.backend !== DEFAULT_FILTERS.backend) params.set("backend", filters.backend);
  if (filters.dateFrom) params.set("dateFrom", filters.dateFrom);
  if (filters.dateTo) params.set("dateTo", filters.dateTo);
  if (filters.minConfidence) params.set("minConfidence", filters.minConfidence);
  if (filters.minStationQuality) params.set("minStationQuality", filters.minStationQuality);
  if (filters.alertLevel !== DEFAULT_FILTERS.alertLevel) params.set("alertLevel", filters.alertLevel);

  return params.toString();
}

function surfaceReason(point: AlertVisualPoint) {
  const cluster = point.clusterContext;

  if (point.reviewContext) {
    const clusterNote =
      cluster?.clusterQuality === "clustered"
        ? ` It also lands in ${cluster.clusterLabel?.toLowerCase() ?? "a persisted cluster"} for that species-week${cluster.isExemplar ? " and is the nearest-centroid exemplar for that cluster." : cluster.centroidDistance !== null ? `, sitting ${Math.round(cluster.centroidDistance * 100)}% from the cluster centroid.` : "."}`
        : cluster
          ? ` The latest cluster artifact marked it as ${humanizeLabel(cluster.clusterQuality)} instead of placing it into a stable cluster.`
          : "";

    return `Latest weekly model evidence scored this detection at ${Math.round(point.reviewContext.finalScore * 100)}% for ${point.reviewContext.reviewReason}.${clusterNote} The clip-level embedding isolation is still shown so you can compare the persisted weekly signal against the local neighbourhood map.`;
  }

  if (cluster?.clusterQuality === "clustered") {
    return `This clip falls inside ${cluster.clusterLabel?.toLowerCase() ?? "a persisted cluster"} with ${cluster.clusterSize} member${cluster.clusterSize === 1 ? "" : "s"} in the latest weekly modeling window${cluster.isExemplar ? " and is the nearest-centroid exemplar for that group." : cluster.centroidDistance !== null ? `, sitting ${Math.round(cluster.centroidDistance * 100)}% from the cluster centroid.` : "."}`;
  }

  if (cluster?.clusterQuality === "noise") {
    return "This clip sits outside the stable persisted clusters for its weekly species-location window, which can indicate either a rare acoustic mode or an isolated acoustic outlier.";
  }

  if (cluster?.clusterQuality === "small_sample") {
    return "The weekly modeling run found too few similar clips to form a stable persisted cluster for this species-location window, so the local embedding neighbourhood and media evidence carry more weight.";
  }

  const score = Math.round(point.alertScore * 100);
  const confidence = Math.round(point.confidence * 100);

  if (point.alertScore >= 0.5) {
    return `This clip sits ${score}% away from its nearest neighbours in the current embedding space, which makes it a high-priority acoustic outlier.`;
  }

  if (point.alertScore >= 0.3) {
    return `This clip is moderately separated from nearby detections at ${score}% isolation${confidence > 0 ? ` with ${confidence}% model confidence` : ""}.`;
  }

  return `This clip remains within the normal acoustic neighbourhood, but it is still available for direct comparison.`;
}

function humanizeLabel(value: string) {
  return value.replaceAll("_", " ");
}

function reviewToneColor(review: AlertReviewContext | null) {
  if (!review) return "rgba(255,255,255,0.44)";
  if (review.alertLevel === "red") return "rgba(138,52,58,1)";
  if (review.alertLevel === "amber") return "rgba(150,118,62,1)";
  return "rgba(94,124,74,1)";
}

function clusterToneColor(cluster: AlertClusterContext | null) {
  if (!cluster) return "rgba(255,255,255,0.44)";
  if (cluster.clusterQuality === "clustered") return "rgba(96,165,250,1)";
  if (cluster.clusterQuality === "small_sample") return "rgba(150,118,62,1)";
  return "rgba(148,163,184,1)";
}

function clusterStatusLabel(cluster: AlertClusterContext | null) {
  if (!cluster) return "No persisted cluster";
  if (cluster.clusterQuality === "clustered") {
    return `${cluster.clusterLabel ?? "Cluster"} · ${cluster.clusterSize} clips`;
  }
  if (cluster.clusterQuality === "small_sample") return "Small sample";
  return "Noise";
}

function formatScorePercent(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "N/A";
  return `${Math.round(value * 100)}%`;
}

function formatMetricNumber(value: number | null, digits = 1) {
  if (value === null || !Number.isFinite(value)) return "N/A";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function preferredAudioUrl(point: AlertVisualPoint): string | null {
  return point.audioUrls.processed ?? point.audioUrls.source ?? null;
}

function pointHasLoadedDetail(point: AlertVisualPoint): boolean {
  return Boolean(
    point.audioUrls.processed ||
      point.audioUrls.source ||
      point.visualUrls.spectrogram ||
      point.visualUrls.waveform ||
      point.visualUrls.waveformRaw,
  );
}

function minutesOfDay(value: string): number {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 0;

  const parts = TIME_OF_DAY_FORMATTER.formatToParts(date);
  const hour = Number(parts.find((part) => part.type === "hour")?.value ?? "0");
  const minute = Number(parts.find((part) => part.type === "minute")?.value ?? "0");

  return hour * 60 + minute;
}

function circularMinutesDistance(left: number, right: number): number {
  const delta = Math.abs(left - right);
  return Math.min(delta, 1_440 - delta);
}

function clampIsoDate(value: string, minIso: string, maxIso: string) {
  if (!value) return "";
  if (minIso && value < minIso) return minIso;
  if (maxIso && value > maxIso) return maxIso;
  return value;
}

function hasCorroboratingContext(review: AlertReviewContext | null) {
  if (!review) return false;
  return review.sovonLagMaxScore > 0 || review.sovonLagDeadBirds > 0;
}

function corroborationToneColor(review: AlertReviewContext | null) {
  if (!review || !hasCorroboratingContext(review)) return "rgba(148,163,184,1)";
  if (review.sovonLagMaxScore > 0) return "rgba(150,118,62,1)";
  return "rgba(94,124,74,1)";
}

function pointClusterGroupKey(point: AlertVisualPoint): string | null {
  const cluster = point.clusterContext;
  if (
    !cluster ||
    cluster.clusterQuality !== "clustered" ||
    !cluster.clusterId ||
    !cluster.weeklyPeriodStart ||
    !cluster.locationType ||
    !cluster.locationId
  ) {
    return null;
  }

  return [
    point.species,
    cluster.embeddingModel,
    cluster.weeklyPeriodStart,
    cluster.weeklyPeriodEnd ?? "",
    cluster.locationType,
    cluster.locationId,
    cluster.clusterId,
  ].join("::");
}

function buildClusterGroups(points: AlertVisualPoint[]): ClusterGroup[] {
  const grouped = new Map<
    string,
    ClusterGroup & { totalIsolation: number }
  >();

  for (const point of points) {
    const key = pointClusterGroupKey(point);
    const cluster = point.clusterContext;
    if (!key || !cluster) continue;

    const current = grouped.get(key);
    if (current) {
      current.count += 1;
      current.exemplarCount += cluster.isExemplar ? 1 : 0;
      current.totalIsolation += point.alertScore;
      continue;
    }

    grouped.set(key, {
      key,
      label: `${cluster.clusterLabel ?? cluster.clusterId ?? "Cluster"} · ${point.species}`,
      shortLabel: cluster.clusterLabel ?? cluster.clusterId ?? "Cluster",
      count: 1,
      exemplarCount: cluster.isExemplar ? 1 : 0,
      totalIsolation: point.alertScore,
      averageIsolation: point.alertScore,
      embeddingModel: cluster.embeddingModel,
      weeklyPeriodStart: cluster.weeklyPeriodStart,
      weeklyPeriodEnd: cluster.weeklyPeriodEnd,
    });
  }

  return [...grouped.values()]
    .map((group) => ({
      ...group,
      averageIsolation: group.count > 0 ? group.totalIsolation / group.count : 0,
    }))
    .sort((left, right) => {
      return right.count - left.count || left.label.localeCompare(right.label);
    });
}

function clusterPaletteColor(index: number): string {
  return CLUSTER_PALETTE[index % CLUSTER_PALETTE.length];
}

function clusterScopeMatches(
  point: AlertVisualPoint,
  scope: AlertsPageFilters["clusterScope"],
): boolean {
  if (scope === "all") return true;
  if (scope === "clustered") return point.clusterContext?.clusterQuality === "clustered";
  if (scope === "unclustered") return point.clusterContext?.clusterQuality !== "clustered";
  if (scope === "exemplar") return Boolean(point.clusterContext?.isExemplar);
  return true;
}

function pointDisplayColor(
  point: AlertVisualPoint,
  viewMode: AlertsPageFilters["viewMode"],
  clusterColorByKey: Map<string, string>,
): string {
  if (viewMode !== "cluster") {
    return scoreColor(point.alertScore);
  }

  const key = pointClusterGroupKey(point);
  if (key) {
    return clusterColorByKey.get(key) ?? "rgba(56,189,248,1)";
  }

  if (point.clusterContext?.clusterQuality === "small_sample") {
    return "rgba(150,118,62,1)";
  }

  return "rgba(148,163,184,1)";
}

function pointDisplaySize(
  point: AlertVisualPoint,
  viewMode: AlertsPageFilters["viewMode"],
): number {
  if (viewMode !== "cluster") {
    return 19 + point.alertScore * 11 + Math.max(0, point.confidence) * 5;
  }

  if (point.clusterContext?.clusterQuality === "clustered") {
    const clusterBoost = Math.min(point.clusterContext.clusterSize * 1.5, 8);
    const exemplarBoost = point.clusterContext.isExemplar ? 4 : 0;
    return 18 + clusterBoost + exemplarBoost;
  }

  if (point.clusterContext?.clusterQuality === "small_sample") {
    return 19;
  }

  return 16;
}

function pointDisplayLabel(
  point: AlertVisualPoint,
  viewMode: AlertsPageFilters["viewMode"],
): string {
  if (viewMode !== "cluster") return point.species;
  if (point.clusterContext) {
    return `${clusterStatusLabel(point.clusterContext)} · ${point.species}`;
  }
  return `No persisted cluster · ${point.species}`;
}

function buildValueOptions(
  points: AlertVisualPoint[],
  key: "species" | "station",
): DropdownOption[] {
  const grouped = new Map<
    string,
    { count: number; scientificName: string | null; imageUrl: string | null }
  >();

  for (const point of points) {
    const value = key === "species" ? point.species : point.station;
    if (!value) continue;

    const current = grouped.get(value);
    if (current) {
      current.count += 1;
      if (key === "species" && !current.scientificName && point.scientificName) {
        current.scientificName = point.scientificName;
      }
      if (key === "species" && !current.imageUrl && point.imageUrl) {
        current.imageUrl = point.imageUrl;
      }
      continue;
    }

    grouped.set(value, {
      count: 1,
      scientificName: point.scientificName,
      imageUrl: key === "species" ? point.imageUrl : null,
    });
  }

  return [...grouped.entries()]
    .sort((left, right) => right[1].count - left[1].count || left[0].localeCompare(right[0]))
    .map(([value, meta]) => ({
      value,
      label: value,
      detail:
        key === "species"
          ? meta.scientificName ?? `${meta.count.toLocaleString()} clips`
          : `${meta.count.toLocaleString()} clips`,
      imageUrl: key === "species" ? meta.imageUrl : null,
    }));
}


function scoreBands(points: AlertVisualPoint[]) {
  return points.reduce(
    (accumulator, point) => {
      if (point.alertScore >= 0.5) accumulator.high += 1;
      else if (point.alertScore >= 0.3) accumulator.moderate += 1;
      else accumulator.normal += 1;

      return accumulator;
    },
    { high: 0, moderate: 0, normal: 0 },
  );
}

function drawCircularImage(
  context: CanvasRenderingContext2D,
  image: HTMLImageElement,
  x: number,
  y: number,
  radius: number,
  opacity: number,
  overlay: string,
  tierColor?: string,
) {
  context.save();
  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.clip();
  context.globalAlpha = opacity;

  const imageRatio = image.naturalWidth / Math.max(image.naturalHeight, 1);
  const targetSize = radius * 2;
  let drawWidth = targetSize;
  let drawHeight = targetSize;
  if (imageRatio > 1) {
    drawWidth = targetSize * imageRatio;
  } else {
    drawHeight = targetSize / Math.max(imageRatio, 0.01);
  }

  context.drawImage(
    image,
    x - drawWidth / 2,
    y - drawHeight / 2,
    drawWidth,
    drawHeight,
  );
  context.globalAlpha = 1;
  context.fillStyle = overlay;
  context.fillRect(x - radius, y - radius, radius * 2, radius * 2);

  // Subtle warm wash — old print / vintage portrait feel.
  context.fillStyle = "rgba(178,138,98,0.10)";
  context.fillRect(x - radius, y - radius, radius * 2, radius * 2);

  // Inner vignette — portrait fades to dark at the edges.
  const vignette = context.createRadialGradient(x, y, radius * 0.18, x, y, radius);
  vignette.addColorStop(0, "rgba(0,0,0,0)");
  vignette.addColorStop(0.65, "rgba(0,0,0,0.20)");
  vignette.addColorStop(1, "rgba(0,0,0,0.58)");
  context.fillStyle = vignette;
  context.fillRect(x - radius, y - radius, radius * 2, radius * 2);

  // Tier-color cast — edges pick up the band tint so the bird ties to the palette.
  if (tierColor) {
    const tierVignette = context.createRadialGradient(x, y, radius * 0.5, x, y, radius);
    tierVignette.addColorStop(0, withAlpha(tierColor, 0));
    tierVignette.addColorStop(1, withAlpha(tierColor, 0.30));
    context.fillStyle = tierVignette;
    context.fillRect(x - radius, y - radius, radius * 2, radius * 2);
  }

  context.restore();
}

function drawEmbeddingCrossGuides(
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
) {
  const minDimension = Math.min(width, height);
  const inset = Math.max(12, Math.min(18, Math.round(minDimension * 0.028)));
  const cornerLength = Math.max(16, Math.min(24, Math.round(minDimension * 0.05)));
  const axisInset = inset + 2;

  context.save();
  context.lineCap = "square";
  context.lineJoin = "miter";

  context.strokeStyle = "rgba(128,128,128,0.18)";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(width / 2, axisInset);
  context.lineTo(width / 2, height - axisInset);
  context.moveTo(axisInset, height / 2);
  context.lineTo(width - axisInset, height / 2);
  context.stroke();

  context.restore();
}

function drawEmbeddingCornerGuides(
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
) {
  const minDimension = Math.min(width, height);
  const inset = Math.max(12, Math.min(18, Math.round(minDimension * 0.028)));
  const cornerLength = Math.max(18, Math.min(28, Math.round(minDimension * 0.056)));

  context.save();
  context.lineCap = "square";
  context.lineJoin = "miter";

  context.strokeStyle = "rgba(136,136,136,0.44)";
  context.lineWidth = 1.25;
  context.beginPath();

  context.moveTo(inset, inset + cornerLength);
  context.lineTo(inset, inset);
  context.lineTo(inset + cornerLength, inset);

  context.moveTo(width - inset - cornerLength, inset);
  context.lineTo(width - inset, inset);
  context.lineTo(width - inset, inset + cornerLength);

  context.moveTo(width - inset, height - inset - cornerLength);
  context.lineTo(width - inset, height - inset);
  context.lineTo(width - inset - cornerLength, height - inset);

  context.moveTo(inset + cornerLength, height - inset);
  context.lineTo(inset, height - inset);
  context.lineTo(inset, height - inset - cornerLength);

  context.stroke();
  context.restore();
}

const EmbeddingCanvasMap = memo(function EmbeddingCanvasMap({
  points,
  activePointIds,
  baselinePointIds,
  selectedId,
  viewMode,
  focusedBand,
  clusterColorByKey,
  emptyStateMessage,
  status,
  onSelectPoint,
  onHoverPoint,
}: {
  points: AlertVisualPoint[];
  activePointIds: Set<string>;
  baselinePointIds: Set<string>;
  selectedId: string | null;
  viewMode: AlertsPageFilters["viewMode"];
  focusedBand: AlertBand | null;
  clusterColorByKey: Map<string, string>;
  emptyStateMessage: string;
  status: "idle" | "loading" | "ready" | "error";
  onSelectPoint: (id: string) => void;
  onHoverPoint?: (id: string | null) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const shellRef = useRef<HTMLDivElement | null>(null);
  const imageCacheRef = useRef<Map<string, HTMLImageElement>>(new Map());
  const [imageVersion, setImageVersion] = useState(0);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return undefined;

    const updateSize = () => {
      const rect = shell.getBoundingClientRect();
      setCanvasSize({
        width: Math.max(1, Math.round(rect.width)),
        height: Math.max(1, Math.round(rect.height)),
      });
    };
    updateSize();

    const resizeObserver = new ResizeObserver(updateSize);
    resizeObserver.observe(shell);
    return () => resizeObserver.disconnect();
  }, []);

  useEffect(() => {
    const urls = [...new Set(points.map((point) => point.imageUrl).filter(Boolean) as string[])]
      .slice(0, 64);
    for (const url of urls) {
      if (imageCacheRef.current.has(url)) continue;

      const image = new Image();
      image.crossOrigin = "anonymous";
      image.decoding = "async";
      image.onload = () => setImageVersion((version) => version + 1);
      image.onerror = () => setImageVersion((version) => version + 1);
      image.src = url;
      imageCacheRef.current.set(url, image);
    }
  }, [points]);

  const hitPoints = useMemo<CanvasHitPoint[]>(() => {
    if (!canvasSize.width || !canvasSize.height) return [];

    return points.map((point, order) => ({
      id: point.id,
      x: point.x * canvasSize.width,
      y: point.y * canvasSize.height,
      radius: pointDisplaySize(point, viewMode) / 2 + 6,
      order,
    }));
  }, [canvasSize.height, canvasSize.width, points, viewMode]);
  const hitIndex = useMemo(() => buildCanvasHitIndex(hitPoints), [hitPoints]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !canvasSize.width || !canvasSize.height) return;

    const pixelRatio = window.devicePixelRatio || 1;
    canvas.width = Math.round(canvasSize.width * pixelRatio);
    canvas.height = Math.round(canvasSize.height * pixelRatio);
    canvas.style.width = `${canvasSize.width}px`;
    canvas.style.height = `${canvasSize.height}px`;

    const context = canvas.getContext("2d");
    if (!context) return;

    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, canvasSize.width, canvasSize.height);
    context.textAlign = "center";
    context.textBaseline = "middle";
    drawEmbeddingCrossGuides(context, canvasSize.width, canvasSize.height);

    const priorityPointIds = new Set<string>();
    if (hoveredId) {
      priorityPointIds.add(hoveredId);
    }
    if (selectedId) {
      priorityPointIds.add(selectedId);
    }
    for (const point of points) {
      if (
        baselinePointIds.has(point.id) ||
        point.clusterContext?.isExemplar ||
        point.alertScore >= 0.5
      ) {
        priorityPointIds.add(point.id);
      }
    }

    const drawPoint = (point: AlertVisualPoint) => {
      const isSelected = point.id === selectedId;
      const isHovered = point.id === hoveredId;
      const isBaselineReference = baselinePointIds.has(point.id);
      const isActivePoint = activePointIds.has(point.id);
      const pointBand = alertBand(point.alertScore);
      const isMuted =
        !isActivePoint ||
        (viewMode !== "cluster" &&
          focusedBand !== null &&
          pointBand !== focusedBand);
      const color = isMuted
        ? "rgba(148,163,184,1)"
        : pointDisplayColor(point, viewMode, clusterColorByKey);
      const baseDiameter = pointDisplaySize(point, viewMode);
      const diameter = isHovered ? baseDiameter * 1.28 : baseDiameter;
      const radius = diameter / 2;
      const x = point.x * canvasSize.width;
      const y = point.y * canvasSize.height;
      const imageTreatment = pointImageTreatment(isMuted);

      // Dark moat — invisible against canvas, separates overlapping nodes in clusters.
      context.fillStyle = "rgba(12,12,12,1)";
      context.beginPath();
      context.arc(x, y, radius + 0.8, 0, Math.PI * 2);
      context.fill();

      // Disc backing.
      context.fillStyle = "rgba(10,10,10,0.92)";
      context.beginPath();
      context.arc(x, y, radius, 0, Math.PI * 2);
      context.fill();

      const image = point.imageUrl ? imageCacheRef.current.get(point.imageUrl) : undefined;
      if (image?.complete && image.naturalWidth > 0) {
        drawCircularImage(
          context,
          image,
          x,
          y,
          radius,
          isSelected || isHovered ? 0.96 : imageTreatment.opacity,
          imageTreatment.overlay,
          isMuted ? undefined : color,
        );
      } else {
        // Fallback: keep the node legible without text until its image arrives.
        context.fillStyle = withAlpha(color, isMuted ? 0.06 : 0.14);
        context.beginPath();
        context.arc(x, y, radius, 0, Math.PI * 2);
        context.fill();
      }

      // Refined bezel — thin, slightly inset so the stroke isn't clipped.
      context.lineWidth = isSelected ? 1.6 : isHovered ? 1.5 : isMuted ? 0.8 : 1.2;
      context.strokeStyle = isSelected
        ? withAlpha(color, 0.96)
        : isHovered
          ? withAlpha(color, 0.95)
          : isMuted
            ? withAlpha(color, 0.32)
            : withAlpha(color, 0.78);
      context.beginPath();
      context.arc(x, y, radius - 0.5, 0, Math.PI * 2);
      context.stroke();

      // Hair-thin inner highlight — gives the bezel an "engraved coin" feel. Skipped on tiny nodes.
      if (!isMuted && radius > 5.5) {
        context.lineWidth = 0.5;
        context.strokeStyle = withAlpha(color, isSelected || isHovered ? 0.42 : 0.28);
        context.beginPath();
        context.arc(x, y, Math.max(radius - 2.4, radius * 0.55), 0, Math.PI * 2);
        context.stroke();
      }

      if (viewMode === "cluster" && point.clusterContext?.isExemplar) {
        context.save();
        context.setLineDash([4, 3]);
        context.strokeStyle = withAlpha(color, 0.60);
        context.lineWidth = 1.2;
        context.beginPath();
        context.arc(x, y, radius + 3, 0, Math.PI * 2);
        context.stroke();
        context.restore();
      }

      if (isBaselineReference) {
        context.fillStyle = withAlpha(BASELINE_HIGHLIGHT_COLOR, isMuted ? 0.24 : 0.96);
        context.strokeStyle = withAlpha(BASELINE_HIGHLIGHT_COLOR, isMuted ? 0.34 : 1);
        context.lineWidth = 1;
        context.beginPath();
        context.arc(x + radius - 2, y - radius + 2, 6, 0, Math.PI * 2);
        context.fill();
        context.stroke();
        context.fillStyle = "rgba(12,12,12,0.92)";
        context.font = "700 9px sans-serif";
        context.fillText("+", x + radius - 2, y - radius + 2.4);
      }

      if (isSelected) {
        // Thin colored outer ring for the selected point — no white, no glow.
        context.strokeStyle = withAlpha(color, 0.78);
        context.lineWidth = 1;
        context.beginPath();
        context.arc(x, y, radius + 3.5, 0, Math.PI * 2);
        context.stroke();
      }
    };

    for (const point of points) {
      if (priorityPointIds.has(point.id)) {
        continue;
      }
      drawPoint(point);
    }

    for (const point of points) {
      if (!priorityPointIds.has(point.id)) {
        continue;
      }
      drawPoint(point);
    }

    drawEmbeddingCornerGuides(context, canvasSize.width, canvasSize.height);
  }, [
    activePointIds,
    baselinePointIds,
    canvasSize.height,
    canvasSize.width,
    clusterColorByKey,
    focusedBand,
    hoveredId,
    imageVersion,
    points,
    selectedId,
    viewMode,
  ]);

  const moveHover = (clientX: number, clientY: number) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;

    const hit = findCanvasHitPoint(hitIndex, clientX - rect.left, clientY - rect.top);
    const nextId = hit?.id ?? null;
    setHoveredId((current) => (current === nextId ? current : nextId));
    onHoverPoint?.(nextId);
  };

  const selectAt = (clientX: number, clientY: number) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;

    const hit = findCanvasHitPoint(hitIndex, clientX - rect.left, clientY - rect.top);
    if (hit?.id) {
      onSelectPoint(hit.id);
    }
  };

  return (
    <div ref={shellRef} className="absolute inset-0">
      <canvas
        ref={canvasRef}
        role="img"
        aria-label="Embedding neighbourhood map"
        className="absolute inset-0 h-full w-full"
        style={{ cursor: hoveredId ? "pointer" : "default" }}
        onPointerMove={(event) => moveHover(event.clientX, event.clientY)}
        onPointerLeave={() => {
          setHoveredId(null);
          onHoverPoint?.(null);
        }}
        onPointerDown={(event) => {
          if (event.button !== 0) {
            return;
          }
          selectAt(event.clientX, event.clientY);
        }}
      />

      {points.length === 0 ? (
        status === "loading" ? (
          <AlertsLoadingState
            label="Loading embedding neighbourhood"
            className="absolute inset-0"
          />
        ) : (
        <div className="absolute inset-0 flex items-center justify-center px-6">
          <div
            className="max-w-xl rounded-2xl px-6 py-7 text-center"
            style={{
              background: NEUTRAL_SURFACE.panelDeep,
              border: `1px solid ${NEUTRAL_SURFACE.border}`,
            }}
          >
            <div className="mb-3 font-mono text-[10px] uppercase tracking-[0.24em]" style={{ color: "rgba(255,255,255,0.28)" }}>
              {status === "error" ? "Request failed" : "Empty slice"}
            </div>
            <h2 className="text-xl font-semibold tracking-tight" style={{ color: "rgba(255,255,255,0.92)" }}>
              {status === "error"
                ? "Embedding neighbourhood unavailable"
                : "No detections are visible in this neighbourhood map"}
            </h2>
            <p className="mt-3 text-sm leading-relaxed" style={{ color: "rgba(255,255,255,0.46)" }}>
              {status === "error"
                ? "The embedding map request failed. Check the local frontend logs and retry the current filters."
                : emptyStateMessage}
            </p>
          </div>
        </div>
        )
      ) : null}
    </div>
  );
});

function formatHotspotPreview(
  items: Array<{ label: string; count: number }>,
  limit = 3,
) {
  if (items.length === 0) return "No standout signals yet";

  const preview = items
    .slice(0, limit)
    .map((item) => `${item.label} ${item.count}`);

  if (items.length > limit) {
    preview.push(`+${items.length - limit} more`);
  }

  return preview.join(" · ");
}

function HotspotSignalIcons({
  cell,
  activeLens,
}: {
  cell: HotspotCellSummary;
  activeLens: HotspotLens | null;
}) {
  const signals = [...cell.overlapSignals].sort((left, right) => {
    if (left === activeLens) return -1;
    if (right === activeLens) return 1;

    const order: HotspotLens[] = ["bioacoustic"];
    return order.indexOf(left) - order.indexOf(right);
  });

  if (signals.length === 0) {
    return null;
  }

  return (
    <div className="flex flex-wrap justify-end gap-1">
      {signals.map((signal) => {
        const meta = HOTSPOT_LENS_META[signal];
        const Icon = meta.icon;
        const emphasized = activeLens === signal;
        const toneColor = hotspotLensToneColor(cell);

        return (
          <span
            key={signal}
            className="inline-flex h-6 w-6 items-center justify-center rounded-full"
            style={{
              background: emphasized ? withAlpha(toneColor, 0.14) : "rgba(255,255,255,0.04)",
              border: `1px solid ${emphasized ? withAlpha(toneColor, 0.26) : withAlpha("rgba(255,255,255,1)", 0.08)}`,
              color: emphasized ? withAlpha(toneColor, 0.98) : "rgba(255,255,255,0.72)",
            }}
            title={meta.label}
          >
            <Icon size={11} strokeWidth={2} />
          </span>
        );
      })}
    </div>
  );
}

type HotspotSignalSummary = {
  lens: HotspotLens;
  label: string;
  value: number;
  toneColor: string;
  highlighted: boolean;
  active: boolean;
};

function hotspotLensToneColor(cell: HotspotCellSummary): string {
  return hotspotSignalColor(cell.hotspotScore);
}

function buildHotspotSignalSummaries(
  cell: HotspotCellSummary,
  activeLens: HotspotLens | null,
): HotspotSignalSummary[] {
  const bioacousticCases = cell.highCount + cell.moderateCount;

  return [
    {
      lens: "bioacoustic",
      label: HOTSPOT_LENS_META.bioacoustic.label,
      value: bioacousticCases,
      toneColor: hotspotLensToneColor(cell),
      highlighted: cell.overlapSignals.includes("bioacoustic") || bioacousticCases > 0,
      active: activeLens === "bioacoustic",
    },
  ];
}

function HotspotSignalSummaryCard({
  summary,
}: {
  summary: HotspotSignalSummary;
}) {
  const emphasized = summary.active || summary.highlighted;

  return (
    <div
      className="min-w-0 rounded-lg px-2.5 py-2"
      style={{
        background: emphasized ? withAlpha(summary.toneColor, summary.active ? 0.16 : 0.10) : NEUTRAL_SURFACE.panelAlt,
        border: `1px solid ${withAlpha(
          emphasized ? summary.toneColor : "rgba(255,255,255,1)",
          summary.active ? 0.32 : summary.highlighted ? 0.18 : 0.08,
        )}`,
      }}
    >
      <div
        className="flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-[0.14em]"
        style={{ color: emphasized ? withAlpha(summary.toneColor, 0.96) : "rgba(255,255,255,0.42)" }}
      >
        <span
          className="h-1.5 w-1.5 shrink-0 rounded-full"
          style={{ background: emphasized ? withAlpha(summary.toneColor, 0.98) : "rgba(255,255,255,0.22)" }}
        />
        <span className="truncate">{summary.label}</span>
      </div>

      <div className="mt-1.5 flex items-baseline gap-1">
        <div
          className="truncate text-xl font-semibold tracking-tight"
          style={{ color: "rgba(255,255,255,0.92)" }}
        >
          {summary.value.toLocaleString()}
        </div>
        <div className="font-mono text-[9px] uppercase tracking-[0.14em]" style={{ color: "rgba(255,255,255,0.30)" }}>
          cases
        </div>
      </div>
    </div>
  );
}

function HotspotTitleVisual({
  cell,
  gridCell,
}: {
  cell: HotspotCellSummary;
  gridCell: HotspotGridCell | null;
}) {
  const centerLat = gridCell
    ? (gridCell.bboxMinLat + gridCell.bboxMaxLat) / 2
    : null;
  const centerLon = gridCell
    ? (gridCell.bboxMinLon + gridCell.bboxMaxLon) / 2
    : null;
  const googleMapsHref = centerLat !== null && centerLon !== null
    ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(
      `${centerLat.toFixed(6)},${centerLon.toFixed(6)}`,
    )}`
    : null;
  const coordinateTitle = centerLat !== null && centerLon !== null
    ? `${formatGeoCoordinate(centerLat, "lat")}, ${formatGeoCoordinate(centerLon, "lon")}`
    : "Selected grid cell";
  const gridId = gridCell?.gridCellId ?? cell.gridCellId;

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-2.5 sm:flex-row sm:items-center">
      {gridCell ? (
        <AlertsHotspotPreviewMap gridCell={gridCell} />
      ) : (
        <div
          className="flex h-[132px] w-full max-w-[220px] items-center justify-center rounded-lg text-xs"
          style={{
            background: NEUTRAL_SURFACE.panelAlt,
            border: `1px solid ${NEUTRAL_SURFACE.borderSoft}`,
            color: "rgba(255,255,255,0.44)",
          }}
        >
          No satellite preview available
        </div>
      )}

      <div className="min-w-0 sm:max-w-[200px]">
        <div className="font-mono text-[9px] uppercase tracking-[0.16em]" style={{ color: "rgba(255,255,255,0.34)" }}>
          Center coordinates
        </div>
        <h2 id="hotspot-detail-title" className="mt-1 text-lg font-semibold leading-snug tracking-tight" style={{ color: "rgba(255,255,255,0.94)" }}>
          {coordinateTitle}
        </h2>
        <div className="mt-1 text-xs leading-relaxed" style={{ color: "rgba(255,255,255,0.50)" }}>
          Grid ID {gridId}
        </div>
        {googleMapsHref ? (
          <a
            href={googleMapsHref}
            target="_blank"
            rel="noreferrer"
            className="mt-2 inline-flex items-center gap-1 text-[11px] transition-opacity hover:opacity-100"
            style={{ color: "rgba(255,255,255,0.72)" }}
          >
            <span>Open in Google Maps</span>
            <ArrowUpRight size={12} />
          </a>
        ) : null}
      </div>
    </div>
  );
}

function PanelTitle({
  icon: Icon,
  children,
  detail,
}: {
  icon: typeof SlidersHorizontal;
  children: React.ReactNode;
  detail?: string;
}) {
  return (
    <div className="mb-3 flex items-start justify-between gap-3">
      <div>
        <div
          className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.24em]"
          style={{ color: "rgba(255,255,255,0.42)" }}
        >
          <Icon size={12} />
          {children}
        </div>
        {detail ? (
          <p className="mt-1 text-xs leading-relaxed" style={{ color: "rgba(255,255,255,0.42)" }}>
            {detail}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function FieldShell({ children }: { children: React.ReactNode }) {
  return <div>{children}</div>;
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="mb-1 block font-mono text-[10px] uppercase tracking-[0.18em]"
      style={{ color: "rgba(255,255,255,0.30)" }}
    >
      {children}
    </div>
  );
}

function CompactDateInput({
  label,
  ariaLabel,
  value,
  min,
  max,
  description,
  onChange,
}: {
  label: string;
  ariaLabel: string;
  value: string;
  min?: string;
  max?: string;
  description?: string;
  onChange: (value: string) => void;
}) {
  return (
    <div>
      <FieldLabel>{label}</FieldLabel>
      <input
        type="date"
        aria-label={ariaLabel}
        value={value}
        min={min || undefined}
        max={max || undefined}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-xl px-2.5 py-2 text-sm outline-none [color-scheme:dark]"
        style={{
          background: NEUTRAL_SURFACE.panelAlt,
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          color: "rgba(255,255,255,0.86)",
        }}
      />
      {description ? (
        <div className="mt-1 text-[10px]" style={{ color: "rgba(255,255,255,0.34)" }}>
          {description}
        </div>
      ) : null}
    </div>
  );
}

function resolveWeekRangeFromWeek(
  periodStart: string,
  availableWeeks: AlertsPageData["availableWeeks"],
): Pick<AlertsPageFilters, "dateFrom" | "dateTo"> {
  if (!periodStart) {
    return { dateFrom: "", dateTo: "" };
  }

  const selectedWeek = availableWeeks.find((week) => week.periodStart === periodStart);
  if (!selectedWeek) {
    return { dateFrom: "", dateTo: "" };
  }

  return {
    dateFrom: selectedWeek.periodStart,
    dateTo: selectedWeek.periodEnd,
  };
}

function CompactDropdown({
  label,
  valueLabel,
  placeholder,
  options,
  searchable = false,
  searchValue,
  searchPlaceholder,
  onSearchChange,
  onSelect,
  onClear,
  emptyText,
}: {
  label: string;
  valueLabel: string;
  placeholder: string;
  options: DropdownOption[];
  searchable?: boolean;
  searchValue?: string;
  searchPlaceholder?: string;
  onSearchChange?: (value: string) => void;
  onSelect: (value: string) => void;
  onClear?: () => void;
  emptyText?: string;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const shellRef = useRef<HTMLDivElement | null>(null);
  const selectedOption = options.find(
    (option) => option.value === valueLabel || option.label === valueLabel,
  );
  const normalizedSearch = searchValue?.trim().toLowerCase() ?? "";
  const filteredOptions = normalizedSearch
    ? options.filter((option) => {
        const haystack = [option.label, option.detail]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        return haystack.includes(normalizedSearch);
      })
    : options;

  useEffect(() => {
    if (!isOpen) return undefined;

    const handlePointerDown = (event: MouseEvent) => {
      if (!shellRef.current?.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };

    window.addEventListener("mousedown", handlePointerDown);
    return () => window.removeEventListener("mousedown", handlePointerDown);
  }, [isOpen]);

  const hasTypedQuery = searchable && Boolean(searchValue?.trim());

  return (
    <div ref={shellRef} className="relative">
      <FieldLabel>{label}</FieldLabel>
      <button
        type="button"
        onClick={() => setIsOpen((current) => !current)}
        className="flex w-full items-center justify-between gap-3 rounded-xl px-2.5 py-2 text-left"
        style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}` }}
      >
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {selectedOption?.imageUrl ? (
            <img
              src={selectedOption.imageUrl}
              alt=""
              className="h-7 w-7 shrink-0 rounded-lg object-cover"
              loading="lazy"
              decoding="async"
            />
          ) : null}
          <span
            className="min-w-0 flex-1 truncate text-sm"
            style={{ color: valueLabel ? "rgba(255,255,255,0.86)" : "rgba(255,255,255,0.40)" }}
          >
            {valueLabel || placeholder}
          </span>
        </div>
        <ChevronDown size={14} style={{ color: "rgba(255,255,255,0.36)" }} className="shrink-0" />
      </button>

      {isOpen ? (
        <div
          className="absolute left-0 right-0 top-[calc(100%+6px)] z-30 rounded-xl p-1.5 shadow-2xl"
          style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.border}`, boxShadow: "0 20px 70px rgba(0,0,0,0.46)" }}
        >
          {searchable ? (
            <div className="mb-1.5 rounded-xl px-2.5 py-1.5" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}` }}>
              <div className="flex items-center gap-2">
                <Search size={14} style={{ color: "rgba(255,255,255,0.28)" }} />
                <input
                  value={searchValue ?? ""}
                  onChange={(event) => onSearchChange?.(event.target.value)}
                  placeholder={searchPlaceholder ?? "Search"}
                  className="w-full bg-transparent text-sm outline-none"
                  style={{ color: "rgba(255,255,255,0.86)" }}
                />
              </div>
            </div>
          ) : null}

          <div className="flex max-h-72 flex-col gap-1 overflow-y-auto pr-1">
            {hasTypedQuery ? (
              <button
                type="button"
                onClick={() => setIsOpen(false)}
                className="rounded-xl px-2.5 py-1.5 text-left"
                style={{ background: "rgba(94,124,74,0.12)", border: "1px solid rgba(94,124,74,0.18)", color: "rgba(220,232,194,0.92)" }}
              >
                <div className="text-sm">Use “{searchValue?.trim()}”</div>
              </button>
            ) : null}

            {onClear ? (
              <button
                type="button"
                onClick={() => {
                  onClear();
                  setIsOpen(false);
                }}
                className="rounded-xl px-2.5 py-1.5 text-left"
                style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.70)" }}
              >
                Clear selection
              </button>
            ) : null}

            {filteredOptions.length > 0 ? filteredOptions.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => {
                  onSelect(option.value);
                  setIsOpen(false);
                }}
                className="rounded-xl px-2.5 py-1.5 text-left"
                style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.82)" }}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    {option.imageUrl ? (
                      <img
                        src={option.imageUrl}
                        alt=""
                        className="h-8 w-8 shrink-0 rounded-lg object-cover"
                        loading="lazy"
                        decoding="async"
                      />
                    ) : null}
                    <div className="truncate text-sm">{option.label}</div>
                  </div>
                  {option.detail ? (
                    <div className="shrink-0 text-[10px]" style={{ color: "rgba(255,255,255,0.42)" }}>
                      {option.detail}
                    </div>
                  ) : null}
                </div>
              </button>
            )) : (
              <div className="rounded-xl px-2.5 py-1.5 text-sm" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.44)" }}>
                {emptyText ?? "No matching options"}
              </div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function MonitorTabButton({
  active,
  label,
  detail,
  onClick,
}: {
  active: boolean;
  label: string;
  detail?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={active
        ? "monitor-tab-button relative z-20 min-w-0 rounded-t-2xl rounded-b-none px-4 pb-3 pt-2.5 text-left transition-all duration-150"
        : "monitor-tab-button relative min-w-0 rounded-2xl px-3.5 py-2 text-left transition-all duration-150"}
      style={{
        marginBottom: active ? -1 : 0,
        transform: active ? "translateY(1px)" : "translateY(0)",
        background: active ? NEUTRAL_SURFACE.panel : "rgba(14,14,14,0.78)",
        border: `1px solid ${NEUTRAL_SURFACE.borderSoft}`,
        borderBottomColor: active ? NEUTRAL_SURFACE.panel : NEUTRAL_SURFACE.borderSoft,
        boxShadow: active
          ? "0 12px 24px rgba(0,0,0,0.18), inset 0 1px 0 rgba(255,255,255,0.02)"
          : "inset 0 1px 0 rgba(255,255,255,0.01)",
        color: active ? "rgba(255,255,255,0.92)" : "rgba(255,255,255,0.56)",
      }}
    >
      <div className="font-mono text-[10px] uppercase tracking-[0.24em]">{label}</div>
      {detail ? (
        <div className="mt-1 text-xs" style={{ color: active ? "rgba(225,255,241,0.72)" : "rgba(255,255,255,0.40)" }}>
          {detail}
        </div>
      ) : null}
    </button>
  );
}

function pointWeeklyPeriodStart(point: AlertVisualPoint): string | null {
  return point.reviewContext?.weeklyPeriodStart ?? point.clusterContext?.weeklyPeriodStart ?? null;
}

const AnomalyListItem = memo(function AnomalyListItem({
  point,
  index,
  isActive,
  isBaselineReference,
  viewMode,
  clusterColorByKey,
  onOpen,
  onPrewarm,
  registerRef,
}: {
  point: AlertVisualPoint;
  index: number;
  isActive: boolean;
  isBaselineReference: boolean;
  viewMode: AlertsPageFilters["viewMode"];
  clusterColorByKey: Map<string, string>;
  onOpen: (id: string) => void;
  onPrewarm: (id: string | null) => void;
  registerRef: (pointId: string, node: HTMLButtonElement | null) => void;
}) {
  const color = pointDisplayColor(point, viewMode, clusterColorByKey);
  const imageTreatment = pointImageTreatment(false);

  return (
    <button
      ref={(node) => {
        registerRef(point.id, node);
      }}
      type="button"
      onClick={() => onOpen(point.id)}
      onPointerEnter={() => onPrewarm(point.id)}
      onPointerDown={() => onPrewarm(point.id)}
      onFocus={() => onPrewarm(point.id)}
      className="w-full rounded-xl px-3 py-2.5 text-left transition-colors"
      style={{
        background: isActive
          ? withAlpha(color, 0.10)
          : isBaselineReference
            ? withAlpha(BASELINE_HIGHLIGHT_COLOR, 0.06)
            : "rgba(255,255,255,0.02)",
        border: `1px solid ${isActive
          ? withAlpha(color, 0.28)
          : isBaselineReference
            ? withAlpha(BASELINE_HIGHLIGHT_COLOR, 0.20)
            : "#13161e"}`,
        boxShadow: isActive ? `inset 3px 0 0 ${withAlpha(color, 0.75)}` : "none",
      }}
    >
      <div className="flex items-start gap-2.5">
        <span className="w-5 shrink-0 pt-0.5 font-mono text-[10px] tabular-nums" style={{ color: "rgba(255,255,255,0.24)" }}>
          {String(index + 1).padStart(2, "0")}
        </span>

        <div className="relative h-10 w-10 shrink-0 overflow-hidden rounded-lg" style={{ border: `1.5px solid ${withAlpha(color, 0.28)}` }}>
          {point.imageUrl ? (
            <>
              <img src={point.imageUrl} alt={point.species} loading="lazy" decoding="async" className="h-full w-full object-cover" style={{ filter: imageTreatment.filter, opacity: imageTreatment.opacity }} />
              <span className="pointer-events-none absolute inset-0" style={{ background: imageTreatment.overlay }} />
            </>
          ) : (
            <div className="flex h-full w-full items-center justify-center text-[10px] font-semibold uppercase" style={{ color: "rgba(255,255,255,0.40)" }}>
              {point.species.slice(0, 2)}
            </div>
          )}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-[12px] font-medium" style={{ color: "rgba(255,255,255,0.84)" }}>
                {point.species}
              </div>
              <div className="mt-0.5 truncate text-[10px]" style={{ color: "rgba(255,255,255,0.40)" }}>
                {point.station}
              </div>
            </div>
            <div className="font-mono text-[10px] tabular-nums" style={{ color: withAlpha(color, 0.88) }}>
              {Math.round(point.alertScore * 100)}%
            </div>
          </div>

          <div className="mt-1 truncate text-[10px]" style={{ color: "rgba(255,255,255,0.48)" }}>
            {formatShortDate(point.detectedAt)} · {formatBackendLabel(point.embeddingBackend)}
          </div>

          <div className="mt-1.5 flex flex-wrap gap-1.5 text-[9px] uppercase">
            <span
              className="rounded-full px-1.5 py-0.5"
              style={{
                background: withAlpha(color, 0.12),
                border: `1px solid ${withAlpha(color, 0.20)}`,
                color: withAlpha(color, 0.92),
              }}
            >
              {scoreLabel(point.alertScore)}
            </span>
            {point.reviewContext && hasCorroboratingContext(point.reviewContext) ? (
              <span
                className="rounded-full px-1.5 py-0.5"
                style={{
                  background: withAlpha(corroborationToneColor(point.reviewContext), 0.10),
                  border: `1px solid ${withAlpha(corroborationToneColor(point.reviewContext), 0.18)}`,
                  color: withAlpha(corroborationToneColor(point.reviewContext), 0.92),
                }}
              >
                Context+
              </span>
            ) : null}
            {isBaselineReference ? (
              <span
                className="rounded-full px-1.5 py-0.5"
                style={{
                  background: withAlpha(BASELINE_HIGHLIGHT_COLOR, 0.12),
                  border: `1px solid ${withAlpha(BASELINE_HIGHLIGHT_COLOR, 0.22)}`,
                  color: withAlpha(BASELINE_HIGHLIGHT_COLOR, 0.94),
                }}
              >
                Baseline
              </span>
            ) : null}
          </div>
        </div>
      </div>
    </button>
  );
});

type AlertReviewTab = "overview" | "context" | "compare";

function HotspotDetailModal({
  cell,
  gridCell,
  onClose,
  activeLens,
}: {
  cell: HotspotCellSummary;
  gridCell: HotspotGridCell | null;
  onClose: () => void;
  activeLens: HotspotLens | null;
}) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const signalSummaries = buildHotspotSignalSummaries(cell, activeLens);

  useEffect(() => {
    closeButtonRef.current?.focus();
  }, [cell.gridCellId]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-2.5 sm:p-3">
      <button
        type="button"
        className="absolute inset-0"
        style={{ background: "transparent" }}
        onClick={onClose}
        aria-label="Close hotspot detail modal"
      />

      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="hotspot-detail-title"
        className="relative z-10 w-full max-w-[560px] overflow-hidden rounded-xl"
        style={{
          background: NEUTRAL_SURFACE.panelDeep,
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          boxShadow: "0 18px 56px rgba(0,0,0,0.54)",
        }}
      >
        <div className="border-b px-3 py-3" style={{ borderColor: NEUTRAL_SURFACE.borderSoft }}>
          <div className="flex items-start justify-between gap-2.5">
            <HotspotTitleVisual cell={cell} gridCell={gridCell} />

            <button
              ref={closeButtonRef}
              type="button"
              onClick={onClose}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full transition-colors"
              style={{
                border: `1px solid ${NEUTRAL_SURFACE.border}`,
                background: NEUTRAL_SURFACE.panelAlt,
                color: "rgba(255,255,255,0.68)",
              }}
              aria-label="Close hotspot detail modal"
            >
              <X size={14} />
            </button>
          </div>
        </div>

        <div className="p-2.5">
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
            {signalSummaries.map((summary) => (
              <HotspotSignalSummaryCard key={summary.lens} summary={summary} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function AlertsClient({ data: initialData }: { data: AlertsPageData }) {
  const router = useRouter();
  const pathname = usePathname();
  const [isPending, startRouteTransition] = useTransition();
  const [data, setData] = useState<AlertsPageData>(initialData);
  const [embeddingStatus, setEmbeddingStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  // Dedicated state for the spatial early-warning panel, independent of the
  // legacy trend-evaluation status machinery. Seeded from any report already
  // present on initialData; otherwise fetched lazily when the tab opens.
  const [modelPerformanceReport, setModelPerformanceReport] =
    useState<ModelPerformanceReport | null>(initialData.modelPerformance ?? null);
  const [modelPerformanceStatus, setModelPerformanceStatus] = useState<
    "idle" | "loading" | "ready" | "error"
  >(initialData.modelPerformance ? "ready" : "idle");
  // Monotonic request id so only the latest model-performance fetch may write
  // state (survives StrictMode's dev mount/unmount/remount).
  const modelPerformanceRequestRef = useRef(0);
  const [draftFilters, setDraftFilters] = useState<AlertsPageFilters>(data.filters);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedModalPoint, setSelectedModalPoint] = useState<AlertVisualPoint | null>(null);
  const [selectionContextPointId, setSelectionContextPointId] = useState<string | null>(null);
  const [modalActiveTab, setModalActiveTab] = useState<AlertReviewTab>("overview");
  const pendingSelectionFrameRef = useRef<number | null>(null);
  const [anomalyRailVisibleCount, setAnomalyRailVisibleCount] = useState(
    ANOMALY_RAIL_INITIAL_COUNT,
  );
  const anomalyItemRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const fetchedDetailIdsRef = useRef<Set<string>>(new Set());
  const warmedMediaIdsRef = useRef<Set<string>>(new Set());
  const [gridCells, setGridCells] = useState<HotspotGridCell[]>([]);
  const [gridCellsStatus, setGridCellsStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [selectedHotspotId, setSelectedHotspotId] = useState<string | null>(null);
  const [openHotspotDetailId, setOpenHotspotDetailId] = useState<string | null>(null);
  const [hotspotFilterId, setHotspotFilterId] = useState<string | null>(null);
  const hotspotRowRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const [activeMonitorTab, setActiveMonitorTab] = useState<MonitorTab>("embeddings");
  const [focusedBand, setFocusedBand] = useState<AlertBand | null>(null);
  const [hotspotSourceFocus, setHotspotSourceFocus] = useState<HotspotLens | null>(null);
  const [hotspotGrainFocus, setHotspotGrainFocus] = useState<HotspotGrain>("grid");
  const [hotspotSummary, setHotspotSummary] =
    useState<HotspotSummaryResponse | null>(null);
  const [hotspotSummaryStatus, setHotspotSummaryStatus] = useState<
    "idle" | "loading" | "ready" | "error"
  >("idle");
  const [selectedEvaluationAnomalyType, setSelectedEvaluationAnomalyType] =
    useState(DEFAULT_EVALUATION_ANOMALY_TYPE);
  const [selectedEvaluationAlertLevel, setSelectedEvaluationAlertLevel] =
    useState(DEFAULT_EVALUATION_ALERT_LEVEL);
  const [selectedEvaluationHorizon, setSelectedEvaluationHorizon] = useState(0);
  const [draftEvaluationAnomalyType, setDraftEvaluationAnomalyType] =
    useState(DEFAULT_EVALUATION_ANOMALY_TYPE);
  const [draftEvaluationAlertLevel, setDraftEvaluationAlertLevel] =
    useState(DEFAULT_EVALUATION_ALERT_LEVEL);
  const [draftEvaluationHorizon, setDraftEvaluationHorizon] = useState(0);
  const currentFilterQuery = useMemo(() => buildSearchParams(data.filters), [data.filters]);
  const shouldComputeEvaluation = activeMonitorTab === "evaluation";
  const shouldComputeHotspots =
    activeMonitorTab === "hotspot" ||
    hotspotFilterId !== null ||
    openHotspotDetailId !== null ||
    selectedHotspotId !== null ||
    hotspotSourceFocus !== null;
  const spatialValidationReport = modelPerformanceReport?.spatialValidation ?? null;
  const evaluationAnomalyTypes = useMemo(() => {
    const present = new Set<string>();
    if (spatialValidationReport) {
      for (const row of spatialValidationReport.evaluationTable) {
        present.add(row.anomalyType);
      }
      for (const variant of spatialValidationReport.scoreVariants) {
        present.add(variant);
      }
    }
    if (present.size === 0) return EMPTY_STRING_LIST;
    return [
      ...EVALUATION_ANOMALY_TYPE_ORDER.filter((value) => present.has(value)),
      ...[...present].filter(
        (value) =>
          !EVALUATION_ANOMALY_TYPE_ORDER.includes(
            value as (typeof EVALUATION_ANOMALY_TYPE_ORDER)[number],
          ),
      ),
    ];
  }, [spatialValidationReport]);
  const evaluationAlertLevels = useMemo(() => {
    const present = new Set<string>();
    if (spatialValidationReport) {
      for (const row of spatialValidationReport.evaluationTable) {
        present.add(row.alertLevel);
      }
    }
    if (present.size === 0) return EMPTY_STRING_LIST;
    return [
      ...EVALUATION_ALERT_LEVEL_ORDER.filter((value) => present.has(value)),
      ...[...present].filter(
        (value) =>
          !EVALUATION_ALERT_LEVEL_ORDER.includes(
            value as (typeof EVALUATION_ALERT_LEVEL_ORDER)[number],
          ),
      ),
    ];
  }, [spatialValidationReport]);
  const evaluationHorizons = spatialValidationReport?.horizonsWeeks ?? EMPTY_NUMBER_LIST;

  useEffect(() => {
    let cancelled = false;
    const query = buildSearchParams(initialData.filters);
    const hasInitialEmbeddingData = initialData.referencePoints.length > 0;

    setData(initialData);
    setSelectedId(null);
    setSelectedModalPoint(null);
    setModalActiveTab("overview");
    setFocusedBand(null);
    fetchedDetailIdsRef.current = new Set();
    setEmbeddingStatus(hasInitialEmbeddingData ? "ready" : "loading");

    if (hasInitialEmbeddingData) {
      return () => {
        cancelled = true;
      };
    }

    fetch(`/api/alerts/embedding-map${query ? `?${query}` : ""}`)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Embedding map request failed with ${response.status}`);
        }

        return response.json() as Promise<PackedAlertsEmbeddingMapPayload>;
      })
      .then((payload) => {
        if (cancelled) return;
        setData(unpackAlertsEmbeddingMap(payload, initialData));
        setEmbeddingStatus("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setEmbeddingStatus("error");
      });

    return () => {
      cancelled = true;
    };
  }, [initialData]);

  useEffect(() => {
    // Fetch the spatial report when the evaluation tab is open and we don't yet
    // have it. The report is a single static artifact and does not depend on
    // the alerts filters, so this is intentionally NOT keyed on the filter
    // query.
    //
    // We guard against React 18+ StrictMode's mount/unmount/remount in dev (and
    // any unrelated re-render) using a ref rather than a closure flag: the old
    // closure-`cancelled` approach left the panel stuck on "loading" because
    // StrictMode's first-pass cleanup cancelled the only in-flight request and
    // the bailing `.then` never set status to "ready".
    if (activeMonitorTab !== "evaluation") {
      return undefined;
    }
    if (modelPerformanceStatus !== "idle") {
      return undefined;
    }

    const requestId = modelPerformanceRequestRef.current + 1;
    modelPerformanceRequestRef.current = requestId;
    setModelPerformanceStatus("loading");

    fetch(`/api/alerts/model-performance`)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Evaluation request failed with ${response.status}`);
        }
        return response.json() as Promise<{
          modelPerformance?: ModelPerformanceReport | null;
        }>;
      })
      .then((payload) => {
        // Only the most recent request may write state.
        if (modelPerformanceRequestRef.current !== requestId) return;
        setModelPerformanceReport(payload.modelPerformance ?? null);
        setModelPerformanceStatus("ready");
      })
      .catch(() => {
        if (modelPerformanceRequestRef.current !== requestId) return;
        setModelPerformanceStatus("error");
      });

    return undefined;
  }, [activeMonitorTab, modelPerformanceStatus]);

  const modeledWeeks = useMemo(
    () => [...data.availableWeeks].sort((left, right) => right.periodStart.localeCompare(left.periodStart) || right.periodEnd.localeCompare(left.periodEnd)),
    [data.availableWeeks],
  );
  const modeledWeekByStart = useMemo(
    () => new Map(modeledWeeks.map((week) => [week.periodStart, week])),
    [modeledWeeks],
  );
  const weekOptions = useMemo(
    () => buildModeledWeekDropdownOptions(modeledWeeks),
    [modeledWeeks],
  );
  const selectedWeekLabel = draftFilters.dateFrom && draftFilters.dateTo
    ? formatModeledWeekRangeLabel({
        periodStart: draftFilters.dateFrom,
        periodEnd: draftFilters.dateTo,
      })
    : draftFilters.dateFrom
      ? modeledWeekByStart.get(draftFilters.dateFrom)
        ? formatModeledWeekRangeLabel(modeledWeekByStart.get(draftFilters.dateFrom)!)
        : draftFilters.dateFrom
      : "";
  const activeEvaluationAnomalyType = evaluationAnomalyTypes.includes(selectedEvaluationAnomalyType)
    ? selectedEvaluationAnomalyType
    : evaluationAnomalyTypes.includes(DEFAULT_EVALUATION_ANOMALY_TYPE)
      ? DEFAULT_EVALUATION_ANOMALY_TYPE
      : (evaluationAnomalyTypes[0] ?? DEFAULT_EVALUATION_ANOMALY_TYPE);
  const activeEvaluationAlertLevel = evaluationAlertLevels.includes(selectedEvaluationAlertLevel)
    ? selectedEvaluationAlertLevel
    : evaluationAlertLevels.includes(DEFAULT_EVALUATION_ALERT_LEVEL)
      ? DEFAULT_EVALUATION_ALERT_LEVEL
      : (evaluationAlertLevels[0] ?? DEFAULT_EVALUATION_ALERT_LEVEL);
  const activeEvaluationHorizon = evaluationHorizons.includes(selectedEvaluationHorizon)
    ? selectedEvaluationHorizon
    : evaluationHorizons.includes(3)
      ? 3
      : (evaluationHorizons[0] ?? 0);

  useEffect(() => {
    if (!shouldComputeEvaluation) {
      return;
    }

    setSelectedEvaluationAnomalyType((current) => {
      if (current && evaluationAnomalyTypes.includes(current)) {
        return current;
      }

      if (evaluationAnomalyTypes.includes(DEFAULT_EVALUATION_ANOMALY_TYPE)) {
        return DEFAULT_EVALUATION_ANOMALY_TYPE;
      }

      return evaluationAnomalyTypes[0] ?? DEFAULT_EVALUATION_ANOMALY_TYPE;
    });
  }, [evaluationAnomalyTypes, shouldComputeEvaluation]);

  useEffect(() => {
    if (!shouldComputeEvaluation) {
      return;
    }

    setSelectedEvaluationAlertLevel((current) => {
      if (current && evaluationAlertLevels.includes(current)) {
        return current;
      }

      if (evaluationAlertLevels.includes(DEFAULT_EVALUATION_ALERT_LEVEL)) {
        return DEFAULT_EVALUATION_ALERT_LEVEL;
      }

      return evaluationAlertLevels[0] ?? DEFAULT_EVALUATION_ALERT_LEVEL;
    });
  }, [evaluationAlertLevels, shouldComputeEvaluation]);

  useEffect(() => {
    if (!shouldComputeEvaluation) {
      return;
    }

    setSelectedEvaluationHorizon((current) => {
      if (evaluationHorizons.includes(current)) {
        return current;
      }

      if (evaluationHorizons.includes(3)) {
        return 3;
      }

      return evaluationHorizons[0] ?? 0;
    });
  }, [evaluationHorizons, shouldComputeEvaluation]);

  useEffect(() => {
    if (!shouldComputeEvaluation) {
      return;
    }

    setDraftEvaluationAnomalyType(activeEvaluationAnomalyType);
  }, [activeEvaluationAnomalyType, shouldComputeEvaluation]);

  useEffect(() => {
    if (!shouldComputeEvaluation) {
      return;
    }

    setDraftEvaluationAlertLevel(activeEvaluationAlertLevel);
  }, [activeEvaluationAlertLevel, shouldComputeEvaluation]);

  useEffect(() => {
    if (!shouldComputeEvaluation) {
      return;
    }

    setDraftEvaluationHorizon(activeEvaluationHorizon);
  }, [activeEvaluationHorizon, shouldComputeEvaluation]);

  const clusterGroups = useMemo(() => buildClusterGroups(data.referencePoints), [data.referencePoints]);
  const clusterColorByKey = useMemo(
    () => new Map(clusterGroups.map((group, index) => [group.key, clusterPaletteColor(index)])),
    [clusterGroups],
  );
  const filteredPoints = useMemo(() => {
    if (data.filters.viewMode !== "cluster") {
      return data.points;
    }

    let next = data.points.filter((point) =>
      clusterScopeMatches(point, data.filters.clusterScope),
    );
    if (data.filters.clusterKey) {
      next = next.filter(
        (point) => pointClusterGroupKey(point) === data.filters.clusterKey,
      );
    }
    return next;
  }, [
    data.points,
    data.filters.viewMode,
    data.filters.clusterScope,
    data.filters.clusterKey,
  ]);
  const workspacePoints = useMemo(() => {
    if (data.filters.viewMode !== "cluster") {
      return data.referencePoints;
    }

    let next = data.referencePoints.filter((point) =>
      clusterScopeMatches(point, data.filters.clusterScope),
    );
    if (data.filters.clusterKey) {
      next = next.filter(
        (point) => pointClusterGroupKey(point) === data.filters.clusterKey,
      );
    }
    return next;
  }, [
    data.referencePoints,
    data.filters.viewMode,
    data.filters.clusterScope,
    data.filters.clusterKey,
  ]);
  const visibleClusterGroups = useMemo(
    () => buildClusterGroups(workspacePoints),
    [workspacePoints],
  );
  const sortedPoints = useMemo(
    () => sortPoints(filteredPoints, data.filters.sort),
    [filteredPoints, data.filters.sort],
  );
  const sortedWorkspacePoints = useMemo(
    () => sortPoints(workspacePoints, data.filters.sort),
    [workspacePoints, data.filters.sort],
  );
  const sortedWorkspacePointMeta = useMemo(() => {
    const next = new Map<string, { point: AlertVisualPoint; index: number }>();

    sortedWorkspacePoints.forEach((point, index) => {
      next.set(point.id, { point, index });
    });

    return next;
  }, [sortedWorkspacePoints]);
  const referencePointIndex = useMemo(
    () => buildReferencePointIndex(data.referencePoints),
    [data.referencePoints],
  );
  const activePointIds = useMemo(
    () => new Set(sortedPoints.map((point) => point.id)),
    [sortedPoints],
  );
  const selectedMeta = selectedId ? sortedWorkspacePointMeta.get(selectedId) ?? null : null;
  const selectedIndex = selectedMeta?.index ?? -1;
  const selected = selectedMeta?.point ?? null;
  const modalPoint = selected ??
    (selectedModalPoint
      ? sortedWorkspacePointMeta.get(selectedModalPoint.id)?.point ?? selectedModalPoint
      : null);
  const modalIndex = modalPoint
    ? (sortedWorkspacePointMeta.get(modalPoint.id)?.index ?? selectedIndex)
    : -1;
  const selectionContextPoint = selectionContextPointId
    ? sortedWorkspacePointMeta.get(selectionContextPointId)?.point ??
      (selectedModalPoint?.id === selectionContextPointId ? selectedModalPoint : null)
    : null;
  const warmPointMedia = useCallback((point: AlertVisualPoint | null) => {
    if (!point) {
      return;
    }

    const mediaUrls = [
      point.imageUrl,
      point.visualUrls.spectrogram,
      point.audioUrls.processed,
    ].filter(Boolean);

    if (mediaUrls.length === 0 || warmedMediaIdsRef.current.has(point.id)) {
      return;
    }

    warmedMediaIdsRef.current.add(point.id);

    if (point.imageUrl) {
      const image = new Image();
      image.decoding = "async";
      image.src = point.imageUrl;
    }

    if (point.visualUrls.spectrogram) {
      const image = new Image();
      image.decoding = "async";
      image.src = point.visualUrls.spectrogram;
    }

    if (point.audioUrls.processed) {
      void fetch(point.audioUrls.processed, { cache: "force-cache" }).catch(() => {
        warmedMediaIdsRef.current.delete(point.id);
      });
    }
  }, []);
  const primePointDetail = useCallback(
    (pointId: string, fallbackPoint?: AlertVisualPoint | null) => {
      const knownPoint =
        fallbackPoint ??
        sortedWorkspacePointMeta.get(pointId)?.point ??
        referencePointIndex.byId.get(pointId)?.point ??
        null;

      if (!knownPoint) {
        return;
      }

      if (pointHasLoadedDetail(knownPoint)) {
        warmPointMedia(knownPoint);
        return;
      }

      if (fetchedDetailIdsRef.current.has(pointId)) {
        return;
      }

      fetchedDetailIdsRef.current.add(pointId);
      const separator = currentFilterQuery ? "&" : "";

      fetch(
        `/api/alerts/embedding-detail?${currentFilterQuery}${separator}detectionId=${encodeURIComponent(pointId)}`,
      )
        .then(async (response) => {
          if (!response.ok) {
            throw new Error(`Embedding detail request failed with ${response.status}`);
          }

          return response.json() as Promise<{ point?: AlertVisualPoint }>;
        })
        .then((payload) => {
          if (!payload.point) {
            fetchedDetailIdsRef.current.delete(pointId);
            return;
          }

          const detailPoint = payload.point;

          warmPointMedia(detailPoint);
          setSelectedModalPoint((current) => {
            if (!current) {
              return null;
            }

            return current.id === detailPoint.id ? detailPoint : current;
          });
          setData((current) => mergeAlertDetailPoint(current, detailPoint));
        })
        .catch(() => {
          fetchedDetailIdsRef.current.delete(pointId);
        });
    },
    [currentFilterQuery, referencePointIndex, sortedWorkspacePointMeta, warmPointMedia],
  );
  useEffect(() => {
    if (!selectedId || !selected) return undefined;
    primePointDetail(selectedId, selected);
    return undefined;
  }, [primePointDetail, selected, selectedId]);
  useEffect(() => {
    if (!modalPoint) {
      setSelectionContextPointId(null);
      return undefined;
    }

    setSelectionContextPointId(null);
    let commitFrame: number | null = null;
    const prepFrame = window.requestAnimationFrame(() => {
      commitFrame = window.requestAnimationFrame(() => {
        setSelectionContextPointId(modalPoint.id);
      });
    });

    return () => {
      window.cancelAnimationFrame(prepFrame);
      if (commitFrame !== null) {
        window.cancelAnimationFrame(commitFrame);
      }
    };
  }, [modalPoint]);
  useEffect(() => {
    if (embeddingStatus !== "ready") {
      return undefined;
    }

    const warmCandidates = sortedPoints
      .filter(
        (point) =>
          Boolean(point.visualUrls.spectrogram || point.audioUrls.processed),
      )
      .slice(0, 12);

    for (const point of warmCandidates) {
      warmPointMedia(point);
    }

    return undefined;
  }, [embeddingStatus, sortedPoints, warmPointMedia]);
  const speciesOptions = useMemo(
    () => buildValueOptions(data.referencePoints, "species"),
    [data.referencePoints],
  );
  const evaluationAnomalyTypeOptions = useMemo(
    () =>
      evaluationAnomalyTypes.map((value) => ({
        value,
        label: SPATIAL_VARIANT_NAMES[value] ?? value,
        detail:
          value === "embedding"
            ? "Bioacoustic"
            : value === "bioacoustic_excess"
              ? "Scene-adjusted"
              : value === "bioacoustic_outlier"
                ? "Outlier share"
            : value === "fusion"
              ? "Fused score"
              : value === "population"
                ? "Species tracker"
                : value,
      })),
    [evaluationAnomalyTypes],
  );
  const evaluationAlertLevelOptions = useMemo(
    () =>
      evaluationAlertLevels.map((value) => ({
        value,
        label: SPATIAL_ALERT_LEVEL_NAMES[value] ?? value,
        detail:
          value === "amber"
            ? "Top 5%"
            : value === "red"
              ? "Top 1%"
              : value === "optimized"
                ? "Max F1, cap 30%"
                : value,
      })),
    [evaluationAlertLevels],
  );
  const evaluationHorizonOptions = useMemo(
    () =>
      evaluationHorizons.map((horizon) => ({
        value: String(horizon),
        label: horizon === 1 ? "1 week" : `${horizon} weeks`,
        detail: `${horizon}w`,
      })),
    [evaluationHorizons],
  );
  const bandCounts = useMemo(() => scoreBands(workspacePoints), [workspacePoints]);
  const unclusteredVisibleCount = useMemo(
    () => workspacePoints.filter((point) => point.clusterContext?.clusterQuality !== "clustered").length,
    [workspacePoints],
  );
  const hotspotCellsAll = useMemo(
    () => {
      if (!shouldComputeHotspots) {
        return [] as HotspotCellSummary[];
      }

      return buildHotspotCells(filteredPoints, {
        hasContextHit: (point) =>
          Boolean(point.reviewContext && hasCorroboratingContext(point.reviewContext)),
      });
    },
    [filteredPoints, shouldComputeHotspots],
  );
  const apiGridHotspotCells = useMemo(
    () => {
      if (
        hotspotGrainFocus !== "grid" ||
        hotspotSummaryStatus !== "ready" ||
        hotspotSummary?.grain !== "grid"
      ) {
        return [] as HotspotCellSummary[];
      }

      return hotspotSummary.hotspots.map(hotspotCellFromSummaryUnit);
    },
    [hotspotGrainFocus, hotspotSummary, hotspotSummaryStatus],
  );
  const hotspotCells = useMemo(
    () => {
      if (!shouldComputeHotspots) {
        return [] as HotspotCellSummary[];
      }

      if (hotspotSourceFocus !== null && hotspotGrainFocus === "grid") {
        return apiGridHotspotCells;
      }

      if (hotspotGrainFocus === "admin_area") {
        return [] as HotspotCellSummary[];
      }

      return focusHotspotCells(hotspotCellsAll, hotspotSourceFocus);
    },
    [
      apiGridHotspotCells,
      hotspotCellsAll,
      hotspotGrainFocus,
      hotspotSourceFocus,
      shouldComputeHotspots,
    ],
  );
  const adminHotspotUnits = useMemo(
    () =>
      hotspotGrainFocus === "admin_area" && hotspotSummary?.grain === "admin_area"
        ? hotspotSummary.hotspots
        : [],
    [hotspotGrainFocus, hotspotSummary],
  );
  const hotspotPointsByCell = useMemo(() => {
    if (!shouldComputeHotspots) {
      return new Map<string, AlertVisualPoint[]>();
    }

    const grouped = new Map<string, AlertVisualPoint[]>();

    for (const point of sortedPoints) {
      if (!point.gridCellId) continue;
      const current = grouped.get(point.gridCellId) || [];
      current.push(point);
      grouped.set(point.gridCellId, current);
    }

    return grouped;
  }, [shouldComputeHotspots, sortedPoints]);
  const hotspotCellById = useMemo(
    () => new Map(hotspotCells.map((cell) => [cell.gridCellId, cell])),
    [hotspotCells],
  );
  const gridCellById = useMemo(
    () => new Map(gridCells.map((cell) => [cell.gridCellId, cell])),
    [gridCells],
  );
  const hotspotRankById = useMemo(
    () => new Map(hotspotCells.map((cell, index) => [cell.gridCellId, index + 1])),
    [hotspotCells],
  );
  const openHotspotDetail = openHotspotDetailId ? hotspotCellById.get(openHotspotDetailId) ?? null : null;
  const openHotspotDetailGrid = openHotspotDetailId ? gridCellById.get(openHotspotDetailId) ?? null : null;
  const openHotspotDetailPoints = useMemo(
    () => (openHotspotDetailId ? hotspotPointsByCell.get(openHotspotDetailId) ?? [] : []),
    [openHotspotDetailId, hotspotPointsByCell],
  );
  const hotspotFilterCell = hotspotFilterId ? hotspotCellById.get(hotspotFilterId) ?? null : null;
  const anomalyPoints = useMemo(
    () => {
      let next = sortedPoints;

      if (data.filters.viewMode !== "cluster" && focusedBand) {
        next = next.filter((point) => alertBand(point.alertScore) === focusedBand);
      }

      if (hotspotFilterId) {
        next = next.filter((point) => point.gridCellId === hotspotFilterId);
      }

      return next;
    },
    [sortedPoints, data.filters.viewMode, focusedBand, hotspotFilterId],
  );
  const anomalyRailResetKey = useMemo(
    () =>
      [
        data.filters.species,
        data.filters.backend,
        data.filters.dateFrom,
        data.filters.dateTo,
        data.filters.alertLevel,
        data.filters.minConfidence,
        data.filters.minStationQuality,
        data.filters.viewMode,
        data.filters.clusterScope,
        data.filters.clusterKey,
        data.filters.sort,
        hotspotFilterCell?.gridCellId ?? "",
        focusedBand ?? "",
      ].join("|"),
    [
      data.filters.alertLevel,
      data.filters.backend,
      data.filters.clusterKey,
      data.filters.clusterScope,
      data.filters.dateFrom,
      data.filters.dateTo,
      data.filters.minConfidence,
      data.filters.minStationQuality,
      data.filters.sort,
      data.filters.species,
      data.filters.viewMode,
      focusedBand,
      hotspotFilterCell?.gridCellId,
    ],
  );
  const visibleAnomalyPoints = useMemo(
    () => anomalyPoints.slice(0, anomalyRailVisibleCount),
    [anomalyPoints, anomalyRailVisibleCount],
  );
  const hasMoreAnomalyPoints = visibleAnomalyPoints.length < anomalyPoints.length;
  useEffect(() => {
    setAnomalyRailVisibleCount(
      Math.min(anomalyPoints.length, ANOMALY_RAIL_INITIAL_COUNT),
    );
  }, [anomalyPoints.length, anomalyRailResetKey]);
  useEffect(() => {
    if (!selectedId) {
      return;
    }

    const selectedAnomalyIndex = anomalyPoints.findIndex(
      (point) => point.id === selectedId,
    );
    if (
      selectedAnomalyIndex < 0 ||
      selectedAnomalyIndex < anomalyRailVisibleCount
    ) {
      return;
    }

    setAnomalyRailVisibleCount(
      Math.min(
        anomalyPoints.length,
        Math.ceil((selectedAnomalyIndex + 1) / ANOMALY_RAIL_BATCH_SIZE) *
          ANOMALY_RAIL_BATCH_SIZE,
      ),
    );
  }, [anomalyPoints, anomalyRailVisibleCount, selectedId]);
  const hotspotLeaderboardCells = useMemo(() => {
    return hotspotCells;
  }, [hotspotCells]);
  const hotspotSourceCounts = useMemo(
    () => {
      if (!shouldComputeHotspots) {
        return {
          bioacoustic: 0,
        };
      }

      return {
        bioacoustic: hotspotCellsAll.filter((cell) => cell.overlapSignals.includes("bioacoustic")).length,
      };
    },
    [hotspotCellsAll, shouldComputeHotspots],
  );
  const activeHotspotSourceCount = useMemo(() => {
    if (
      hotspotSourceFocus !== null &&
      hotspotSummaryStatus === "ready" &&
      hotspotSummary?.source === hotspotSourceFocus &&
      hotspotSummary.grain === hotspotGrainFocus
    ) {
      return hotspotSummary.coverage.totalRows;
    }
    if (hotspotSourceFocus === "bioacoustic") return hotspotSourceCounts.bioacoustic;
    return hotspotCellsAll.length;
  }, [
    hotspotCellsAll.length,
    hotspotGrainFocus,
    hotspotSourceCounts,
    hotspotSourceFocus,
    hotspotSummary,
    hotspotSummaryStatus,
  ]);
  const focusedBandLabel = focusedBand ? `${focusedBand.slice(0, 1).toUpperCase()}${focusedBand.slice(1)}` : null;
  const focusedBandTone = focusedBand
    ? scoreColor(focusedBand === "high" ? 0.6 : focusedBand === "moderate" ? 0.4 : 0.15)
    : null;
  const legendClusterGroups = useMemo(
    () => visibleClusterGroups.slice(0, 4),
    [visibleClusterGroups],
  );
  const comparisonAnchor = useDeferredValue(selectionContextPoint);
  const baselinePoints = useMemo(() => {
    if (!comparisonAnchor) return [];

    const anchorIndexed = referencePointIndex.byId.get(comparisonAnchor.id);
    const anchorMinutes = anchorIndexed?.minuteOfDay ?? minutesOfDay(comparisonAnchor.detectedAt);
    const speciesBackendKey = speciesBackendKeyForPoint(comparisonAnchor);
    const sameSpeciesAudio =
      referencePointIndex.bySpeciesBackend.get(speciesBackendKey)?.filter(
        (candidate) => candidate.point.id !== comparisonAnchor.id && candidate.hasAudio,
      ) ?? [];

    if (sameSpeciesAudio.length === 0) return [];

    const weeklyStart = comparisonAnchor.reviewContext?.weeklyPeriodStart;
    const weeklyEnd = comparisonAnchor.reviewContext?.weeklyPeriodEnd;
    const weeklyWindowPoints =
      weeklyStart && weeklyEnd
        ? (() => {
            const windowStart = Date.parse(`${weeklyStart}T00:00:00Z`);
            const windowEnd = Date.parse(`${weeklyEnd}T23:59:59Z`);
            if (!Number.isFinite(windowStart) || !Number.isFinite(windowEnd)) {
              return sameSpeciesAudio;
            }

            return sameSpeciesAudio.filter(
              (candidate) =>
                candidate.detectedAtMs >= windowStart && candidate.detectedAtMs <= windowEnd,
            );
          })()
        : sameSpeciesAudio;
    const windowPool = weeklyWindowPoints.length > 0 ? weeklyWindowPoints : sameSpeciesAudio;
    const typicalPool = windowPool.filter((candidate) => candidate.point.alertScore <= 0.2);
    const candidatePool = typicalPool.length > 0 ? typicalPool : windowPool;

    return [...candidatePool]
      .sort((left, right) => {
        const leftHourDistance = circularMinutesDistance(left.minuteOfDay, anchorMinutes);
        const rightHourDistance = circularMinutesDistance(right.minuteOfDay, anchorMinutes);
        const leftStationPenalty = left.point.station === comparisonAnchor.station ? 0 : 1;
        const rightStationPenalty = right.point.station === comparisonAnchor.station ? 0 : 1;

        return left.point.alertScore - right.point.alertScore ||
          leftHourDistance - rightHourDistance ||
          leftStationPenalty - rightStationPenalty ||
          right.detectedAtMs - left.detectedAtMs;
      })
      .slice(0, 3)
      .map((candidate) => candidate.point);
  }, [comparisonAnchor, referencePointIndex]);
  const baselinePointIds = useMemo(
    () => new Set(baselinePoints.map((point) => point.id)),
    [baselinePoints],
  );
  const comparisonTargets = useMemo(() => {
    if (!comparisonAnchor || modalActiveTab !== "compare") {
      return [] as AlertComparisonTarget[];
    }

    const anchorIndexed = referencePointIndex.byId.get(comparisonAnchor.id);
    const anchorDetectedAtMs =
      anchorIndexed?.detectedAtMs ??
      (Number.isFinite(Date.parse(comparisonAnchor.detectedAt))
        ? Date.parse(comparisonAnchor.detectedAt)
        : 0);
    const distanceFromAnchor = (candidate: IndexedAlertPoint): number => {
      const embeddingDistance = Math.hypot(
        comparisonAnchor.x - candidate.point.x,
        comparisonAnchor.y - candidate.point.y,
      );
      const timeDeltaHours = Math.abs(anchorDetectedAtMs - candidate.detectedAtMs) / 3_600_000;
      const stationPenalty = comparisonAnchor.station === candidate.point.station ? 0 : 0.18;

      return embeddingDistance + Math.min(timeDeltaHours / 72, 1.2) + stationPenalty;
    };
    const referencePool =
      referencePointIndex.byBackend
        .get(comparisonAnchor.embeddingBackend)
        ?.filter((candidate) => candidate.point.id !== comparisonAnchor.id) ?? [];
    const sameSpeciesPool =
      referencePointIndex.bySpeciesBackend
        .get(speciesBackendKeyForPoint(comparisonAnchor))
        ?.filter((candidate) => candidate.point.id !== comparisonAnchor.id) ?? [];
    const sameSpeciesAudioPool = sameSpeciesPool.filter((candidate) => candidate.hasAudio);
    const usedIds = new Set<string>();
    const reservePoint = (candidates: IndexedAlertPoint[]) => {
      const next = candidates.find((candidate) => !usedIds.has(candidate.point.id)) ?? null;
      if (next) usedIds.add(next.point.id);
      return next?.point ?? null;
    };

    const clusterCandidates = (() => {
      const clusterKey = persistedClusterKeyForPoint(comparisonAnchor);
      if (!clusterKey) {
        return [] as IndexedAlertPoint[];
      }

      return [...(referencePointIndex.byPersistedCluster.get(clusterKey) ?? [])]
        .filter((candidate) => candidate.point.id !== comparisonAnchor.id)
        .sort((left, right) => {
          const leftIsExemplar = Number(Boolean(left.point.clusterContext?.isExemplar));
          const rightIsExemplar = Number(Boolean(right.point.clusterContext?.isExemplar));
          return rightIsExemplar - leftIsExemplar ||
            (left.point.clusterContext?.centroidDistance ?? Number.POSITIVE_INFINITY) -
              (right.point.clusterContext?.centroidDistance ?? Number.POSITIVE_INFINITY) ||
            distanceFromAnchor(left) - distanceFromAnchor(right);
        });
    })();

    const nearestTypicalCandidates = (() => {
      const typicalPool = sameSpeciesAudioPool.filter((candidate) => candidate.point.alertScore <= 0.25);
      const baselineIndexed = baselinePoints
        .map((point) => referencePointIndex.byId.get(point.id) ?? null)
        .filter((candidate): candidate is IndexedAlertPoint => candidate !== null);
      const candidatePool = typicalPool.length > 0
        ? typicalPool
        : sameSpeciesAudioPool.length > 0
          ? sameSpeciesAudioPool
          : baselineIndexed;

      return [...candidatePool].sort((left, right) =>
        distanceFromAnchor(left) - distanceFromAnchor(right) ||
        left.point.alertScore - right.point.alertScore ||
        right.detectedAtMs - left.detectedAtMs,
      );
    })();

    const priorWeekCandidates = (() => {
      const anchorWeek = anchorIndexed?.weekStart ?? pointWeeklyPeriodStart(comparisonAnchor);
      if (!anchorWeek) return [] as IndexedAlertPoint[];

      return [...sameSpeciesPool]
        .filter((candidate) => {
          const candidateWeek = candidate.weekStart;
          return Boolean(candidateWeek && candidateWeek < anchorWeek);
        })
        .sort((left, right) => {
          const leftWeek = left.weekStart ?? "";
          const rightWeek = right.weekStart ?? "";
          return rightWeek.localeCompare(leftWeek) ||
            Number(Boolean(right.point.clusterContext?.isExemplar)) - Number(Boolean(left.point.clusterContext?.isExemplar)) ||
            left.point.alertScore - right.point.alertScore ||
            distanceFromAnchor(left) - distanceFromAnchor(right);
        });
    })();

    return [
      {
        key: "cluster-anchor",
        title: comparisonAnchor.clusterContext?.isExemplar ? "Cluster sibling" : "Cluster anchor",
        description: comparisonAnchor.clusterContext?.isExemplar
          ? "The focal clip is the exemplar, so this shows the closest sibling inside the same persisted cluster."
          : "Representative clip from the same persisted cluster to compare timbre and surroundings.",
        point: reservePoint(clusterCandidates),
        emptyText: "No same-cluster comparison clip is loaded in the current workspace slice.",
      },
      {
        key: "nearest-typical",
        title: "Nearest typical clip",
        description: "Closest low-isolation same-species clip in this embedding space, weighted toward the same station and hour.",
        point: reservePoint(nearestTypicalCandidates),
        emptyText: "No typical same-species clip with audio is loaded in the current window.",
      },
      {
        key: "prior-week-reference",
        title: "Prior-week reference",
        description: "Most recent earlier weekly reference for the same species and embedding backend.",
        point: reservePoint(priorWeekCandidates),
        emptyText: "No earlier same-species weekly reference is available in the current window.",
      },
    ] satisfies AlertComparisonTarget[];
  }, [baselinePoints, comparisonAnchor, modalActiveTab, referencePointIndex]);

  useEffect(() => {
    setDraftFilters(data.filters);
  }, [data.filters]);

  useEffect(() => {
    if (copyState === "idle") return undefined;

    const timeout = window.setTimeout(() => setCopyState("idle"), 1600);
    return () => window.clearTimeout(timeout);
  }, [copyState]);

  useEffect(() => {
    if (selectedId && !sortedWorkspacePoints.some((point) => point.id === selectedId)) {
      setSelectedId(null);
      setSelectedModalPoint(null);
    }
  }, [selectedId, sortedWorkspacePointMeta, sortedWorkspacePoints]);

  useEffect(() => {
    if (activeMonitorTab !== "hotspot" || gridCellsStatus === "ready") return undefined;

    let cancelled = false;
    if (gridCellsStatus !== "loading") {
      setGridCellsStatus("loading");
    }

    fetch("/api/grid-cells?include_counts=false")
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Grid cells request failed with ${response.status}`);
        }

        return response.json() as Promise<{
          source?: string;
          cells?: Array<{
            grid_cell_id?: string;
            bbox_min_lat?: number;
            bbox_min_lon?: number;
            bbox_max_lat?: number;
            bbox_max_lon?: number;
            cell_geojson?: unknown;
            station_count?: number;
            total_detections?: number;
          }>;
        }>;
      })
      .then((payload) => {
        if (cancelled) return;

        const nextCells = (payload.cells ?? [])
          .map((cell) => ({
            gridCellId: String(cell.grid_cell_id ?? "").trim(),
            bboxMinLat: Number(cell.bbox_min_lat),
            bboxMinLon: Number(cell.bbox_min_lon),
            bboxMaxLat: Number(cell.bbox_max_lat),
            bboxMaxLon: Number(cell.bbox_max_lon),
            cellGeojson: cell.cell_geojson ?? null,
            stationCount: Number(cell.station_count) || 0,
            totalDetections: Number(cell.total_detections) || 0,
          }))
          .filter(
            (cell) =>
              Boolean(cell.gridCellId) &&
              Number.isFinite(cell.bboxMinLat) &&
              Number.isFinite(cell.bboxMinLon) &&
              Number.isFinite(cell.bboxMaxLat) &&
              Number.isFinite(cell.bboxMaxLon),
          );

        setGridCells(nextCells);
        setGridCellsStatus("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setGridCellsStatus("error");
      });

    return () => {
      cancelled = true;
    };
  }, [activeMonitorTab]);

  useEffect(() => {
    if (!shouldComputeHotspots) {
      setHotspotSummary(null);
      setHotspotSummaryStatus("idle");
      return undefined;
    }

    const source = hotspotSourceFocus ?? "bioacoustic";
    const controller = new AbortController();
    const params = new URLSearchParams({
      source,
      grain: hotspotGrainFocus,
    });
    if (data.filters.dateFrom) {
      params.set("dateFrom", data.filters.dateFrom);
    }
    if (data.filters.dateTo) {
      params.set("dateTo", data.filters.dateTo);
    }

    setHotspotSummaryStatus("loading");
    fetch(`/api/alerts/hotspots?${params.toString()}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Hotspot request failed with ${response.status}`);
        }

        return response.json() as Promise<HotspotSummaryResponse>;
      })
      .then((payload) => {
        setHotspotSummary(payload);
        setHotspotSummaryStatus("ready");
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setHotspotSummary(null);
        setHotspotSummaryStatus("error");
      });

    return () => controller.abort();
  }, [
    data.filters.dateFrom,
    data.filters.dateTo,
    hotspotGrainFocus,
    hotspotSourceFocus,
    shouldComputeHotspots,
  ]);

  useEffect(() => {
    if (
      hotspotSourceFocus !== null &&
      hotspotSummaryStatus === "ready" &&
      activeHotspotSourceCount === 0
    ) {
      setHotspotSourceFocus(null);
    }
  }, [activeHotspotSourceCount, hotspotSourceFocus, hotspotSummaryStatus]);

  useEffect(() => {
    const nextHotspotId = hotspotCells[0]?.gridCellId ?? null;

    if (!nextHotspotId) {
      if (selectedHotspotId !== null) {
        setSelectedHotspotId(null);
      }
      return;
    }

    if (!selectedHotspotId || !hotspotCellById.has(selectedHotspotId)) {
      setSelectedHotspotId(nextHotspotId);
    }
  }, [hotspotCells, hotspotCellById, selectedHotspotId]);

  useEffect(() => {
    if (hotspotFilterId && !hotspotCellById.has(hotspotFilterId)) {
      setHotspotFilterId(null);
    }
  }, [hotspotCellById, hotspotFilterId]);

  useEffect(() => {
    if (openHotspotDetailId && !hotspotCellById.has(openHotspotDetailId)) {
      setOpenHotspotDetailId(null);
    }
  }, [hotspotCellById, openHotspotDetailId]);

  useEffect(() => {
    if (activeMonitorTab !== "embeddings" || !selectedId) return undefined;

    const frame = window.requestAnimationFrame(() => {
      anomalyItemRefs.current[selectedId]?.scrollIntoView({ block: "nearest" });
    });

    return () => window.cancelAnimationFrame(frame);
  }, [activeMonitorTab, anomalyPoints, selectedId]);

  useEffect(() => {
    if (activeMonitorTab !== "hotspot" || !selectedHotspotId) return undefined;

    const frame = window.requestAnimationFrame(() => {
      hotspotRowRefs.current[selectedHotspotId]?.scrollIntoView({ block: "nearest" });
    });

    return () => window.cancelAnimationFrame(frame);
  }, [activeMonitorTab, hotspotLeaderboardCells, selectedHotspotId]);

  const openSelectedPoint = useCallback(
    (pointId: string) => {
      const nextPoint =
        sortedWorkspacePointMeta.get(pointId)?.point ??
        referencePointIndex.byId.get(pointId)?.point ??
        null;
      if (!nextPoint) return;

      flushSync(() => {
        setSelectedModalPoint(nextPoint);
        setModalActiveTab("overview");
      });
      warmPointMedia(nextPoint);
      primePointDetail(pointId, nextPoint);

      if (pendingSelectionFrameRef.current !== null) {
        window.cancelAnimationFrame(pendingSelectionFrameRef.current);
      }
      pendingSelectionFrameRef.current = window.requestAnimationFrame(() => {
        pendingSelectionFrameRef.current = null;
        startUiTransition(() => {
          setSelectedId(pointId);
        });
      });
    },
    [primePointDetail, referencePointIndex, sortedWorkspacePointMeta, warmPointMedia],
  );
  const prewarmHoveredPoint = useCallback(
    (pointId: string | null) => {
      if (!pointId) {
        return;
      }

      const hoveredPoint =
        sortedWorkspacePointMeta.get(pointId)?.point ??
        referencePointIndex.byId.get(pointId)?.point ??
        null;

      if (!hoveredPoint) {
        return;
      }

      warmPointMedia(hoveredPoint);
      primePointDetail(pointId, hoveredPoint);
    },
    [primePointDetail, referencePointIndex, sortedWorkspacePointMeta, warmPointMedia],
  );

  const closeSelectedPoint = () => {
    if (pendingSelectionFrameRef.current !== null) {
      window.cancelAnimationFrame(pendingSelectionFrameRef.current);
      pendingSelectionFrameRef.current = null;
    }
    setModalActiveTab("overview");
    setSelectedModalPoint(null);
    setSelectedId(null);
  };

  useEffect(() => {
    return () => {
      if (pendingSelectionFrameRef.current !== null) {
        window.cancelAnimationFrame(pendingSelectionFrameRef.current);
        pendingSelectionFrameRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (!modalPoint) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeSelectedPoint();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        openSelectedPoint(
          sortedWorkspacePoints[(modalIndex + 1) % sortedWorkspacePoints.length]?.id ?? "",
        );
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        openSelectedPoint(
          sortedWorkspacePoints[
            (modalIndex - 1 + sortedWorkspacePoints.length) % sortedWorkspacePoints.length
          ]?.id ?? "",
        );
      }
    };

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [modalIndex, modalPoint, sortedWorkspacePoints]);

  const applyFilters = () => {
    const query = buildSearchParams(draftFilters);
    startRouteTransition(() => {
      router.replace(query ? `${pathname}?${query}` : pathname);
    });
  };

  const resetFilters = () => {
    setDraftFilters(DEFAULT_FILTERS);
    startRouteTransition(() => {
      router.replace(pathname);
    });
  };

  const applyEvaluationFilters = () => {
    const nextAnomalyType = evaluationAnomalyTypes.includes(draftEvaluationAnomalyType)
      ? draftEvaluationAnomalyType
      : activeEvaluationAnomalyType;
    const nextAlertLevel = evaluationAlertLevels.includes(draftEvaluationAlertLevel)
      ? draftEvaluationAlertLevel
      : activeEvaluationAlertLevel;
    const nextHorizon = evaluationHorizons.includes(draftEvaluationHorizon)
      ? draftEvaluationHorizon
      : activeEvaluationHorizon;

    setSelectedEvaluationAnomalyType(nextAnomalyType);
    setSelectedEvaluationAlertLevel(nextAlertLevel);
    setSelectedEvaluationHorizon(nextHorizon);
  };

  const resetEvaluationFilters = () => {
    const defaultAnomalyType = evaluationAnomalyTypes.includes(DEFAULT_EVALUATION_ANOMALY_TYPE)
      ? DEFAULT_EVALUATION_ANOMALY_TYPE
      : (evaluationAnomalyTypes[0] ?? DEFAULT_EVALUATION_ANOMALY_TYPE);
    const defaultAlertLevel = evaluationAlertLevels.includes(DEFAULT_EVALUATION_ALERT_LEVEL)
      ? DEFAULT_EVALUATION_ALERT_LEVEL
      : (evaluationAlertLevels[0] ?? DEFAULT_EVALUATION_ALERT_LEVEL);
    const defaultHorizon = evaluationHorizons.includes(3)
      ? 3
      : (evaluationHorizons[0] ?? 0);

    setSelectedEvaluationAnomalyType(defaultAnomalyType);
    setDraftEvaluationAnomalyType(defaultAnomalyType);
    setSelectedEvaluationAlertLevel(defaultAlertLevel);
    setDraftEvaluationAlertLevel(defaultAlertLevel);
    setSelectedEvaluationHorizon(defaultHorizon);
    setDraftEvaluationHorizon(defaultHorizon);
  };

  const shiftSelection = (step: number) => {
    if (!sortedWorkspacePoints.length) return;

    const baseIndex = modalIndex >= 0 ? modalIndex : 0;
    const nextIndex =
      (baseIndex + step + sortedWorkspacePoints.length) % sortedWorkspacePoints.length;
    openSelectedPoint(sortedWorkspacePoints[nextIndex]?.id ?? "");
  };

  const copyCurrentView = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
  };

  const openHotspotCell = (gridCellId: string) => {
    setSelectedHotspotId(gridCellId);
    setOpenHotspotDetailId(gridCellId);
  };

  const registerAnomalyItemRef = useCallback((pointId: string, node: HTMLButtonElement | null) => {
    anomalyItemRefs.current[pointId] = node;
  }, []);

  const clearHotspotFilter = () => {
    setHotspotFilterId(null);
  };

  const emptyStateMessage =
    data.stats.matchedRows === 0
      ? "No embedding-backed detections match the current filters."
      : data.stats.scoredPoints === 0
        ? "Matching detections exist, but no consistent embedding dimension could be scored for this slice."
        : "Matching detections were loaded, but the current anomaly tier hides every scored clip.";

    const monitorPanelHeight = "755px";

    const filterRail = (
      <aside
        className="monitor-filter-panel flex h-full min-h-0 flex-col rounded-2xl p-3"
        style={{
          height: monitorPanelHeight,
          background: NEUTRAL_SURFACE.panel,
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          borderTopColor: "transparent",
        }}
      >
        <PanelTitle icon={Filter}>
          Filters
        </PanelTitle>

        <div className="sb-scroll flex-1 overflow-y-auto pr-1">
          <div className="space-y-2">
          <FieldShell>
            <CompactDropdown
              label="Species"
              valueLabel={draftFilters.species}
              placeholder="Any species"
              options={speciesOptions}
              searchable
              searchValue={draftFilters.species}
              searchPlaceholder="Search species"
              onSearchChange={(species) => setDraftFilters((current) => ({ ...current, species }))}
              onSelect={(species) => setDraftFilters((current) => ({ ...current, species }))}
              onClear={() => setDraftFilters((current) => ({ ...current, species: "" }))}
              emptyText="No matching species in the current workspace slice."
            />
          </FieldShell>


          <FieldShell>
            <CompactDropdown
              label="Embedding model"
              valueLabel={BACKEND_OPTIONS.find((option) => option.value === draftFilters.backend)?.label ?? ""}
              placeholder="Select embedding model"
              options={BACKEND_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
              onSelect={(backend) => setDraftFilters((current) => ({ ...current, backend: backend as AlertsPageFilters["backend"] }))}
              onClear={() => setDraftFilters((current) => ({ ...current, backend: DEFAULT_FILTERS.backend }))}
            />
          </FieldShell>

          <FieldShell>
            <CompactDropdown
              label="Week"
              valueLabel={selectedWeekLabel}
              placeholder={modeledWeeks.length > 0 ? "Select a modeled week" : "No modeled weeks"}
              options={weekOptions}
              onSelect={(periodStart) =>
                setDraftFilters((current) => ({
                  ...current,
                  ...resolveWeekRangeFromWeek(periodStart, modeledWeeks),
                }))
              }
              onClear={() => setDraftFilters((current) => ({ ...current, dateFrom: "", dateTo: "" }))}
              emptyText="No modeled weekly artifacts are available right now."
            />
          </FieldShell>


          <FieldShell>
            <CompactDropdown
              label="Anomaly tier"
              valueLabel={ALERT_LEVEL_OPTIONS.find((option) => option.value === draftFilters.alertLevel)?.label ?? ""}
              placeholder="Select anomaly tier"
              options={ALERT_LEVEL_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
              onSelect={(alertLevel) => setDraftFilters((current) => ({ ...current, alertLevel: alertLevel as AlertsPageFilters["alertLevel"] }))}
              onClear={() => setDraftFilters((current) => ({ ...current, alertLevel: DEFAULT_FILTERS.alertLevel }))}
            />
          </FieldShell>

          <FieldShell>
            <CompactDropdown
              label="Min confidence"
              valueLabel={CONFIDENCE_OPTIONS.find((option) => option.value === draftFilters.minConfidence)?.label ?? ""}
              placeholder="Any confidence"
              options={CONFIDENCE_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
              onSelect={(minConfidence) => setDraftFilters((current) => ({ ...current, minConfidence }))}
              onClear={() => setDraftFilters((current) => ({ ...current, minConfidence: "" }))}
            />
          </FieldShell>

          <FieldShell>
            <CompactDropdown
              label="Min station quality"
              valueLabel={
                STATION_QUALITY_OPTIONS.find(
                  (option) => option.value === draftFilters.minStationQuality,
                )?.label ?? ""
              }
              placeholder="Any lifetime availability"
              options={STATION_QUALITY_OPTIONS}
              onSelect={(minStationQuality) =>
                setDraftFilters((current) => ({ ...current, minStationQuality }))
              }
              onClear={() =>
                setDraftFilters((current) => ({ ...current, minStationQuality: "" }))
              }
            />
          </FieldShell>
          </div>
        </div>

        <div
          className="grid grid-cols-2 gap-2 pt-3"
          style={{
            borderTop: `1px solid ${NEUTRAL_SURFACE.borderSoft}`,
          }}
        >
          <button
            type="button"
            onClick={applyFilters}
            className="inline-flex items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-semibold"
            style={{
              background: NEUTRAL_SURFACE.panelAlt,
              border: `1px solid ${NEUTRAL_SURFACE.border}`,
              color: isPending ? "rgba(255,255,255,0.96)" : "rgba(255,255,255,0.84)",
            }}
          >
            {isPending ? "Refreshing…" : "Apply"}
          </button>
          <button
            type="button"
            onClick={resetFilters}
            className="inline-flex w-full items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-medium"
            style={{
              background: NEUTRAL_SURFACE.panelAlt,
              border: `1px solid ${NEUTRAL_SURFACE.border}`,
              color: "rgba(255,255,255,0.68)",
            }}
          >
            <RotateCcw size={13} />
            Reset
          </button>
        </div>
      </aside>
    );

    const anomalyRail = (
      <div
        className="monitor-secondary-panel flex h-full min-h-0 flex-col overflow-hidden rounded-2xl"
        style={{
          height: monitorPanelHeight,
          background: NEUTRAL_SURFACE.panel,
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          borderTopColor: "transparent",
        }}
      >
        <div className="border-b px-3 py-3" style={{ borderColor: NEUTRAL_SURFACE.borderSoft }}>
          <PanelTitle icon={SlidersHorizontal}>
            Top anomalies
          </PanelTitle>
          <div className="flex flex-wrap gap-1.5 text-[10px]" style={{ color: "rgba(255,255,255,0.56)" }}>
            {hotspotFilterCell ? (
              <>
                <span
                  className="rounded-full px-2 py-1"
                  style={{
                    background: withAlpha(hotspotToneColor(hotspotFilterCell.displayScore), 0.12),
                    border: `1px solid ${withAlpha(hotspotToneColor(hotspotFilterCell.displayScore), 0.22)}`,
                    color: withAlpha(hotspotToneColor(hotspotFilterCell.displayScore), 0.92),
                  }}
                >
                  {hotspotFilterCell.gridCellId}
                </span>
                <button
                  type="button"
                  onClick={clearHotspotFilter}
                  className="rounded-full px-2 py-1"
                  style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.72)" }}
                >
                  Clear
                </button>
              </>
            ) : null}
            {data.filters.viewMode !== "cluster" && focusedBandLabel && focusedBandTone ? (
              <>
                <span
                  className="rounded-full px-2 py-1"
                  style={{
                    background: withAlpha(focusedBandTone, 0.12),
                    border: `1px solid ${withAlpha(focusedBandTone, 0.22)}`,
                    color: withAlpha(focusedBandTone, 0.92),
                  }}
                >
                  {focusedBandLabel}
                </span>
                <button
                  type="button"
                  onClick={() => setFocusedBand(null)}
                  className="rounded-full px-2 py-1"
                  style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.72)" }}
                >
                  Clear
                </button>
              </>
            ) : null}
            <span>
              {visibleAnomalyPoints.length.toLocaleString()} of {anomalyPoints.length.toLocaleString()} rendered
            </span>
            <span>·</span>
            <span>Highest isolation</span>
          </div>
        </div>

        <div className="sb-scroll flex-1 overflow-y-auto p-3">
          {anomalyPoints.length === 0 ? (
            <div className="rounded-xl px-3 py-4 text-sm" style={{ color: "rgba(255,255,255,0.46)" }}>
              {hotspotFilterCell
                ? `No detections remain inside ${hotspotFilterCell.gridCellId}.`
                : "No detections are available in the current slice."}
            </div>
          ) : (
            <div className="space-y-2">
              {visibleAnomalyPoints.map((point, index) => (
                <AnomalyListItem
                  key={point.id}
                  point={point}
                  index={index}
                  isActive={point.id === selectedId}
                  isBaselineReference={baselinePointIds.has(point.id)}
                  viewMode={data.filters.viewMode}
                  clusterColorByKey={clusterColorByKey}
                  onOpen={openSelectedPoint}
                  onPrewarm={prewarmHoveredPoint}
                  registerRef={registerAnomalyItemRef}
                />
              ))}
              {hasMoreAnomalyPoints ? (
                <button
                  type="button"
                  onClick={() =>
                    setAnomalyRailVisibleCount((current) =>
                      Math.min(
                        anomalyPoints.length,
                        current + ANOMALY_RAIL_BATCH_SIZE,
                      ),
                    )
                  }
                  className="w-full rounded-xl px-3 py-2.5 text-sm font-medium transition-colors"
                  style={{
                    background: NEUTRAL_SURFACE.panelAlt,
                    border: `1px solid ${NEUTRAL_SURFACE.border}`,
                    color: "rgba(255,255,255,0.72)",
                  }}
                >
                  Render next {Math.min(ANOMALY_RAIL_BATCH_SIZE, anomalyPoints.length - visibleAnomalyPoints.length).toLocaleString()} anomalies
                </button>
              ) : null}
            </div>
          )}
        </div>
      </div>
    );

    const hotspotLeaderboardRail = (
      <div
        className="monitor-secondary-panel flex h-full min-h-0 flex-col overflow-hidden rounded-2xl"
        style={{
          height: monitorPanelHeight,
          background: NEUTRAL_SURFACE.panel,
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          borderTopColor: "transparent",
        }}
      >
        <div className="border-b px-3 py-3" style={{ borderColor: NEUTRAL_SURFACE.borderSoft }}>
          <PanelTitle icon={MapPinned}>
            {hotspotGrainFocus === "admin_area" ? "Top units" : "Top grids"}
          </PanelTitle>
          <div className="flex flex-wrap gap-1.5 text-[10px]" style={{ color: "rgba(255,255,255,0.56)" }}>
            <span>
              {hotspotGrainFocus === "admin_area"
                ? `${adminHotspotUnits.length.toLocaleString()} units`
                : `${hotspotCells.length.toLocaleString()} cells`}
            </span>
            <span>·</span>
            <span>Sorted by {hotspotDisplayLabel(hotspotSourceFocus).toLowerCase()}</span>
            {hotspotGrainFocus === "admin_area" && hotspotSummary ? (
              <>
                <span>·</span>
                <span>
                  {hotspotSummary.coverage.renderableUnits.toLocaleString()} map-ready
                </span>
              </>
            ) : null}
          </div>
        </div>

        <div className="sb-scroll flex-1 overflow-y-auto p-3">
          {hotspotGrainFocus === "admin_area" ? (
            adminHotspotUnits.length > 0 ? (
              <div className="space-y-1.5">
                {adminHotspotUnits.map((unit, index) => {
                  const cellColor = hotspotToneColor(unit.score);
                  const value = hotspotUnitPrimaryValue(unit);

                  return (
                    <div
                      key={`${unit.location_id}-${unit.period_start}`}
                      className="w-full rounded-xl px-2.5 py-2 text-left"
                      style={{
                        background: NEUTRAL_SURFACE.panelAlt,
                        border: `1px solid ${NEUTRAL_SURFACE.border}`,
                      }}
                    >
                      <div className="grid grid-cols-[28px_minmax(0,1fr)] items-start gap-2.5">
                        <div
                          className="flex h-7 w-7 items-center justify-center rounded-full font-mono text-[10px]"
                          style={{
                            background: withAlpha(cellColor, 0.14),
                            border: `1px solid ${withAlpha(cellColor, 0.24)}`,
                            color: withAlpha(cellColor, 0.94),
                          }}
                        >
                          {index + 1}
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-start justify-between gap-2">
                            <div className="min-w-0 truncate text-[12px] font-medium" style={{ color: "rgba(255,255,255,0.84)" }}>
                              {unit.location_name}
                            </div>
                            <div className="shrink-0 font-mono text-[10px]" style={{ color: withAlpha(cellColor, 0.92) }}>
                              {value}
                            </div>
                          </div>
                          <div
                            className="mt-1 whitespace-nowrap font-mono text-[10px] tracking-[0.02em]"
                            style={{ color: "rgba(130,130,130,0.92)" }}
                            title={unit.renderable ? "Municipality boundary match available" : "City fallback has no map boundary yet"}
                          >
                            {unit.period_start} · {unit.admin_level ?? "admin"} · {unit.renderable ? "map-ready" : "coverage only"}
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="rounded-xl px-3 py-4 text-sm" style={{ color: "rgba(255,255,255,0.44)" }}>
                {hotspotSummaryStatus === "loading"
                  ? "Loading admin-area hotspot units…"
                  : "No admin-area hotspot units match the selected source and date range."}
              </div>
            )
          ) : hotspotLeaderboardCells.length > 0 ? (
            <div className="space-y-1.5">
              {hotspotLeaderboardCells.map((cell) => {
                const active = cell.gridCellId === selectedHotspotId;
                const cellColor = hotspotToneColor(cell.displayScore);
                const cellRank = hotspotRankById.get(cell.gridCellId) ?? null;
                const gridCell = gridCellById.get(cell.gridCellId) ?? null;
                const coordinateTitle = hotspotGridCoordinateTitle(gridCell) ?? cell.gridCellId;
                const rankIndicator = hotspotRankIndicator(cell, hotspotSourceFocus);

                return (
                  <button
                    key={cell.gridCellId}
                    ref={(node) => {
                      hotspotRowRefs.current[cell.gridCellId] = node;
                    }}
                    type="button"
                    onClick={() => openHotspotCell(cell.gridCellId)}
                    className="w-full rounded-xl px-2.5 py-2 text-left transition-colors"
                    style={{
                      background: active ? withAlpha(cellColor, 0.10) : NEUTRAL_SURFACE.panelAlt,
                      border: `1px solid ${active ? withAlpha(cellColor, 0.24) : NEUTRAL_SURFACE.border}`,
                    }}
                  >
                    <div className="grid grid-cols-[28px_minmax(0,1fr)] items-start gap-2.5">
                      <div
                        className="flex h-7 w-7 items-center justify-center rounded-full font-mono text-[10px]"
                        style={{
                          background: active ? withAlpha(cellColor, 0.16) : NEUTRAL_SURFACE.panelDeep,
                          border: `1px solid ${active ? withAlpha(cellColor, 0.24) : NEUTRAL_SURFACE.border}`,
                          color: active ? withAlpha(cellColor, 0.96) : "rgba(255,255,255,0.54)",
                        }}
                      >
                        {cellRank ?? "•"}
                      </div>

                      <div className="min-w-0">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0 truncate text-[12px] font-medium" style={{ color: "rgba(255,255,255,0.84)" }}>
                            {coordinateTitle}
                          </div>

                          <div className="flex shrink-0 items-start justify-end text-right">
                            <HotspotSignalIcons activeLens={hotspotSourceFocus} cell={cell} />
                          </div>
                        </div>
                        <div
                          className="mt-1 whitespace-nowrap font-mono text-[10px] tracking-[0.02em]"
                          style={{ color: "rgba(130,130,130,0.92)" }}
                          title={rankIndicator.detail}
                        >
                          {rankIndicator.text}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="rounded-xl px-3 py-4 text-sm" style={{ color: "rgba(255,255,255,0.44)" }}>
              No grid cell received any detections in the current slice.
            </div>
          )}
        </div>
      </div>
    );

    const evaluationControlRail = (
      <aside
        className="monitor-filter-panel flex h-full min-h-0 flex-col rounded-2xl p-3"
        style={{
          height: monitorPanelHeight,
          background: NEUTRAL_SURFACE.panel,
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          borderTopColor: "transparent",
        }}
      >
        <PanelTitle icon={Filter}>
          Filters
        </PanelTitle>

        <div className="sb-scroll flex-1 space-y-3 overflow-y-auto pr-1">
          <FieldShell>
            <CompactDropdown
              label="Anomaly type"
              valueLabel={SPATIAL_VARIANT_NAMES[draftEvaluationAnomalyType] ?? draftEvaluationAnomalyType}
              placeholder={modelPerformanceStatus === "loading" ? "Loading signals" : "Choose a signal"}
              options={evaluationAnomalyTypeOptions}
              onSelect={(value) => setDraftEvaluationAnomalyType(value)}
              emptyText={modelPerformanceStatus === "loading"
                ? "Loading available anomaly types from the validation report."
                : "No anomaly types are available in the current validation report."}
            />
          </FieldShell>
          <FieldShell>
            <CompactDropdown
              label="Alert level"
              valueLabel={SPATIAL_ALERT_LEVEL_NAMES[draftEvaluationAlertLevel] ?? draftEvaluationAlertLevel}
              placeholder={modelPerformanceStatus === "loading" ? "Loading levels" : "Choose a level"}
              options={evaluationAlertLevelOptions}
              onSelect={(value) => setDraftEvaluationAlertLevel(value)}
              emptyText={modelPerformanceStatus === "loading"
                ? "Loading available alert levels from the validation report."
                : "No alert levels are available in the current validation report."}
            />
          </FieldShell>
          <FieldShell>
            <CompactDropdown
              label="Horizon"
              valueLabel={draftEvaluationHorizon > 0 ? (draftEvaluationHorizon === 1 ? "1 week" : `${draftEvaluationHorizon} weeks`) : ""}
              placeholder={modelPerformanceStatus === "loading" ? "Loading horizons" : "Choose a horizon"}
              options={evaluationHorizonOptions}
              onSelect={(value) => setDraftEvaluationHorizon(Number(value) || 0)}
              emptyText={modelPerformanceStatus === "loading"
                ? "Loading available horizons from the validation report."
                : "No evaluation horizons are available in the current validation report."}
            />
          </FieldShell>
        </div>

        <div
          className="grid grid-cols-2 gap-2 pt-3"
          style={{
            borderTop: `1px solid ${NEUTRAL_SURFACE.borderSoft}`,
          }}
        >
          <button
            type="button"
            onClick={applyEvaluationFilters}
            className="inline-flex items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-semibold"
            style={{
              background: NEUTRAL_SURFACE.panelAlt,
              border: `1px solid ${NEUTRAL_SURFACE.border}`,
              color: isPending ? "rgba(255,255,255,0.96)" : "rgba(255,255,255,0.84)",
            }}
          >
            {isPending ? "Refreshing…" : "Apply"}
          </button>
          <button
            type="button"
            onClick={resetEvaluationFilters}
            className="inline-flex w-full items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-medium"
            style={{
              background: NEUTRAL_SURFACE.panelAlt,
              border: `1px solid ${NEUTRAL_SURFACE.border}`,
              color: "rgba(255,255,255,0.68)",
            }}
          >
            <RotateCcw size={13} />
            Reset
          </button>
        </div>
      </aside>
    );

    const evaluationPrimaryPanel = (
      <div
        className="monitor-primary-panel flex h-full min-h-0 flex-col rounded-2xl p-4"
        style={{
          height: monitorPanelHeight,
          background: NEUTRAL_SURFACE.panel,
          border: `1px solid ${NEUTRAL_SURFACE.border}`,
          borderTopColor: "transparent",
        }}
      >
        <div className="sb-scroll flex-1 overflow-y-auto pr-1">
          <AlertsSpatialEvaluation
            report={spatialValidationReport}
            status={modelPerformanceStatus}
            bestF1Rows={modelPerformanceReport?.bestF1Leaderboard ?? []}
            activeAnomalyType={activeEvaluationAnomalyType}
            activeAlertLevel={activeEvaluationAlertLevel}
            activeHorizon={activeEvaluationHorizon}
          />
        </div>
      </div>
    );

  return (
    <div
      className="h-full w-full overflow-x-hidden overflow-y-auto"
      style={{
        background: NEUTRAL_SURFACE.canvas,
        color: "rgba(255,255,255,0.88)",
      }}
    >
      <style dangerouslySetInnerHTML={{ __html: `
        @keyframes alertRingPulse {
          0%   { transform: scale(1);   opacity: 0.55; }
          100% { transform: scale(2.5); opacity: 0; }
        }
        .alert-pulse { animation: alertRingPulse 2.6s ease-out infinite; }
        .sb-scroll::-webkit-scrollbar { width: 5px; }
        .sb-scroll::-webkit-scrollbar-track { background: transparent; }
        .sb-scroll::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.10); border-radius: 999px; }
        .monitor-tabs-spacer { display: none; }
        .monitor-tabs-nav {
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          align-items: end;
          gap: 8px;
          width: 100%;
        }
        .monitor-tab-button {
          width: 100%;
        }
        .monitor-filter-panel,
        .monitor-primary-panel,
        .monitor-secondary-panel {
          border-top-color: transparent !important;
        }
        .evaluation-panel-grid {
          grid-template-columns: minmax(0, 1fr);
        }
        @media (min-width: 1100px) {
          .monitor-tabs-grid,
          .monitor-panel-grid {
            grid-template-columns: 264px minmax(0, 1fr) 264px;
          }
          .evaluation-panel-grid {
            grid-template-columns: 264px minmax(0, 1fr);
          }
          .monitor-tabs-spacer { display: block; }
          .monitor-tabs-content { grid-column: 2 / 4; }
          .monitor-tabs-nav {
            display: inline-flex;
            width: auto;
            gap: 10px;
          }
          .monitor-tab-button {
            width: auto;
            min-width: 156px;
          }
        }
      `}} />

      <div className="relative mx-auto max-w-[1600px] px-6 pb-8 pt-10 sm:px-8 lg:px-10 xl:px-12">
        <header className="mb-8 px-1">
          <h1
            className={`${alertTitleFont.className} text-[31px] font-semibold leading-none tracking-[-0.045em]`}
            style={{ color: "rgba(255,255,255,0.94)" }}
          >
            Bioacoustic anomaly monitoring
          </h1>
        </header>

        <section className="space-y-0">
          <div className="monitor-tabs-grid grid gap-x-5 gap-y-3">
            <div className="monitor-tabs-spacer" />
            <div className="monitor-tabs-content relative z-10 -mb-[2px]">
              <div
                className="monitor-tabs-nav relative max-w-full rounded-[24px] p-1 pb-0"
                style={{ background: "rgba(255,255,255,0.012)" }}
              >
                {MONITOR_TABS.map((tab) => (
                  <MonitorTabButton
                    key={tab.value}
                    active={activeMonitorTab === tab.value}
                    label={tab.label}
                    onClick={() => setActiveMonitorTab(tab.value)}
                  />
                ))}
              </div>
            </div>
          </div>

          {activeMonitorTab === "embeddings" ? (
            <div className="monitor-panel-grid grid items-stretch gap-5">
              {filterRail}

              <div
                className="monitor-primary-panel overflow-hidden rounded-2xl"
                style={{
                  background: NEUTRAL_SURFACE.panel,
                  border: `1px solid ${NEUTRAL_SURFACE.border}`,
                  borderTopColor: "transparent",
                  height: monitorPanelHeight,
                }}
              >
                <div className="relative h-full">
                  <div className="absolute right-5 top-4 z-10 flex max-w-[calc(100%-2.5rem)] justify-end gap-1.5">
                    {data.filters.viewMode === "cluster"
                      ? (
                        <div className="flex flex-wrap justify-end gap-1.5">
                          {legendClusterGroups.map((group) => {
                            const groupColor = clusterColorByKey.get(group.key) ?? "rgba(56,189,248,1)";

                            return (
                              <div
                                key={group.key}
                                className="rounded-full px-2.5 py-1 text-[10px]"
                                style={{
                                  background: withAlpha(groupColor, 0.10),
                                  border: `1px solid ${withAlpha(groupColor, 0.18)}`,
                                  color: withAlpha(groupColor, 0.95),
                                }}
                              >
                                {group.shortLabel} · {group.count}
                              </div>
                            );
                          })}
                          {unclusteredVisibleCount > 0 ? (
                            <div
                              className="rounded-full px-2.5 py-1 text-[10px]"
                              style={{
                                background: "rgba(148,163,184,0.10)",
                                border: "1px solid rgba(148,163,184,0.18)",
                                color: "rgba(203,213,225,0.95)",
                              }}
                            >
                              No stable cluster · {unclusteredVisibleCount}
                            </div>
                          ) : null}
                        </div>
                      )
                      : [
                          { value: "high", label: "High", count: bandCounts.high, color: "rgba(138,52,58,1)" },
                          { value: "moderate", label: "Moderate", count: bandCounts.moderate, color: "rgba(150,118,62,1)" },
                          { value: "normal", label: "Normal", count: bandCounts.normal, color: "rgba(94,124,74,1)" },
                        ].map((band) => {
                          const active = focusedBand === band.value;
                          const faded = focusedBand !== null && focusedBand !== band.value;

                          return (
                            <button
                              type="button"
                              onClick={() => setFocusedBand((current) => current === band.value ? null : (band.value as AlertBand))}
                              key={band.label}
                              className="rounded-full px-2.5 py-1 text-[10px] transition-colors"
                              style={{
                                background: active ? withAlpha(band.color, 0.16) : withAlpha(band.color, 0.10),
                                border: `1px solid ${withAlpha(active ? band.color : faded ? "rgba(148,163,184,1)" : band.color, active ? 0.30 : 0.18)}`,
                                color: active ? withAlpha(band.color, 0.96) : faded ? "rgba(148,163,184,0.86)" : withAlpha(band.color, 0.95),
                              }}
                            >
                              {band.label} · {band.count}
                            </button>
                          );
                        })}
                    {baselinePoints.length > 0 ? (
                      <div
                        className="rounded-full px-2.5 py-1 text-[10px]"
                        style={{
                          background: withAlpha(BASELINE_HIGHLIGHT_COLOR, 0.10),
                          border: `1px solid ${withAlpha(BASELINE_HIGHLIGHT_COLOR, 0.18)}`,
                          color: withAlpha(BASELINE_HIGHLIGHT_COLOR, 0.95),
                        }}
                      >
                        Baseline refs · {baselinePoints.length}
                      </div>
                    ) : null}
                  </div>

                  <EmbeddingCanvasMap
                    points={sortedWorkspacePoints}
                    activePointIds={activePointIds}
                    baselinePointIds={baselinePointIds}
                    selectedId={selectedId}
                    viewMode={data.filters.viewMode}
                    focusedBand={focusedBand}
                    clusterColorByKey={clusterColorByKey}
                    emptyStateMessage={emptyStateMessage}
                    status={embeddingStatus}
                    onSelectPoint={openSelectedPoint}
                    onHoverPoint={prewarmHoveredPoint}
                  />

                  <div className="absolute bottom-4 right-5 flex items-center gap-4" style={{ zIndex: 10 }}>
                    {data.filters.viewMode === "cluster"
                      ? (
                        <>
                          {legendClusterGroups.map((group) => {
                            const groupColor = clusterColorByKey.get(group.key) ?? "rgba(56,189,248,1)";

                            return (
                              <div key={group.key} className="flex items-center gap-1.5">
                                <span className="h-2 w-2 rounded-full" style={{ background: groupColor, opacity: 0.78 }} />
                                <span className="font-mono text-[10px] uppercase tracking-[0.20em]" style={{ color: "rgba(255,255,255,0.28)" }}>
                                  {group.shortLabel}
                                </span>
                              </div>
                            );
                          })}
                          {unclusteredVisibleCount > 0 ? (
                            <div className="flex items-center gap-1.5">
                              <span className="h-2 w-2 rounded-full" style={{ background: "rgba(148,163,184,1)", opacity: 0.78 }} />
                              <span className="font-mono text-[10px] uppercase tracking-[0.20em]" style={{ color: "rgba(255,255,255,0.28)" }}>
                                Unclustered
                              </span>
                            </div>
                          ) : null}
                        </>
                      )
                      : null}
                  </div>
                </div>
              </div>

              {anomalyRail}
            </div>
          ) : null}

          {activeMonitorTab === "hotspot" ? (
            <div className="monitor-panel-grid grid items-stretch gap-5">
              {filterRail}

              <div
                className="monitor-primary-panel flex h-full min-h-0 flex-col rounded-2xl p-3"
                style={{
                  height: monitorPanelHeight,
                  background: NEUTRAL_SURFACE.panel,
                  border: `1px solid ${NEUTRAL_SURFACE.border}`,
                  borderTopColor: "transparent",
                }}
              >
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap gap-1.5">
                    {[
                      { value: "grid" as const, label: "5 km grid" },
                      { value: "admin_area" as const, label: "Admin area" },
                    ].map((grainOption) => {
                      const active = hotspotGrainFocus === grainOption.value;
                      return (
                        <button
                          key={grainOption.value}
                          type="button"
                          onClick={() => setHotspotGrainFocus(grainOption.value)}
                          className="rounded-full px-2.5 py-1 text-[10px] transition-colors"
                          style={{
                            background: active ? "rgba(255,255,255,0.14)" : "rgba(18,18,18,0.86)",
                            border: `1px solid ${active ? "rgba(255,255,255,0.22)" : "rgba(255,255,255,0.10)"}`,
                            color: active ? "rgba(255,255,255,0.92)" : "rgba(255,255,255,0.62)",
                          }}
                        >
                          {grainOption.label}
                        </button>
                      );
                    })}
                  </div>
                  <div className="flex flex-wrap justify-end gap-1.5">
                    {[
                      {
                        value: null,
                        label: "All",
                        count: hotspotCellsAll.length,
                      },
                      {
                        value: "bioacoustic" as const,
                        label: "Bioacoustic",
                        count:
                          hotspotSourceFocus === "bioacoustic" && hotspotSummary?.source === "bioacoustic"
                            ? hotspotSummary.coverage.totalRows
                            : hotspotSourceCounts.bioacoustic,
                      },
                    ].map((lensOption) => {
                      const active = hotspotSourceFocus === lensOption.value;
                      const defaultActive =
                        hotspotSourceFocus === null && lensOption.value === null;

                      return (
                        <button
                          key={lensOption.label}
                          type="button"
                          onClick={() => setHotspotSourceFocus(lensOption.value)}
                          className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] transition-colors"
                          style={{
                            background:
                              active || defaultActive
                                ? "rgba(255,255,255,0.14)"
                                : "rgba(18,18,18,0.86)",
                            border: `1px solid ${
                              active || defaultActive
                                ? "rgba(255,255,255,0.22)"
                                : "rgba(255,255,255,0.10)"
                            }`,
                            color:
                              active || defaultActive
                                ? "rgba(255,255,255,0.92)"
                                : "rgba(255,255,255,0.62)",
                          }}
                        >
                          <span>{lensOption.label}</span>
                          <span
                            className="rounded-full px-1.5 py-0.5 font-mono"
                            style={{
                              background:
                                active || defaultActive
                                  ? "rgba(255,255,255,0.14)"
                                  : "rgba(255,255,255,0.08)",
                              color:
                                active || defaultActive
                                  ? "rgba(255,255,255,0.86)"
                                  : "rgba(255,255,255,0.50)",
                            }}
                          >
                            {lensOption.count}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>

                {hotspotGrainFocus === "admin_area" ? (
                  <div className="grid min-h-0 flex-1 gap-3 md:grid-cols-3">
                    <div className="rounded-xl px-4 py-3 text-sm" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.66)" }}>
                      <div className="font-mono text-[9px] uppercase tracking-[0.18em]" style={{ color: "rgba(255,255,255,0.36)" }}>
                        Admin coverage
                      </div>
                      <div className="mt-2 text-2xl font-semibold" style={{ color: "rgba(255,255,255,0.90)" }}>
                        {(hotspotSummary?.coverage.totalUnits ?? 0).toLocaleString()}
                      </div>
                      <div className="mt-1 text-xs" style={{ color: "rgba(255,255,255,0.50)" }}>
                        units for {hotspotDisplayLabel(hotspotSourceFocus).toLowerCase()}
                      </div>
                    </div>
                    <div className="rounded-xl px-4 py-3 text-sm" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.66)" }}>
                      <div className="font-mono text-[9px] uppercase tracking-[0.18em]" style={{ color: "rgba(255,255,255,0.36)" }}>
                        Boundary matched
                      </div>
                      <div className="mt-2 text-2xl font-semibold" style={{ color: "rgba(255,255,255,0.90)" }}>
                        {(hotspotSummary?.coverage.renderableUnits ?? 0).toLocaleString()}
                      </div>
                      <div className="mt-1 text-xs" style={{ color: "rgba(255,255,255,0.50)" }}>
                        municipality-backed rows can draw on maps
                      </div>
                    </div>
                    <div className="rounded-xl px-4 py-3 text-sm" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.66)" }}>
                      <div className="font-mono text-[9px] uppercase tracking-[0.18em]" style={{ color: "rgba(255,255,255,0.36)" }}>
                        Caveat
                      </div>
                      <div className="mt-2 text-xs leading-relaxed" style={{ color: "rgba(255,255,255,0.58)" }}>
                        City fallback rows are kept in coverage and leaderboards but marked coverage-only until a municipality boundary or geocode exists.
                      </div>
                    </div>
                  </div>
                ) : filteredPoints.length === 0 && hotspotSourceFocus === null ? (
                  <div className="rounded-xl px-4 py-5 text-sm" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.46)" }}>
                    No detections are available in the current slice, so there is nothing to project into hotspot cells.
                  </div>
                ) : hotspotCells.length === 0 ? (
                  <div className="rounded-xl px-4 py-5 text-sm" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.46)" }}>
                    {hotspotSummaryStatus === "loading" && hotspotSourceFocus !== null
                      ? "Loading hotspot cells from the persisted modeling context..."
                      : "No hotspot cells are available in the current slice."}
                  </div>
                ) : gridCellsStatus === "error" ? (
                  <div className="rounded-xl px-4 py-5 text-sm" style={{ background: NEUTRAL_SURFACE.panelAlt, border: `1px solid ${NEUTRAL_SURFACE.border}`, color: "rgba(255,255,255,0.46)" }}>
                    The grid layer could not be loaded, so the map is temporarily unavailable. The hotspot ranking still reflects the current filtered slice.
                  </div>
                ) : (
                  <>
                    {gridCellsStatus !== "ready" ? (
                      <div className="flex min-h-0 flex-1 items-center justify-center rounded-xl text-sm" style={{ background: NEUTRAL_SURFACE.panelDeep, border: `1px solid ${NEUTRAL_SURFACE.borderSoft}`, color: "rgba(255,255,255,0.44)" }}>
                        Loading hotspot grid…
                      </div>
                    ) : (
                      <div className="relative flex min-h-0 flex-1 flex-col">
                        <AlertsHotspotMap
                          gridCells={gridCells}
                          hotspotCells={hotspotCells}
                          selectedGridCellId={selectedHotspotId}
                          onSelectCell={openHotspotCell}
                        />
                      </div>
                    )}
                  </>
                )}
              </div>

              {hotspotLeaderboardRail}
            </div>
          ) : null}

          {activeMonitorTab === "evaluation" ? (
            <div className="evaluation-panel-grid grid items-stretch gap-5">
              {evaluationControlRail}
              {evaluationPrimaryPanel}
            </div>
          ) : null}
        </section>
      </div>

      {modalPoint ? (
        <AlertReviewModal
          point={modalPoint}
          modeling={data.modeling}
          baselinePoints={baselinePoints}
          comparisonTargets={comparisonTargets}
          onTabChange={setModalActiveTab}
          index={modalIndex + 1}
          total={sortedWorkspacePoints.length}
          onClose={closeSelectedPoint}
          onNext={() => shiftSelection(1)}
          onPrevious={() => shiftSelection(-1)}
          onSelectRelated={openSelectedPoint}
          onCopyView={copyCurrentView}
          copyState={copyState}
        />
      ) : null}

      {openHotspotDetail ? (
        <HotspotDetailModal
          cell={openHotspotDetail}
          gridCell={openHotspotDetailGrid}
          onClose={() => setOpenHotspotDetailId(null)}
          activeLens={hotspotSourceFocus}
        />
      ) : null}
    </div>
  );
}
