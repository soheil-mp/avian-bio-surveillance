import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "fs/promises";
import { resolve } from "path";

import { STATION_QUALITY_LEVELS } from "@/config/stations";
import {
  TARGET_SPECIES,
  TARGET_SPECIES_BY_BIRDWEATHER_ID,
  TARGET_SPECIES_LIST,
} from "@/config/target-species";
import { query } from "@/lib/db";
import {
  loadLatestModelingAvailableWeeks,
  loadLatestModelingClusterPayloads,
  loadLatestModelingRun,
  loadLatestModelingScorePayloads,
  loadLatestModelPerformanceJson,
  type ModelingRunRow,
} from "@/lib/modeling-db";

import type {
  AlertClusterContext,
  AlertsEvaluationData,
  AlertReviewContext,
  AlertsHotspot,
  AlertsPageFilters,
  AlertsPageData,
  AlertsModelingSummary,
  AlertsTrendPoint,
  AlertVisualPoint,
  ModelPerformanceReport,
} from "./types";
import {
  assessAlertsArtifactCoverage,
  buildDefaultAlertsDateWindow,
  formatUtcDateInput,
  normalizeAlertsDateRange,
  preferAvailableWeeksWithData,
  readConfidenceParam,
  readEnumParam,
  readSearchParam,
  selectLatestAlertsCoverageDate,
  type AlertsAvailableWeek,
  type AlertsDefaultDateWindow,
  type SearchParamValue,
} from "./server";
import { buildAlertsEvaluationTrendData } from "./evaluation";
import { buildAlertsDataCacheKey } from "./cache-key";
import { parseModelPerformanceReport } from "./model-performance";

export { buildAlertsDataCacheKey } from "./cache-key";

export const dynamic = "force-dynamic";
const ALERTS_DISK_CACHE_DIR = resolve(process.cwd(), ".cache", "alerts");
const ALERTS_DATA_CACHE_TTL_MS = 300_000;
const ALERTS_DATA_CACHE_MAX_ENTRIES = 6;
// The embedding canvas can't render — and the browser can't pack or transfer —
// the full plottable set for a busy week: a single recent week holds >100k
// projected target-species detections, which made the main query alone take
// ~32s (detoasting feature_artifacts + a station PK lookup per row) and blow the
// 30s statement timeout. Cap to the most-recent N for the scatter view. This is
// a visual sample, not an analytic aggregate — the weekly trend/hotspot numbers
// come from the anomaly rows — so recency-capping the canvas is fine, and the
// detail popup path (focusDetectionId) returns its single row well under N.
const MAX_EMBEDDING_POINTS = 8000;
const PROJECTED_WEEK_LOOKBACK_BATCH_SIZE = 12;
const TARGET_SPECIES_IDS = TARGET_SPECIES_LIST.map((species) => String(species.birdweatherId));
const TARGET_SPECIES_ID_BY_ENGLISH = new Map(
  TARGET_SPECIES_LIST.map((species) => [species.english, String(species.birdweatherId)]),
);
const MIN_STATION_QUALITY_VALUES = new Set(
  STATION_QUALITY_LEVELS
    .map((level) => level.value)
    .filter((value) => value > 0)
    .map((value) => String(value)),
);

type AlertSort = AlertsPageFilters["sort"];
type AlertLevel = AlertsPageFilters["alertLevel"];
type EmbeddingBackend = AlertsPageFilters["backend"];

type AlertsCoverageRow = {
  latest_detected_at: Date | string | null;
  latest_embedded_detected_at: Date | string | null;
};

type AlertsCoverageState = {
  latestDetectedAt: Date | string | null;
  latestEmbeddedDetectedAt: Date | string | null;
  availableEmbeddedWeeks: AlertsAvailableWeek[];
};

type QueryRow = {
  detection_id: string;
  detected_at: Date | string;
  observation_date: Date | string | null;
  confidence: number | string | null;
  species_id: string | null;
  station_id: string | null;
  station_name: string | null;
  station_state: string | null;
  station_country: string | null;
  station_lat: number | string | null;
  station_lon: number | string | null;
  grid_cell_id: string | null;
  common_name: string | null;
  scientific_name: string | null;
  species_image_url: string | null;
  species_thumbnail_url: string | null;
  audio_url: string | null;
  processed_clip: string | null;
  smooth_waveform: string | null;
  waveform: string | null;
  spectrogram: string | null;
  // Precomputed by avian_biosurveillance.modeling.embedding_projection and
  // stored on birdweather_tbl. Null when the projection hasn't been run yet
  // for the active backend — callers drop those rows.
  pc1: number | string | null;
  pc2: number | string | null;
  alert_score: number | string | null;
  embedding_backend: string | null;
};

type WeeklyEvidenceArtifactRow = {
  species_id: string;
  species_english: string;
  location_id: string;
  period_start: string;
  period_end: string;
  month_start: string;
  model_anomaly_score: string;
  population_anomaly_score: string;
  population_drop_score: string;
  population_spike_score: string;
  embedding_anomaly_score: string;
  final_score: string;
  alert_level: string;
  top_detection_ids: string;
  observed_activity: string;
  observed_total_detections: string;
  expected_detections_per_active_day: string;
  expected_total_detections: string;
  baseline_level: string;
  baseline_sample_size: string;
  quality_mask_reason: string;
  embedding_drift_score: string;
  embedding_outlier_share: string;
  embedding_clip_count_scored: string;
  embedding_baseline_size: string;
  embedding_baseline_level: string;
  background_noise_summary: string;
  audio_quality_flag: string;
  sovon_dead_bird_count: string;
  sovon_mortality_score: string;
  sovon_lag_0_to_3mo_max_score?: string;
  sovon_lag_0_to_3mo_dead_birds?: string;
  sovon_lag_0_to_8w_max_score?: string;
  sovon_lag_0_to_8w_dead_birds?: string;
  sovon_has_context: string;
  weather_temp_lag_2w_mean_c: string;
  weather_temp_lag_4w_mean_c: string;
  weather_temp_lag_6w_mean_c: string;
  weather_precip_lag_7w_sum_mm: string;
  drought_dry_context_flag: string;
  drought_wet_context_flag: string;
  count_anomaly_score: string;
  count_drop_score: string;
  count_spike_score: string;
  context_boost: string;
  anomaly_streams: string;
};

type ClusterArtifactRow = {
  detection_id: string;
  period_start: string;
  period_end: string;
  location_type: string;
  location_id: string;
  embedding_model: string;
  cluster_id: string;
  cluster_label: string;
  cluster_rank: string;
  cluster_size: string;
  cluster_member_share: string;
  cluster_count: string;
  cluster_centroid_distance: string;
  cluster_distance_threshold: string;
  cluster_quality: string;
  is_cluster_exemplar: string;
  cluster_exemplar_detection_id: string;
};

type ModelingSummaryPayload = {
  generated_at?: string;
  run_id?: string;
  stats?: {
    rows_scored?: number;
    alerts_green?: number;
    alerts_amber?: number;
    alerts_red?: number;
    sparse_baseline_rows?: number;
    cluster_rows?: number;
    cluster_exemplar_rows?: number;
  };
};

type DetectionWeeklyEvidence = Pick<
  AlertReviewContext,
  | "weeklyPeriodStart"
  | "weeklyPeriodEnd"
  | "anomalyStreams"
  | "countAnomalyScore"
  | "countDropScore"
  | "countSpikeScore"
  | "embeddingDriftScore"
  | "embeddingOutlierShare"
  | "embeddingClipCountScored"
  | "embeddingBaselineSize"
  | "embeddingBaselineLevel"
  | "baselineLevel"
  | "baselineSampleSize"
  | "observedActivity"
  | "observedTotalDetections"
  | "expectedDetectionsPerActiveDay"
  | "expectedTotalDetections"
  | "sovonLagMaxScore"
  | "sovonLagDeadBirds"
  | "droughtDryContextFlag"
  | "droughtWetContextFlag"
  | "qualityMaskReason"
  | "backgroundNoiseSummary"
  | "audioQualityFlag"
  | "contextBoost"
> & {
  evidenceRankScore: number;
  alertLevel: string;
};

type ResolvedFilters = AlertsPageFilters & {
  minConfidenceValue: number | null;
  minStationQualityValue: number | null;
  alertThreshold: number;
  dateFromIso: string | null;
  dateToIso: string | null;
  hasExplicitDateRange: boolean;
  rangeLabel: string;
  // Internal: scope the main query to a single detection (feed detail popup).
  // The /alerts page never sets this, so its full-window load is unaffected.
  focusDetectionId: string | null;
};

type ModelingArtifactsBundle = {
  summary: AlertsModelingSummary;
  evaluation: AlertsEvaluationData;
  reviewByDetectionId: Record<string, AlertReviewContext>;
  clusterByDetectionId: Record<string, AlertClusterContext>;
  anomalyRows: WeeklyEvidenceArtifactRow[];
};

type ModelingArtifactManifest = {
  cacheKey: string;
};

type AlertsShellArtifacts = {
  summary: AlertsModelingSummary;
  availableWeeks: AlertsAvailableWeek[];
};

type AlertsDataCacheEntry = {
  key: string;
  expiresAt: number;
  value: AlertsPageData;
};

type AlertsCoverageCacheEntry = {
  expiresAt: number;
  value: AlertsCoverageState;
};

type AlertsDiskCacheEnvelope = {
  key: string;
  value: AlertsPageData;
};

type AlertsRuntimeState = {
  modelingArtifactsCache: {
    key: string;
    value: ModelingArtifactsBundle;
  } | null;
  alertsShellArtifactsCache: {
    key: string;
    value: AlertsShellArtifacts;
  } | null;
  modelingArtifactsPending: {
    key: string;
    promise: Promise<ModelingArtifactsBundle>;
  } | null;
  alertsShellArtifactsPending: {
    key: string;
    promise: Promise<AlertsShellArtifacts>;
  } | null;
  alertsDataCache: Map<string, AlertsDataCacheEntry>;
  alertsShellCache: Map<string, AlertsDataCacheEntry>;
  alertsCoverageCache: AlertsCoverageCacheEntry | null;
  alertsDataPending: Map<string, Promise<AlertsPageData>>;
  alertsShellPending: Map<string, Promise<AlertsPageData>>;
};

type GlobalAlertsDataState = typeof globalThis & {
  __avianAlertsRuntimeState?: AlertsRuntimeState;
};

const globalAlertsDataState = globalThis as GlobalAlertsDataState;
const alertsRuntimeState =
  globalAlertsDataState.__avianAlertsRuntimeState ??
  ({
    modelingArtifactsCache: null,
    alertsShellArtifactsCache: null,
    modelingArtifactsPending: null,
    alertsShellArtifactsPending: null,
    alertsDataCache: new Map<string, AlertsDataCacheEntry>(),
    alertsShellCache: new Map<string, AlertsDataCacheEntry>(),
    alertsCoverageCache: null,
    alertsDataPending: new Map<string, Promise<AlertsPageData>>(),
    alertsShellPending: new Map<string, Promise<AlertsPageData>>(),
  } satisfies AlertsRuntimeState);

globalAlertsDataState.__avianAlertsRuntimeState = alertsRuntimeState;

function readStationQualityParam(value: SearchParamValue): string {
  const candidate = readSearchParam(value);
  if (!candidate) return "";

  return MIN_STATION_QUALITY_VALUES.has(candidate) ? candidate : "";
}

function normalizeFilters(
  defaultDateWindow: AlertsDefaultDateWindow,
  searchParams?: Record<string, SearchParamValue>,
  defaultableAvailableWeeks: readonly AlertsAvailableWeek[] = [],
  selectableAvailableWeeks: readonly AlertsAvailableWeek[] = defaultableAvailableWeeks,
): ResolvedFilters {
  const selectedSpecies = readSearchParam(searchParams?.species);
  const backend = readEnumParam<EmbeddingBackend>(
    searchParams?.backend,
    ["birdnet", "perch_v2"],
    "perch_v2",
  );
  const alertLevel = readEnumParam<AlertLevel>(
    searchParams?.alertLevel,
    ["all", "moderate", "high"],
    "all",
  );
  const minConfidence = readConfidenceParam(searchParams?.minConfidence);
  const minConfidenceValue = minConfidence ? Number(minConfidence) : null;
  const minStationQuality = readStationQualityParam(searchParams?.minStationQuality);
  const minStationQualityValue = minStationQuality ? Number(minStationQuality) : null;
  const { dateFrom, dateTo, dateFromIso, dateToIso, rangeLabel } =
    normalizeAlertsDateRange(searchParams, defaultDateWindow, selectableAvailableWeeks);
  const hasExplicitDateRange = Boolean(
    readSearchParam(searchParams?.dateFrom) || readSearchParam(searchParams?.dateTo),
  );

  return {
    species: TARGET_SPECIES.has(selectedSpecies) ? selectedSpecies : "",
    station: "",
    background: "",
    backend,
    dateFrom,
    dateTo,
    minConfidence,
    minStationQuality,
    alertLevel,
    viewMode: "anomaly",
    clusterScope: "all",
    clusterKey: "",
    sort: "alert-desc",
    minConfidenceValue,
    minStationQualityValue,
    alertThreshold:
      alertLevel === "high" ? 0.5 : alertLevel === "moderate" ? 0.3 : 0,
    dateFromIso,
    dateToIso,
    hasExplicitDateRange,
    rangeLabel,
    focusDetectionId: readSearchParam(searchParams?.focusDetectionId) || null,
  };
}

function collectAvailableWeeks(
  anomalyRows: WeeklyEvidenceArtifactRow[],
): AlertsAvailableWeek[] {
  const dedupedWeeks = new Map<string, AlertsAvailableWeek>();

  for (const row of anomalyRows) {
    const periodStart = row.period_start?.trim() || "";
    const periodEnd = row.period_end?.trim() || row.period_start?.trim() || "";
    if (!periodStart || !periodEnd) continue;

    const key = `${periodStart}:${periodEnd}`;
    if (!dedupedWeeks.has(key)) {
      dedupedWeeks.set(key, { periodStart, periodEnd });
    }
  }

  return [...dedupedWeeks.values()].sort(
    (left, right) => left.periodStart.localeCompare(right.periodStart) || left.periodEnd.localeCompare(right.periodEnd),
  );
}

async function loadDefaultAlertsDateWindow(
  coverage: AlertsCoverageState,
  availableWeeks: readonly AlertsAvailableWeek[] = [],
): Promise<AlertsDefaultDateWindow> {
  if (availableWeeks.length > 0) {
    return buildDefaultAlertsDateWindow({ availableWeeks });
  }

  return buildDefaultAlertsDateWindow({
    latestDetectedAt: selectLatestAlertsCoverageDate({
      latestDetectedAt: coverage.latestDetectedAt,
      latestEmbeddedDetectedAt: coverage.latestEmbeddedDetectedAt,
    }),
  });
}

function selectDefaultableAvailableWeeks({
  availableWeeks,
  coverage,
  artifactCoverageTrusted,
}: {
  availableWeeks: readonly AlertsAvailableWeek[];
  coverage: AlertsCoverageState;
  artifactCoverageTrusted: boolean;
}): AlertsAvailableWeek[] {
  if (coverage.availableEmbeddedWeeks.length === 0) {
    return [];
  }

  return artifactCoverageTrusted
    ? preferAvailableWeeksWithData({
        availableWeeks,
        weeksWithData: coverage.availableEmbeddedWeeks,
      })
    : coverage.availableEmbeddedWeeks;
}

async function loadExplicitRangeAlertsCoverage(
  availableWeeks: readonly AlertsAvailableWeek[],
): Promise<AlertsCoverageState> {
  const cached = readCachedAlertsCoverage();
  if (cached) {
    return cached;
  }

  // `birdweather_stations_tbl.last_detection` is a denormalized column that lags
  // the live detection/projection pipeline by months (it currently reads
  // 2025-07 while detections and perch_v2 projections are current to year-end).
  // Anchoring coverage on it made assessAlertsArtifactCoverage() flag the
  // current modeling run as "ahead of detections" and collapse the whole page to
  // a stale single week. Read the true latest detection directly instead;
  // MAX(detected_at) is index-backed on the hypertable (a few seconds) and the
  // coverage result is cached for 5 minutes.
  const stationCoverageResult = await query<AlertsCoverageRow>(`
    SELECT
      MAX(detected_at)::timestamptz AS latest_detected_at,
      NULL::timestamptz AS latest_embedded_detected_at
    FROM birdweather_tbl
  `);
  const latestDetectedAt = stationCoverageResult.rows[0]?.latest_detected_at ?? null;
  const latestDetectedDate = latestDetectedAt
    ? formatUtcDateInput(latestDetectedAt)
    : null;
  const coverageCandidateWeeks = latestDetectedDate
    ? availableWeeks.filter((week) => week.periodEnd <= latestDetectedDate)
    : [...availableWeeks];

  // The embedding canvas needs detection-level projection coordinates, not just
  // weekly score rows. Search backward in small modeled-week batches so the
  // default lands on the latest week with plottable target-species clips without
  // forcing a full-history distinct scan on first page load.
  let availableEmbeddedWeeks: AlertsAvailableWeek[] = [];
  for (
    let endIndex = coverageCandidateWeeks.length;
    endIndex > 0 && availableEmbeddedWeeks.length === 0;
    endIndex -= PROJECTED_WEEK_LOOKBACK_BATCH_SIZE
  ) {
    const batchStartIndex = Math.max(
      0,
      endIndex - PROJECTED_WEEK_LOOKBACK_BATCH_SIZE,
    );
    const candidateWeeks = coverageCandidateWeeks.slice(batchStartIndex, endIndex);
    const earliestWeek = candidateWeeks[0];
    const latestWeek = candidateWeeks[candidateWeeks.length - 1];
    if (!earliestWeek || !latestWeek) continue;

    const projectedWeekResult = await query<{ period_start: string }>(
      `
      SELECT date_trunc('week', b.detected_at)::date::text AS period_start
      FROM birdweather_tbl b
      WHERE b.has_processed_clip
        AND b.species_id = ANY($1::text[])
        AND b.detected_at >= $2::timestamptz
        AND b.detected_at <= $3::timestamptz
        AND (
          (
            b.birdnet_embedding IS NOT NULL
            AND b.birdnet_pc1 IS NOT NULL
            AND b.birdnet_pc2 IS NOT NULL
          )
          OR (
            b.perch_v2_embedding IS NOT NULL
            AND b.perch_v2_pc1 IS NOT NULL
            AND b.perch_v2_pc2 IS NOT NULL
          )
        )
      ORDER BY b.detected_at DESC
      LIMIT 1
      `,
      [
        TARGET_SPECIES_IDS,
        `${earliestWeek.periodStart}T00:00:00.000Z`,
        `${latestWeek.periodEnd}T23:59:59.999Z`,
      ],
    );
    const projectedWeekStart = projectedWeekResult.rows[0]?.period_start;

    if (projectedWeekStart) {
      availableEmbeddedWeeks = candidateWeeks.filter(
        (week) => week.periodStart === projectedWeekStart,
      );
    }
  }

  const latestEmbeddedWeek =
    availableEmbeddedWeeks[availableEmbeddedWeeks.length - 1] ?? null;

  const coverage = {
    latestDetectedAt,
    latestEmbeddedDetectedAt:
      latestEmbeddedWeek?.periodEnd ?? latestDetectedAt,
    availableEmbeddedWeeks,
  };

  alertsRuntimeState.alertsCoverageCache = {
    expiresAt: Date.now() + ALERTS_DATA_CACHE_TTL_MS,
    value: coverage,
  };

  return coverage;
}

function buildWhereClause(filters: ResolvedFilters): {
  baseWhereClause: string;
  whereClause: string;
  params: unknown[];
  hasLookupFilters: boolean;
} {
  const params: unknown[] = [];
  const baseClauses = [
    "b.has_processed_clip",
    "(b.birdnet_embedding IS NOT NULL OR b.perch_v2_embedding IS NOT NULL)",
  ];
  const lookupClauses: string[] = [];

  params.push(TARGET_SPECIES_IDS);
  baseClauses.push(`b.species_id = ANY($${params.length}::text[])`);

  if (filters.species) {
    const speciesId = TARGET_SPECIES_ID_BY_ENGLISH.get(filters.species);
    if (speciesId) {
      params.push(speciesId);
      lookupClauses.push(`b.species_id = $${params.length}`);
    }
  }

  if (filters.backend === "birdnet") {
    baseClauses.push("b.birdnet_embedding IS NOT NULL");
    // Only load rows the projection has actually reached (pc1/pc2 present).
    // Embeddings extend far past the projected window, so loading every
    // embedded row makes dense windows scan 100k+ rows and time out; these
    // rows are skipped in JS anyway because they can't be plotted.
    baseClauses.push("b.birdnet_pc1 IS NOT NULL");
    baseClauses.push("b.birdnet_pc2 IS NOT NULL");
  } else if (filters.backend === "perch_v2") {
    baseClauses.push("b.perch_v2_embedding IS NOT NULL");
    baseClauses.push("b.perch_v2_pc1 IS NOT NULL");
    baseClauses.push("b.perch_v2_pc2 IS NOT NULL");
  }

  if (filters.minConfidenceValue !== null) {
    params.push(filters.minConfidenceValue);
    baseClauses.push(`COALESCE(b.confidence, 0) >= $${params.length}`);
  }

  if (filters.minStationQualityValue !== null) {
    params.push(filters.minStationQualityValue);
    lookupClauses.push(`COALESCE(rs.lifetime_availability_pct, 0) >= $${params.length}`);
  }

  if (filters.dateFromIso) {
    params.push(filters.dateFromIso);
    baseClauses.push(`b.detected_at >= $${params.length}::timestamptz`);
  }

  if (filters.dateToIso) {
    params.push(filters.dateToIso);
    baseClauses.push(`b.detected_at <= $${params.length}::timestamptz`);
  }

  // Single-detection lookup (feed popup): PK is (detection_id, detected_at),
  // so this + the day bound is an index lookup instead of a day-wide scan.
  if (filters.focusDetectionId) {
    params.push(filters.focusDetectionId);
    baseClauses.push(`b.detection_id = $${params.length}`);
  }

  const whereParts = [...baseClauses, ...lookupClauses];

  return {
    baseWhereClause: `WHERE ${baseClauses.join("\n      AND ")}`,
    whereClause: `WHERE ${whereParts.join("\n      AND ")}`,
    params,
    hasLookupFilters: lookupClauses.length > 0,
  };
}

function resolveOrderBy(sort: AlertSort): string {
  const sortMap: Record<Exclude<AlertSort, "alert-desc">, string> = {
    "date-desc": "b.detected_at DESC",
    "date-asc": "b.detected_at ASC",
    "confidence-desc": "b.confidence DESC NULLS LAST",
    "confidence-asc": "b.confidence ASC NULLS LAST",
    "species-asc": "sp.common_name ASC NULLS LAST, b.detected_at DESC",
  };

  return sort === "alert-desc" ? "b.detected_at DESC" : sortMap[sort];
}

// Selects the precomputed projection columns produced by
// avian_biosurveillance.modeling.embedding_projection. The hot path no longer
// fetches embedding text or runs PCA / kNN in Node — each row carries its
// canvas coordinate (pc1, pc2) and alert_score directly.
function buildEmbeddingSelection(backend: EmbeddingBackend): {
  embeddingSelect: string;
  backendSelect: string;
} {
  const column = backend === "birdnet" ? "birdnet" : "perch_v2";
  const featureKey = column;

  return {
    embeddingSelect: `b.${column}_pc1 AS pc1,
      b.${column}_pc2 AS pc2,
      b.${column}_alert_score AS alert_score`,
    backendSelect: `COALESCE(b.feature_artifacts->'embeddings'->'${featureKey}'->>'backend', '${featureKey}') AS embedding_backend`,
  };
}

function toDateString(value: Date | string): string {
  if (value instanceof Date) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }

  return String(value).split("T")[0];
}

function buildMediaProxyUrl(bucket: string, objectPath: string): string {
  const params = new URLSearchParams({
    bucket,
    path: objectPath,
  });
  return `/api/audio-clip?${params.toString()}`;
}

function artifactProxyUrl(
  value: unknown,
  expectedBucket: "processed-audio" | "features",
): string | null {
  if (typeof value !== "string") return null;
  const artifact = value.trim();
  if (!artifact) return null;

  const bucketPrefix = `${expectedBucket}/`;
  const objectPath = artifact.startsWith(bucketPrefix)
    ? artifact.slice(bucketPrefix.length)
    : artifact;
  if (!objectPath || objectPath.includes("..")) return null;

  return buildMediaProxyUrl(expectedBucket, objectPath);
}

function processedClipFallback(row: QueryRow): string | null {
  if (!row.observation_date) return null;
  const stationId = row.station_id || "unknown_station";
  const dateStr = toDateString(row.observation_date);
  return buildMediaProxyUrl(
    "processed-audio",
    `birdweather/${dateStr}/${stationId}/${row.detection_id}.flac`,
  );
}

function featureChartFallback(
  row: QueryRow,
  prefix: "waveforms" | "spectrograms",
  suffix = "",
): string | null {
  if (!row.observation_date) return null;
  const stationId = row.station_id || "unknown_station";
  const dateStr = toDateString(row.observation_date);
  return buildMediaProxyUrl(
    "features",
    `${prefix}/birdweather/${dateStr}/${stationId}/${row.detection_id}${suffix}.png`,
  );
}

function toNumber(value: unknown): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function toNullableNumber(value: unknown): number | null {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function splitCsvField(value: string | undefined): string[] {
  if (!value) return [];

  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function readBooleanFlag(value: string | undefined): boolean {
  return value?.trim().toLowerCase() === "true";
}

function emptyAlertsModelingSummary(
  coverageLabel = "No persisted weekly modeling run was found for alerts.",
): AlertsModelingSummary {
  return {
    available: false,
    runId: null,
    generatedAt: null,
    rowsScored: 0,
    alertsGreen: 0,
    alertsAmber: 0,
    alertsRed: 0,
    sparseBaselineRows: 0,
    queueRows: 0,
    linkedDetections: 0,
    clusterRows: 0,
    clusterExemplarRows: 0,
    clusteredDetections: 0,
    topReviewReasons: [],
    coverageLabel,
    trendFocusLabel: null,
    trendSeries: [],
    hotspots: [],
  };
}

function modelingSummaryPayloadFromRun(
  run: ModelingRunRow | null,
): ModelingSummaryPayload | null {
  if (!run) return null;

  return {
    run_id: run.run_id,
    generated_at:
      run.generated_at instanceof Date
        ? run.generated_at.toISOString()
        : run.generated_at
          ? String(run.generated_at)
          : undefined,
    stats: (run.stats ?? {}) as ModelingSummaryPayload["stats"],
  };
}

function buildShellModelingSummary(
  parsedSummary: ModelingSummaryPayload | null,
): AlertsModelingSummary {
  if (!parsedSummary) {
    return emptyAlertsModelingSummary();
  }

  return {
    available: true,
    runId: parsedSummary.run_id ?? null,
    generatedAt: parsedSummary.generated_at ?? null,
    rowsScored: toNumber(parsedSummary.stats?.rows_scored),
    alertsGreen: toNumber(parsedSummary.stats?.alerts_green),
    alertsAmber: toNumber(parsedSummary.stats?.alerts_amber),
    alertsRed: toNumber(parsedSummary.stats?.alerts_red),
    sparseBaselineRows: toNumber(parsedSummary.stats?.sparse_baseline_rows),
    queueRows: 0,
    linkedDetections: 0,
    clusterRows: toNumber(parsedSummary.stats?.cluster_rows),
    clusterExemplarRows: toNumber(parsedSummary.stats?.cluster_exemplar_rows),
    clusteredDetections: 0,
    topReviewReasons: [],
    coverageLabel:
      "Detailed weekly evidence loads after the embedding slice finishes loading.",
    trendFocusLabel: null,
    trendSeries: [],
    hotspots: [],
  };
}

function downgradeShellModelingSummary(
  summary: AlertsModelingSummary,
  reason: string,
): AlertsModelingSummary {
  return {
    available: false,
    runId: summary.runId,
    generatedAt: summary.generatedAt,
    rowsScored: 0,
    alertsGreen: 0,
    alertsAmber: 0,
    alertsRed: 0,
    sparseBaselineRows: 0,
    queueRows: 0,
    linkedDetections: 0,
    clusterRows: 0,
    clusterExemplarRows: 0,
    clusteredDetections: 0,
    topReviewReasons: [],
    coverageLabel:
      `${reason} Alerts is falling back to live database coverage until a complete weekly modeling run is available.`,
    trendFocusLabel: null,
    trendSeries: [],
    hotspots: [],
  };
}

async function buildModelingArtifactManifest(): Promise<ModelingArtifactManifest> {
  const run = await loadLatestModelingRun();
  const stats = run?.stats ? JSON.stringify(run.stats) : "";

  return {
    cacheKey: run
      ? `${run.run_id}:${String(run.generated_at ?? "")}:${stats}`
      : "modeling-run:missing",
  };
}

async function loadAlertsShellArtifacts(
  manifest?: ModelingArtifactManifest,
): Promise<AlertsShellArtifacts> {
  const resolvedManifest = manifest ?? (await buildModelingArtifactManifest());

  if (alertsRuntimeState.alertsShellArtifactsCache?.key === resolvedManifest.cacheKey) {
    return alertsRuntimeState.alertsShellArtifactsCache.value;
  }

  return Promise.all([
    loadLatestModelingRun(),
    loadLatestModelingAvailableWeeks(),
  ]).then(([run, availableWeeks]) => {
    const value = {
      summary: buildShellModelingSummary(modelingSummaryPayloadFromRun(run)),
      availableWeeks,
    } satisfies AlertsShellArtifacts;

    alertsRuntimeState.alertsShellArtifactsCache = {
      key: resolvedManifest.cacheKey,
      value,
    };

    return value;
  });
}

function emptyEvaluationData(
  latestRunId: string | null = null,
  latestGeneratedAt: string | null = null,
  coverageLabel = "No anomaly rows are available for trend evaluation yet.",
): AlertsEvaluationData {
  return {
    available: false,
    coverageLabel,
    latestRunId,
    latestGeneratedAt,
    speciesOptions: [],
    trendPoints: [],
    summary: {
      speciesCount: 0,
      weekCount: 0,
      anomalyCount: 0,
      redAnomalyCount: 0,
      sovonDeadBirdCount: 0,
    },
  };
}

function loadEvaluationArtifacts(
  anomalyRows: WeeklyEvidenceArtifactRow[],
  latestRunId: string | null,
  latestGeneratedAt: string | null,
  dateFromIso: string | null = null,
  dateToIso: string | null = null,
): AlertsEvaluationData {
  if (anomalyRows.length === 0) {
    return emptyEvaluationData(latestRunId, latestGeneratedAt);
  }

  return buildAlertsEvaluationTrendData({
    anomalyRows,
    dateFromIso,
    dateToIso,
    latestRunId,
    latestGeneratedAt,
  });
}

async function loadModelingArtifacts(
  manifest?: ModelingArtifactManifest,
): Promise<ModelingArtifactsBundle> {
  const resolvedManifest = manifest ?? (await buildModelingArtifactManifest());

  if (alertsRuntimeState.modelingArtifactsCache?.key === resolvedManifest.cacheKey) {
    return alertsRuntimeState.modelingArtifactsCache.value;
  }

  return loadModelingArtifactsUncached().then((value) => {
    alertsRuntimeState.modelingArtifactsCache = {
      key: resolvedManifest.cacheKey,
      value,
    };
    return value;
  });
}

function readAlertsDataCache(key: string): AlertsPageData | null {
  const cached = alertsRuntimeState.alertsDataCache.get(key);
  if (!cached) return null;

  if (cached.expiresAt <= Date.now()) {
    alertsRuntimeState.alertsDataCache.delete(key);
    return null;
  }

  alertsRuntimeState.alertsDataCache.delete(key);
  alertsRuntimeState.alertsDataCache.set(key, cached);
  return cached.value;
}

function writeAlertsDataCache(key: string, value: AlertsPageData): void {
  alertsRuntimeState.alertsDataCache.set(key, {
    key,
    expiresAt: Date.now() + ALERTS_DATA_CACHE_TTL_MS,
    value,
  });

  while (alertsRuntimeState.alertsDataCache.size > ALERTS_DATA_CACHE_MAX_ENTRIES) {
    const oldestKey = alertsRuntimeState.alertsDataCache.keys().next().value;
    if (!oldestKey) break;
    alertsRuntimeState.alertsDataCache.delete(oldestKey);
  }
}

function alertsDiskCachePath(key: string): string {
  return resolve(
    ALERTS_DISK_CACHE_DIR,
    `${createHash("sha256").update(key).digest("hex")}.json`,
  );
}

async function readAlertsDataDiskCache(
  key: string,
): Promise<AlertsPageData | null> {
  try {
    const cachedText = await readFile(alertsDiskCachePath(key), "utf-8");
    const parsed = JSON.parse(cachedText) as AlertsDiskCacheEnvelope;
    if (parsed?.key !== key || !parsed.value) {
      return null;
    }

    writeAlertsDataCache(key, parsed.value);
    return parsed.value;
  } catch {
    return null;
  }
}

async function writeAlertsDataDiskCache(
  key: string,
  value: AlertsPageData,
): Promise<void> {
  try {
    await mkdir(ALERTS_DISK_CACHE_DIR, { recursive: true });
    await writeFile(
      alertsDiskCachePath(key),
      JSON.stringify({ key, value } satisfies AlertsDiskCacheEnvelope),
      "utf-8",
    );
  } catch (error: unknown) {
    console.warn("[alerts] failed to write disk cache:", error);
  }
}

function readAlertsShellCache(key: string): AlertsPageData | null {
  const cached = alertsRuntimeState.alertsShellCache.get(key);
  if (!cached) return null;

  if (cached.expiresAt <= Date.now()) {
    alertsRuntimeState.alertsShellCache.delete(key);
    return null;
  }

  alertsRuntimeState.alertsShellCache.delete(key);
  alertsRuntimeState.alertsShellCache.set(key, cached);
  return cached.value;
}

function writeAlertsShellCache(key: string, value: AlertsPageData): void {
  alertsRuntimeState.alertsShellCache.set(key, {
    key,
    expiresAt: Date.now() + ALERTS_DATA_CACHE_TTL_MS,
    value,
  });

  while (alertsRuntimeState.alertsShellCache.size > ALERTS_DATA_CACHE_MAX_ENTRIES) {
    const oldestKey = alertsRuntimeState.alertsShellCache.keys().next().value;
    if (!oldestKey) break;
    alertsRuntimeState.alertsShellCache.delete(oldestKey);
  }
}

function readCachedAlertsCoverage(): AlertsCoverageState | null {
  const cached = alertsRuntimeState.alertsCoverageCache;
  if (!cached) return null;

  if (cached.expiresAt <= Date.now()) {
    alertsRuntimeState.alertsCoverageCache = null;
    return null;
  }

  return cached.value;
}

function buildAlertsCoverageCacheKey(coverage: AlertsCoverageState): string {
  return [
    coverage.latestDetectedAt ? formatUtcDateInput(coverage.latestDetectedAt) : "none",
    coverage.latestEmbeddedDetectedAt
      ? formatUtcDateInput(coverage.latestEmbeddedDetectedAt)
      : "none",
    coverage.availableEmbeddedWeeks
      .map((week) => `${week.periodStart}:${week.periodEnd}`)
      .join(",") || "none",
  ].join(":");
}

function downgradeModelingArtifactsBundle(
  bundle: ModelingArtifactsBundle,
  reason: string,
): ModelingArtifactsBundle {
  return {
    summary: {
      available: false,
      runId: bundle.summary.runId,
      generatedAt: bundle.summary.generatedAt,
      rowsScored: 0,
      alertsGreen: 0,
      alertsAmber: 0,
      alertsRed: 0,
      sparseBaselineRows: 0,
      queueRows: 0,
      linkedDetections: 0,
      clusterRows: 0,
      clusterExemplarRows: 0,
      clusteredDetections: 0,
      topReviewReasons: [],
      coverageLabel:
        `${reason} Alerts is falling back to live database coverage until a complete weekly modeling run is available.`,
      trendFocusLabel: null,
      trendSeries: [],
      hotspots: [],
    },
    evaluation: emptyEvaluationData(
      bundle.evaluation.latestRunId,
      bundle.evaluation.latestGeneratedAt,
      `${reason} Alerts is hiding trend evaluation until the persisted modeling run realigns with the live database.`,
    ),
    reviewByDetectionId: {},
    clusterByDetectionId: {},
    anomalyRows: [],
  };
}

async function loadModelingArtifactsUncached(): Promise<ModelingArtifactsBundle> {
  const emptySummary = emptyAlertsModelingSummary();
  const scorePayloads = await loadLatestModelingScorePayloads();
  const runId = scorePayloads.run?.run_id ?? "";
  const clusterRowsRaw = runId ? await loadLatestModelingClusterPayloads(runId) : [];

  if (!scorePayloads.run && clusterRowsRaw.length === 0 && scorePayloads.rows.length === 0) {
    return {
      summary: emptySummary,
      evaluation: loadEvaluationArtifacts([], null, null),
      reviewByDetectionId: {},
      clusterByDetectionId: {},
      anomalyRows: [],
    };
  }

  const parsedSummary = modelingSummaryPayloadFromRun(scorePayloads.run);
  const clusterRows = clusterRowsRaw as ClusterArtifactRow[];
  const anomalyRows = scorePayloads.rows as WeeklyEvidenceArtifactRow[];
  const reviewByDetectionId: Record<string, AlertReviewContext> = {};
  const clusterByDetectionId: Record<string, AlertClusterContext> = {};
  const weeklyEvidenceByDetectionId: Record<string, DetectionWeeklyEvidence> = {};
  const linkedDetections = new Set<string>();
  const clusteredDetections = new Set<string>();
  const exemplarDetections = new Set<string>();
  const evaluation = loadEvaluationArtifacts(
    anomalyRows,
    parsedSummary?.run_id ?? null,
    parsedSummary?.generated_at ?? null,
  );

  for (const row of anomalyRows) {
    const detectionIds = splitCsvField(row.top_detection_ids);
    if (detectionIds.length === 0) continue;

    const evidenceRankScore = Math.max(
      toNumber(row.model_anomaly_score),
      toNumber(row.final_score),
    );
    const weeklyEvidence: DetectionWeeklyEvidence = {
      weeklyPeriodStart: row.period_start?.trim() || null,
      weeklyPeriodEnd: row.period_end?.trim() || null,
      anomalyStreams: splitCsvField(row.anomaly_streams),
      countAnomalyScore: Math.max(
        toNumber(row.population_anomaly_score),
        toNumber(row.count_anomaly_score),
      ),
      countDropScore: Math.max(
        toNumber(row.population_drop_score),
        toNumber(row.count_drop_score),
      ),
      countSpikeScore: Math.max(
        toNumber(row.population_spike_score),
        toNumber(row.count_spike_score),
      ),
      embeddingDriftScore: Math.max(
        toNumber(row.embedding_anomaly_score),
        toNumber(row.embedding_drift_score),
      ),
      embeddingOutlierShare: toNullableNumber(row.embedding_outlier_share),
      embeddingClipCountScored: toNumber(row.embedding_clip_count_scored),
      embeddingBaselineSize: toNumber(row.embedding_baseline_size),
      embeddingBaselineLevel: row.embedding_baseline_level?.trim() || null,
      baselineLevel: row.baseline_level?.trim() || null,
      baselineSampleSize: toNumber(row.baseline_sample_size),
      observedActivity: toNullableNumber(row.observed_activity),
      observedTotalDetections: toNullableNumber(row.observed_total_detections),
      expectedDetectionsPerActiveDay: toNullableNumber(row.expected_detections_per_active_day),
      expectedTotalDetections: toNullableNumber(row.expected_total_detections),
      sovonLagMaxScore: toNumber(
        row.sovon_lag_0_to_3mo_max_score ?? row.sovon_lag_0_to_8w_max_score,
      ),
      sovonLagDeadBirds: toNumber(
        row.sovon_lag_0_to_3mo_dead_birds ?? row.sovon_lag_0_to_8w_dead_birds,
      ),
      droughtDryContextFlag: false,
      droughtWetContextFlag: false,
      qualityMaskReason: row.quality_mask_reason?.trim() || null,
      backgroundNoiseSummary: row.background_noise_summary?.trim() || null,
      audioQualityFlag: row.audio_quality_flag?.trim() || null,
      contextBoost: 0,
      evidenceRankScore,
      alertLevel: row.alert_level?.trim() || "green",
    };

    for (const detectionId of detectionIds) {
      const existingWeeklyEvidence = weeklyEvidenceByDetectionId[detectionId];
      if (!existingWeeklyEvidence || weeklyEvidence.evidenceRankScore > existingWeeklyEvidence.evidenceRankScore) {
        weeklyEvidenceByDetectionId[detectionId] = weeklyEvidence;
      }
      linkedDetections.add(detectionId);
      const reviewContext: AlertReviewContext = {
        finalScore: weeklyEvidence.evidenceRankScore,
        reviewPriority: weeklyEvidence.alertLevel,
        reviewReason: "model evidence",
        alertLevel: weeklyEvidence.alertLevel,
        requiresAudioReview: false,
        requiresContextReview: false,
        qualityReviewFlag: weeklyEvidence.qualityMaskReason !== null,
        qualityMaskReason: weeklyEvidence.qualityMaskReason,
        weeklyPeriodStart: weeklyEvidence.weeklyPeriodStart,
        weeklyPeriodEnd: weeklyEvidence.weeklyPeriodEnd,
        backgroundNoiseSummary: weeklyEvidence.backgroundNoiseSummary,
        audioQualityFlag: weeklyEvidence.audioQualityFlag,
        anomalyStreams: weeklyEvidence.anomalyStreams,
        countAnomalyScore: weeklyEvidence.countAnomalyScore,
        countDropScore: weeklyEvidence.countDropScore,
        countSpikeScore: weeklyEvidence.countSpikeScore,
        embeddingDriftScore: weeklyEvidence.embeddingDriftScore,
        embeddingOutlierShare: weeklyEvidence.embeddingOutlierShare,
        embeddingClipCountScored: weeklyEvidence.embeddingClipCountScored,
        embeddingBaselineSize: weeklyEvidence.embeddingBaselineSize,
        embeddingBaselineLevel: weeklyEvidence.embeddingBaselineLevel,
        baselineLevel: weeklyEvidence.baselineLevel,
        baselineSampleSize: weeklyEvidence.baselineSampleSize,
        observedActivity: weeklyEvidence.observedActivity,
        observedTotalDetections: weeklyEvidence.observedTotalDetections,
        expectedDetectionsPerActiveDay: weeklyEvidence.expectedDetectionsPerActiveDay,
        expectedTotalDetections: weeklyEvidence.expectedTotalDetections,
        sovonLagMaxScore: weeklyEvidence.sovonLagMaxScore,
        sovonLagDeadBirds: weeklyEvidence.sovonLagDeadBirds,
        droughtDryContextFlag: false,
        droughtWetContextFlag: false,
        contextBoost: 0,
      };
      const existingReviewContext = reviewByDetectionId[detectionId];
      if (!existingReviewContext || reviewContext.finalScore > existingReviewContext.finalScore) {
        reviewByDetectionId[detectionId] = reviewContext;
      }
    }
  }

  for (const row of clusterRows) {
    const detectionId = row.detection_id?.trim();
    if (!detectionId) continue;

    const clusterContext: AlertClusterContext = {
      clusterId: row.cluster_id?.trim() || null,
      clusterLabel: row.cluster_label?.trim() || null,
      clusterRank: toNumber(row.cluster_rank),
      clusterSize: toNumber(row.cluster_size),
      clusterMemberShare: toNullableNumber(row.cluster_member_share),
      clusterCount: toNumber(row.cluster_count),
      centroidDistance: toNullableNumber(row.cluster_centroid_distance),
      distanceThreshold: toNullableNumber(row.cluster_distance_threshold),
      clusterQuality: row.cluster_quality?.trim() || "noise",
      isExemplar: readBooleanFlag(row.is_cluster_exemplar),
      exemplarDetectionId: row.cluster_exemplar_detection_id?.trim() || null,
      embeddingModel: row.embedding_model?.trim() || "unknown",
      weeklyPeriodStart: row.period_start?.trim() || null,
      weeklyPeriodEnd: row.period_end?.trim() || null,
      locationType: row.location_type?.trim() || null,
      locationId: row.location_id?.trim() || null,
    };
    const existing = clusterByDetectionId[detectionId];
    if (
      !existing ||
      (existing.clusterQuality !== "clustered" && clusterContext.clusterQuality === "clustered")
    ) {
      clusterByDetectionId[detectionId] = clusterContext;
    }
    if (clusterContext.clusterQuality === "clustered") {
      clusteredDetections.add(detectionId);
    }
    if (clusterContext.isExemplar) {
      exemplarDetections.add(detectionId);
    }
  }

  const coverageLabel =
    linkedDetections.size > 0 && clusteredDetections.size > 0
      ? `${linkedDetections.size} detections are linked from weekly model evidence and ${clusteredDetections.size} clips carry persisted cluster assignments.`
      : linkedDetections.size > 0
        ? `${linkedDetections.size} detection previews are linked from the latest weekly model evidence.`
        : clusteredDetections.size > 0
          ? `${clusteredDetections.size} clips carry persisted embedding cluster assignments from the latest weekly run.`
          : clusterRows.length > 0
            ? "The latest cluster table has rows, but no clips formed stable persisted clusters in the current window."
            : "No detection-level model evidence is available in the latest weekly modeling run.";

  return {
    summary: {
      available: true,
      runId: parsedSummary?.run_id ?? null,
      generatedAt: parsedSummary?.generated_at ?? null,
      rowsScored: toNumber(parsedSummary?.stats?.rows_scored),
      alertsGreen: toNumber(parsedSummary?.stats?.alerts_green),
      alertsAmber: toNumber(parsedSummary?.stats?.alerts_amber),
      alertsRed: toNumber(parsedSummary?.stats?.alerts_red),
      sparseBaselineRows: toNumber(parsedSummary?.stats?.sparse_baseline_rows),
      queueRows: 0,
      linkedDetections: linkedDetections.size,
      clusterRows: toNumber(parsedSummary?.stats?.cluster_rows) || clusterRows.length,
      clusterExemplarRows:
        toNumber(parsedSummary?.stats?.cluster_exemplar_rows) || exemplarDetections.size,
      clusteredDetections: clusteredDetections.size,
      topReviewReasons: [],
      coverageLabel,
      trendFocusLabel: null,
      trendSeries: [],
      hotspots: [],
    },
    evaluation,
    reviewByDetectionId,
    clusterByDetectionId,
    anomalyRows,
  };
}

function contextHitCount(row: WeeklyEvidenceArtifactRow): number {
  return toNumber(row.sovon_lag_0_to_3mo_max_score ?? row.sovon_lag_0_to_8w_max_score) > 0
    ? 1
    : 0;
}

function weekLabel(periodStart: string): string {
  const parsed = new Date(`${periodStart}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return periodStart;

  return new Intl.DateTimeFormat("en-GB", {
    month: "short",
    day: "2-digit",
  }).format(parsed);
}

function chooseTrendFocusLabel(
  points: AlertVisualPoint[],
  anomalyRows: WeeklyEvidenceArtifactRow[],
  speciesFilter: string,
): string | null {
  const normalizedFilter = speciesFilter.trim().toLowerCase();
  if (normalizedFilter) {
    const exact = anomalyRows.find((row) =>
      row.species_english?.trim().toLowerCase().includes(normalizedFilter),
    );
    if (exact?.species_english) {
      return exact.species_english.trim();
    }
  }

  const counts = new Map<string, number>();
  for (const point of points) {
    counts.set(point.species, (counts.get(point.species) || 0) + 1);
  }

  const topSpecies = [...counts.entries()].sort((left, right) => right[1] - left[1])[0]?.[0];
  if (topSpecies) return topSpecies;

  return anomalyRows.find((row) => row.species_english?.trim())?.species_english?.trim() ?? null;
}

function buildTrendSeries(
  anomalyRows: WeeklyEvidenceArtifactRow[],
  focusLabel: string | null,
): AlertsTrendPoint[] {
  if (!focusLabel) return [];

  const focusRows = anomalyRows.filter(
    (row) => row.species_english?.trim() === focusLabel,
  );
  if (focusRows.length === 0) return [];

  const grouped = new Map<string, WeeklyEvidenceArtifactRow[]>();
  for (const row of focusRows) {
    const key = row.period_start?.trim();
    if (!key) continue;
    const current = grouped.get(key);
    if (current) current.push(row);
    else grouped.set(key, [row]);
  }

  return [...grouped.entries()]
    .sort((left, right) => left[0].localeCompare(right[0]))
    .slice(-6)
    .map(([periodStart, rows]) => ({
      weekLabel: weekLabel(periodStart),
      periodStart,
      rowCount: rows.length,
      meanFinalScore:
        rows.reduce((sum, row) => sum + toNumber(row.final_score), 0) / rows.length,
      meanEmbeddingDrift:
        rows.reduce((sum, row) => sum + toNumber(row.embedding_drift_score), 0) / rows.length,
      meanCountAnomaly:
        rows.reduce(
          (sum, row) =>
            sum + Math.max(toNumber(row.population_anomaly_score), toNumber(row.count_anomaly_score)),
          0,
        ) / rows.length,
      contextHits: rows.reduce((sum, row) => sum + contextHitCount(row), 0),
    }));
}

function buildHotspots(
  anomalyRows: WeeklyEvidenceArtifactRow[],
  visibleSpecies: Set<string>,
): AlertsHotspot[] {
  if (anomalyRows.length === 0) return [];

  const latestPeriod = anomalyRows
    .map((row) => row.period_start?.trim() || "")
    .filter(Boolean)
    .sort()
    .at(-1);
  if (!latestPeriod) return [];

  const latestRows = anomalyRows.filter((row) => {
    if (row.period_start?.trim() !== latestPeriod) return false;
    if (visibleSpecies.size === 0) return true;
    return visibleSpecies.has(row.species_english?.trim() || "");
  });
  if (latestRows.length === 0) return [];

  const speciesHotspots = [...latestRows.reduce((map, row) => {
    const key = row.species_english?.trim() || "Unknown species";
    const current = map.get(key) || [];
    current.push(row);
    map.set(key, current);
    return map;
  }, new Map<string, WeeklyEvidenceArtifactRow[]>()).entries()]
    .map(([label, rows]) => ({
      title: "Species hotspot",
      label,
      detail: `${rows.length} weekly rows · ${rows.filter((row) => contextHitCount(row) > 0).length} context-backed`,
      rowCount: rows.length,
      speciesCount: 1,
      meanFinalScore:
        rows.reduce((sum, row) => sum + toNumber(row.final_score), 0) / rows.length,
      contextHits: rows.reduce((sum, row) => sum + contextHitCount(row), 0),
    }))
    .sort((left, right) => right.contextHits - left.contextHits || right.meanFinalScore - left.meanFinalScore)
    .slice(0, 2);

  const gridHotspots = [...latestRows.reduce((map, row) => {
    const key = row.location_id?.trim() || "Unknown grid";
    const current = map.get(key) || [];
    current.push(row);
    map.set(key, current);
    return map;
  }, new Map<string, WeeklyEvidenceArtifactRow[]>()).entries()]
    .map(([label, rows]) => ({
      title: "Grid hotspot",
      label,
      detail: `${new Set(rows.map((row) => row.species_english?.trim() || "Unknown species")).size} species · ${rows.filter((row) => contextHitCount(row) > 0).length} context-backed`,
      rowCount: rows.length,
      speciesCount: new Set(rows.map((row) => row.species_english?.trim() || "Unknown species")).size,
      meanFinalScore:
        rows.reduce((sum, row) => sum + toNumber(row.final_score), 0) / rows.length,
      contextHits: rows.reduce((sum, row) => sum + contextHitCount(row), 0),
    }))
    .filter((hotspot) => hotspot.rowCount > 1 || hotspot.speciesCount > 1)
    .sort((left, right) => right.contextHits - left.contextHits || right.speciesCount - left.speciesCount || right.meanFinalScore - left.meanFinalScore)
    .slice(0, 2);

  return [...speciesHotspots, ...gridHotspots].slice(0, 4);
}

async function loadAlertsDataUncached(
  filters: ResolvedFilters,
  modelingArtifacts: ModelingArtifactsBundle,
  fallbackAvailableWeeks: readonly AlertsAvailableWeek[] = [],
  completeAvailableWeeks: readonly AlertsAvailableWeek[] = [],
): Promise<AlertsPageData> {
  const { whereClause, params } = buildWhereClause(filters);
  const { embeddingSelect, backendSelect } = buildEmbeddingSelection(filters.backend);
  const orderBy = resolveOrderBy(filters.sort);
  const featureArtifactSelect = `
      b.feature_artifacts->>'processed_clip' AS processed_clip,
      b.feature_artifacts->>'smooth_waveform' AS smooth_waveform,
      b.feature_artifacts->>'waveform' AS waveform,
      b.feature_artifacts->>'spectrogram' AS spectrogram`;

  const {
    summary: baseModeling,
    evaluation,
    reviewByDetectionId,
    clusterByDetectionId,
    anomalyRows,
  } = modelingArtifacts;
  // Prefer the complete modeled-week list (caller passes it when the run is
  // trusted) so the week picker is whole; detection payloads only cover recent
  // weeks, so collectAvailableWeeks(anomalyRows) would otherwise truncate it.
  const responseAvailableWeeks =
    completeAvailableWeeks.length > 0
      ? [...completeAvailableWeeks]
      : anomalyRows.length > 0
        ? collectAvailableWeeks(anomalyRows)
        : [...fallbackAvailableWeeks];
  const result = await query<QueryRow>(
    `
    SELECT
      b.detection_id,
      b.detected_at,
      b.observation_date,
      b.confidence,
      b.species_id,
      b.station_id,
      b.audio_url,
      rs.station_name,
      rs.station_state,
      rs.station_country,
      rs.lat AS station_lat,
      rs.lon AS station_lon,
      fn_point_to_cell(rs.lat, rs.lon, 'nl_geodesic_5km') AS grid_cell_id,
      sp.common_name,
      sp.scientific_name,
      sp.species_image_url,
      sp.species_thumbnail_url,
      ${featureArtifactSelect},
      ${embeddingSelect},
      ${backendSelect}
    FROM birdweather_tbl b
    LEFT JOIN species_tbl sp ON sp.species_id = b.species_id
    LEFT JOIN birdweather_stations_tbl rs ON rs.station_id = b.station_id
    ${whereClause}
    ORDER BY ${orderBy}
    LIMIT ${MAX_EMBEDDING_POINTS}
  `,
    params,
  );

  const matchedRows = result.rows.length;
  const scoredPoints: AlertVisualPoint[] = [];
  let unprojectedRows = 0;

  for (const row of result.rows) {
    const pc1Value = row.pc1 === null ? null : Number(row.pc1);
    const pc2Value = row.pc2 === null ? null : Number(row.pc2);
    const alertScoreValue =
      row.alert_score === null ? null : Number(row.alert_score);

    if (
      pc1Value === null ||
      pc2Value === null ||
      !Number.isFinite(pc1Value) ||
      !Number.isFinite(pc2Value)
    ) {
      // Row has an embedding but the precompute hasn't reached it yet —
      // skip so the canvas doesn't pile rows on the (0.5, 0.5) centre.
      unprojectedRows += 1;
      continue;
    }

    const alertScore =
      alertScoreValue !== null && Number.isFinite(alertScoreValue)
        ? Math.max(0, Math.min(1, alertScoreValue))
        : 0;

    const speciesId = row.species_id ? String(row.species_id) : null;
    const targetSpecies = speciesId
      ? TARGET_SPECIES_BY_BIRDWEATHER_ID.get(speciesId)
      : undefined;
    const species =
      (typeof row.common_name === "string" && row.common_name.trim()) ||
      targetSpecies?.english ||
      (typeof row.scientific_name === "string" && row.scientific_name.trim()) ||
      (row.species_id ? `Species ${row.species_id}` : "Unknown species");
    const scientificName =
      (typeof row.scientific_name === "string" && row.scientific_name.trim()) ||
      targetSpecies?.scientific ||
      null;

    const location =
      [row.station_state, row.station_country].filter(Boolean).join(", ") || null;
    const detectedAt = new Date(row.detected_at).toISOString();
    const embeddingBackend = row.embedding_backend || "unknown";

    scoredPoints.push({
      id: row.detection_id,
      speciesId,
      species,
      scientificName,
      imageUrl: row.species_image_url || row.species_thumbnail_url || null,
      station: row.station_name || row.station_id || "Unknown station",
      location,
      gridCellId: row.grid_cell_id || null,
      latitude: toNullableNumber(row.station_lat),
      longitude: toNullableNumber(row.station_lon),
      detectedAt,
      confidence: Number(row.confidence) || 0,
      embeddingBackend,
      audioUrls: {
        processed:
          artifactProxyUrl(row.processed_clip, "processed-audio") ||
          processedClipFallback(row),
        source: row.audio_url || null,
      },
      visualUrls: {
        waveform:
          artifactProxyUrl(row.smooth_waveform, "features") ||
          featureChartFallback(row, "waveforms", "_smooth"),
        waveformRaw:
          artifactProxyUrl(row.waveform, "features") ||
          featureChartFallback(row, "waveforms"),
        spectrogram:
          artifactProxyUrl(row.spectrogram, "features") ||
          featureChartFallback(row, "spectrograms"),
      },
      backgroundTags: [],
      reviewContext: reviewByDetectionId[row.detection_id] ?? null,
      clusterContext:
        clusterByDetectionId[row.detection_id]?.embeddingModel === embeddingBackend
          ? clusterByDetectionId[row.detection_id]
          : null,
      x: pc1Value,
      y: pc2Value,
      alertScore,
    });
  }

  const modeling: AlertsModelingSummary = {
    ...baseModeling,
    trendFocusLabel: null,
    trendSeries: [],
    hotspots: [],
  };

  if (scoredPoints.length === 0) {
    return {
      points: [],
      referencePoints: [],
      availableWeeks: responseAvailableWeeks,
      stats: {
        matchedRows,
        loadedRows: 0,
        scoredPoints: 0,
        displayedPoints: 0,
        speciesCount: 0,
        stationCount: 0,
        // Legacy fields retained for type compatibility with AlertsClient.
        // The precompute path no longer tracks raw embedding dimensions.
        dominantDimension: 0,
        dimensionFilteredRows: unprojectedRows,
        hiddenByAlertLevel: 0,
        meanIsolation: 0,
        maxIsolation: 0,
        timeWindowLabel: filters.rangeLabel,
      },
      filters,
      modeling,
      evaluation,
      modelPerformance: null,
    };
  }

  const visibleSpecies = new Set(scoredPoints.map((point) => point.species));
  const trendFocusLabel = chooseTrendFocusLabel(scoredPoints, anomalyRows, filters.species);
  modeling.trendFocusLabel = trendFocusLabel;
  modeling.trendSeries = buildTrendSeries(anomalyRows, trendFocusLabel);
  modeling.hotspots = buildHotspots(anomalyRows, visibleSpecies);

  const points = scoredPoints.filter(
    (point) => point.alertScore >= filters.alertThreshold,
  );
  const visibleScores = points.length > 0 ? points : scoredPoints;
  const meanIsolation =
    visibleScores.length > 0
      ? visibleScores.reduce((sum, point) => sum + point.alertScore, 0) / visibleScores.length
      : 0;
  const maxIsolation = visibleScores.reduce(
    (max, point) => Math.max(max, point.alertScore),
    0,
  );

  return {
    points,
    referencePoints: scoredPoints,
    availableWeeks: responseAvailableWeeks,
    stats: {
      matchedRows,
      loadedRows: scoredPoints.length + unprojectedRows,
      scoredPoints: scoredPoints.length,
      displayedPoints: points.length,
      speciesCount: new Set(points.map((point) => point.species)).size,
      stationCount: new Set(points.map((point) => point.station)).size,
      // Legacy fields retained for type compatibility with AlertsClient.
      // The precompute path no longer tracks raw embedding dimensions; the
      // count of rows skipped because the projection hasn't reached them yet
      // is exposed via dimensionFilteredRows so the UI can still surface it.
      dominantDimension: 0,
      dimensionFilteredRows: unprojectedRows,
      hiddenByAlertLevel: scoredPoints.length - points.length,
      meanIsolation,
      maxIsolation,
      timeWindowLabel: filters.rangeLabel,
    },
    filters,
    modeling,
    evaluation,
    modelPerformance: null,
  };
}

type AlertsRequestCacheContext = {
  filters: ResolvedFilters;
  modelingManifest: ModelingArtifactManifest;
  coverage: AlertsCoverageState;
  artifactCoverageTrusted: boolean;
  artifactCoverageReason: string | null;
  // Complete modeled-week list (from the cheap DISTINCT query) so the week
  // picker stays whole even though detection-level payloads load only recent
  // weeks. See loadAlertsDataUncached's responseAvailableWeeks.
  availableWeeks: readonly AlertsAvailableWeek[];
};

type AlertsRequestContext = {
  modelingArtifacts: ModelingArtifactsBundle;
} & AlertsRequestCacheContext;

async function resolveAlertsRequestCacheContext(
  searchParams?: Record<string, SearchParamValue>,
): Promise<AlertsRequestCacheContext> {
  const modelingManifest = await buildModelingArtifactManifest();
  const shellArtifacts = await loadAlertsShellArtifacts(modelingManifest);
  const availableWeeks = shellArtifacts.availableWeeks;
  const coverage = await loadExplicitRangeAlertsCoverage(availableWeeks);
  const artifactCoverage = assessAlertsArtifactCoverage({
    availableWeeks,
    latestDetectedAt: coverage.latestDetectedAt,
    latestEmbeddedDetectedAt: coverage.latestEmbeddedDetectedAt,
  });
  const defaultableAvailableWeeks = selectDefaultableAvailableWeeks({
    availableWeeks,
    coverage,
    artifactCoverageTrusted: artifactCoverage.trusted,
  });
  const defaultDateWindow = await loadDefaultAlertsDateWindow(
    coverage,
    defaultableAvailableWeeks,
  );
  const filters = normalizeFilters(
    defaultDateWindow,
    searchParams,
    defaultableAvailableWeeks,
    availableWeeks,
  );

  return {
    filters,
    modelingManifest,
    coverage,
    artifactCoverageTrusted: artifactCoverage.trusted,
    artifactCoverageReason: artifactCoverage.reason ?? null,
    availableWeeks,
  };
}

async function resolveAlertsRequestContextFromCacheContext(
  requestContext: AlertsRequestCacheContext,
): Promise<AlertsRequestContext> {
  const modelingArtifacts = await loadModelingArtifacts(requestContext.modelingManifest);
  const trustedModelingArtifacts = requestContext.artifactCoverageTrusted
    ? modelingArtifacts
    : downgradeModelingArtifactsBundle(
        modelingArtifacts,
        requestContext.artifactCoverageReason ??
          "Ignored the persisted weekly modeling run because it is out of sync with the live database.",
      );

  return {
    ...requestContext,
    modelingArtifacts: trustedModelingArtifacts,
  };
};

async function resolveAlertsRequestContext(
  searchParams?: Record<string, SearchParamValue>,
): Promise<AlertsRequestContext> {
  const requestContext = await resolveAlertsRequestCacheContext(searchParams);
  return resolveAlertsRequestContextFromCacheContext(requestContext);
}

export async function loadAlertsData(
  searchParams?: Record<string, SearchParamValue>,
): Promise<AlertsPageData> {
  const requestContext = await resolveAlertsRequestCacheContext(searchParams);
  const {
    filters,
    modelingManifest,
    coverage,
    artifactCoverageTrusted,
    availableWeeks,
  } = requestContext;
  const cacheKey = buildAlertsDataCacheKey(
    filters,
    `${modelingManifest.cacheKey}:${buildAlertsCoverageCacheKey(coverage)}:${artifactCoverageTrusted ? "trusted" : "untrusted"}`,
  );
  const cached = readAlertsDataCache(cacheKey);

  if (cached) {
    return cached;
  }

  const pending = alertsRuntimeState.alertsDataPending.get(cacheKey);
  if (pending) {
    return pending;
  }

  const diskCached = await readAlertsDataDiskCache(cacheKey);
  if (diskCached) {
    return diskCached;
  }

  const { modelingArtifacts } = await resolveAlertsRequestContextFromCacheContext(requestContext);

  const fallbackAvailableWeeks = artifactCoverageTrusted
    ? []
    : coverage.availableEmbeddedWeeks;
  // When the run is trusted, expose the full modeled-week list to the picker
  // even though detection payloads load only recent weeks. When untrusted, leave
  // this empty so the existing embedded-week fallback still drives the response.
  const completeAvailableWeeks = artifactCoverageTrusted ? availableWeeks : [];
  const promise = Promise.resolve(
    loadAlertsDataUncached(
      filters,
      modelingArtifacts,
      fallbackAvailableWeeks,
      completeAvailableWeeks,
    ),
  )
    .then((value) => {
      writeAlertsDataCache(cacheKey, value);
      void writeAlertsDataDiskCache(cacheKey, value);
      return value;
    });

  alertsRuntimeState.alertsDataPending.set(cacheKey, promise);

  try {
    return await promise;
  } finally {
    alertsRuntimeState.alertsDataPending.delete(cacheKey);
  }
}

export async function loadAlertsShellData(
  searchParams?: Record<string, SearchParamValue>,
): Promise<AlertsPageData> {
  const modelingManifest = await buildModelingArtifactManifest();
  const shellArtifacts = await loadAlertsShellArtifacts(modelingManifest);
  const coverage =
    readCachedAlertsCoverage() ??
    (await loadExplicitRangeAlertsCoverage(shellArtifacts.availableWeeks));
  const artifactCoverage = assessAlertsArtifactCoverage({
    availableWeeks: shellArtifacts.availableWeeks,
    latestDetectedAt: coverage.latestDetectedAt,
    latestEmbeddedDetectedAt: coverage.latestEmbeddedDetectedAt,
  });
  const defaultableAvailableWeeks = selectDefaultableAvailableWeeks({
    availableWeeks: shellArtifacts.availableWeeks,
    coverage,
    artifactCoverageTrusted: artifactCoverage.trusted,
  });
  const defaultDateWindow = await loadDefaultAlertsDateWindow(
    coverage,
    defaultableAvailableWeeks,
  );
  const filters = normalizeFilters(
    defaultDateWindow,
    searchParams,
    defaultableAvailableWeeks,
    shellArtifacts.availableWeeks,
  );
  const artifactCoverageTrusted = artifactCoverage.trusted;
  const cacheKey = buildAlertsDataCacheKey(
    filters,
    `${modelingManifest.cacheKey}:${buildAlertsCoverageCacheKey(coverage)}:${artifactCoverageTrusted ? "trusted" : "untrusted"}:shell`,
  );
  const cached = readAlertsShellCache(cacheKey);

  if (cached) {
    return cached;
  }

  const pending = alertsRuntimeState.alertsShellPending.get(cacheKey);
  if (pending) {
    return pending;
  }

  const promise = Promise.resolve().then(() => {
    const modeling = artifactCoverageTrusted
      ? shellArtifacts.summary
      : downgradeShellModelingSummary(
          shellArtifacts.summary,
          artifactCoverage.reason ??
            "Ignored the persisted weekly modeling run because it is out of sync with the live database.",
        );

    return {
      points: [],
      referencePoints: [],
      availableWeeks: artifactCoverageTrusted
        ? shellArtifacts.availableWeeks
        : coverage.availableEmbeddedWeeks,
      stats: {
        matchedRows: 0,
        loadedRows: 0,
        scoredPoints: 0,
        displayedPoints: 0,
        speciesCount: 0,
        stationCount: 0,
        dominantDimension: 0,
        dimensionFilteredRows: 0,
        hiddenByAlertLevel: 0,
        meanIsolation: 0,
        maxIsolation: 0,
        timeWindowLabel: filters.rangeLabel,
      },
      filters,
      modeling,
      evaluation: emptyEvaluationData(
        modeling.runId,
        modeling.generatedAt,
        artifactCoverageTrusted
          ? "Trend evaluation loads when the Evaluation tab opens."
          : `${artifactCoverage.reason ?? "Persisted modeling coverage is stale."} Alerts is hiding trend evaluation until the modeling run realigns with the live database.`,
      ),
      modelPerformance: null,
    };
  }).then((value) => {
    writeAlertsShellCache(cacheKey, value);
    return value;
  });

  alertsRuntimeState.alertsShellPending.set(cacheKey, promise);

  try {
    return await promise;
  } finally {
    alertsRuntimeState.alertsShellPending.delete(cacheKey);
  }
}

export async function loadAlertsEvaluationData(
  searchParams?: Record<string, SearchParamValue>,
): Promise<AlertsEvaluationData> {
  const { modelingArtifacts } = await resolveAlertsRequestContext(searchParams);
  return modelingArtifacts.evaluation;
}

// Reads and parses the latest validation report (which now carries the
// `spatial_validation` section and `scoring_mode`). Returns null when no
// complete modeling run has a report so the UI can fall back gracefully.
export async function loadAlertsModelPerformance(
  _searchParams?: Record<string, SearchParamValue>,
): Promise<ModelPerformanceReport | null> {
  const rawJson = await loadLatestModelPerformanceJson();
  return parseModelPerformanceReport(rawJson);
}
