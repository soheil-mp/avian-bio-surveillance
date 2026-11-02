import { mkdir, readFile, writeFile } from "fs/promises";
import { join } from "path";

import { unstable_cache } from "next/cache";

import {
  TARGET_SPECIES_BY_BIRDWEATHER_ID,
  TARGET_SPECIES_LIST,
} from "@/config/target-species";
import { query } from "@/lib/db";
import { loadWeeklyAnomalyScoreForDetection } from "@/lib/modeling-db";
import { latestVisualArtifactDate, presignedGetObjectUrl } from "@/lib/minio";
import { readFileMeasured } from "@/lib/server-fs";

import {
  type ExploreAnomalySource,
  buildDetectedAtBounds,
  buildExploreMediaPlan,
  buildVisualUrls,
  extractBackgroundTags,
  readDetectionAnomalyScore,
  readConfidenceParam,
  readDateParam,
  readPositiveIntParam,
  readSearchParam,
  toFiniteNumber,
  type ExploreDetectionRow,
  type SearchParamValue,
} from "./server";

type ExploreSearchParams = { [key: string]: SearchParamValue };
type ExploreSort =
  | "date-desc"
  | "date-asc"
  | "confidence-desc"
  | "confidence-asc"
  | "species-asc"
  | "species-desc";

// What a detection must have to appear in a given view.
//   "visuals" — audio + spectrogram + waveform all already materialized in
//               object storage (recorded in feature_artifacts). Used by the
//               default inventory so it opens on fully-renderable detections.
//   "clip"    — a processed clip exists (audio plays); visuals render when
//               present and fall back to on-demand generation otherwise. Used
//               by filtered views so they stay browsable.
type ExploreInventoryGate = "visuals" | "clip";

type ExploreNormalizedRequest = {
  page: number;
  pageSize: number;
  offset: number;
  species: string | null;
  station: string | null;
  location: string | null;
  minConf: number | null;
  dateFrom: string | null;
  dateTo: string | null;
  detectedAtFrom: string | null;
  detectedAtToExclusive: string | null;
  processedOnly: boolean;
  sort: ExploreSort;
};

type ExploreSpeciesLookupRow = {
  species_id: string;
};

type ExploreStationLookupRow = {
  station_id: string;
};

type ExploreLatestDetectionRow = {
  latest_detected_at: string | null;
};

export type ExploreFilterOptions = {
  species: string[];
  stations: string[];
  locations: string[];
};

export type ExploreClientDetection = {
  id: string;
  speciesId: string | null;
  time: string;
  species: string;
  scientificName: string | null;
  confidence: number;
  station: string;
  location: string | null;
  imageUrl: string | null;
  thumbnailUrl: string | null;
  speciesColor: string | null;
  startTime: number | null;
  endTime: number | null;
  clipDuration: number | null;
  anomalyScore: number | null;
  anomalySource: ExploreAnomalySource;
  bgTags: Array<{ label: string; score: number }>;
  audioUrls: {
    raw: string | null;
    processed: string | null;
    clip: string | null;
    direct: string | null;
  };
  visualUrls: {
    waveform: string | null;
    spectrogram: string | null;
  };
};

export type ExplorePageData = {
  data: ExploreClientDetection[];
  currentPage: number;
  hasNextPage: boolean;
  pageSize: number;
  stationNotInDb: boolean;
  loadFailed: boolean;
  options: ExploreFilterOptions;
};

type ExplorePageCacheSnapshot = {
  version: number;
  createdAt: string;
  payload: ExplorePageData;
};

const DEFAULT_PAGE_SIZE = 20;
const DEFAULT_SORT: ExploreSort = "date-desc";
const EXPLORE_CACHE_SCHEMA_VERSION = 4;
const ADAPTIVE_LOOKBACK_WINDOWS_DAYS = [14, 60, 365] as const;
const ADAPTIVE_LOOKAHEAD_PAD_DAYS = 60;
const DEFAULT_EXPLORE_CACHE_TTL_MS = 15 * 60 * 1000;
const DEFAULT_EXPLORE_CACHE_PATH = join(
  process.cwd(),
  ".cache",
  "explore-default-page.json",
);
const TARGET_SPECIES_ID_BY_ENGLISH = new Map(
  TARGET_SPECIES_LIST.map((species) => [
    species.english.trim().toLowerCase(),
    String(species.birdweatherId),
  ]),
);
const TARGET_SPECIES_ID_BY_SCIENTIFIC = new Map(
  TARGET_SPECIES_LIST.map((species) => [
    species.scientific.trim().toLowerCase(),
    String(species.birdweatherId),
  ]),
);
// Bioacoustic Explore is scoped to the watchlist species the modeling pipeline
// actually scores, matching the /alerts dataset.
const TARGET_SPECIES_IDS = TARGET_SPECIES_LIST.map((species) =>
  String(species.birdweatherId),
);

let defaultExplorePageCache: ExplorePageCacheSnapshot | null = null;
let defaultExplorePageRefreshPromise: Promise<ExplorePageData | null> | null =
  null;
const weeklyAnomalyScoreCache = new Map<string, Promise<number | null>>();

function logExploreLoadFailure(stage: string, error: unknown) {
  console.warn(`[explore] failed to load ${stage}`, error);
}

function logExploreTimings(details: {
  optionsMs: number;
  lookupsMs: number;
  queryMs: number;
  mapMs: number;
  totalMs: number;
  rows: number;
}) {
  if (process.env.NODE_ENV === "production") {
    return;
  }

  console.info("[explore] load timings", details);
}

function isDefaultExploreRequest(filters: ExploreNormalizedRequest): boolean {
  return (
    filters.page === 1 &&
    filters.offset === 0 &&
    filters.pageSize === DEFAULT_PAGE_SIZE &&
    filters.species == null &&
    filters.station == null &&
    filters.location == null &&
    filters.minConf == null &&
    filters.dateFrom == null &&
    filters.dateTo == null &&
    filters.detectedAtFrom == null &&
    filters.detectedAtToExclusive == null &&
    filters.processedOnly &&
    filters.sort === DEFAULT_SORT
  );
}

function isCacheFresh(snapshot: ExplorePageCacheSnapshot): boolean {
  const createdAt = Date.parse(snapshot.createdAt);
  return Number.isFinite(createdAt)
    ? Date.now() - createdAt <= DEFAULT_EXPLORE_CACHE_TTL_MS
    : false;
}

function withExploreOptions(
  payload: ExplorePageData,
  options: ExploreFilterOptions,
): ExplorePageData {
  return {
    ...payload,
    options,
  };
}

function normalizeExploreScore(value: unknown): number | null {
  const numeric = toFiniteNumber(value);
  return numeric == null ? null : Math.min(Math.max(numeric, 0), 1);
}

function parseSpeciesIdFromAssetUrl(value: string | null | undefined): string | null {
  if (typeof value !== "string") {
    return null;
  }

  const matched = value.match(/\/species\/(\d+)\//);
  return matched?.[1] ?? null;
}

function resolveClientSpeciesId(detection: ExploreClientDetection): string | null {
  if (detection.speciesId) {
    return detection.speciesId;
  }

  return (
    parseSpeciesIdFromAssetUrl(detection.imageUrl) ??
    parseSpeciesIdFromAssetUrl(detection.thumbnailUrl) ??
    (detection.scientificName
      ? TARGET_SPECIES_ID_BY_SCIENTIFIC.get(
          detection.scientificName.trim().toLowerCase(),
        ) ?? null
      : null) ??
    TARGET_SPECIES_ID_BY_ENGLISH.get(detection.species.trim().toLowerCase()) ??
    null
  );
}

async function loadWeeklyAnomalyScore(
  speciesId: string | null,
  detectionId: string,
): Promise<number | null> {
  if (!speciesId) {
    return null;
  }

  const cacheKey = `${speciesId}:${detectionId}`;
  const cached = weeklyAnomalyScoreCache.get(cacheKey);
  if (cached) {
    return cached;
  }

  const pending = (async () => {
    try {
      return await loadWeeklyAnomalyScoreForDetection(speciesId, detectionId);
    } catch {
      return null;
    }
  })();

  weeklyAnomalyScoreCache.set(cacheKey, pending);
  pending.catch(() => {
    weeklyAnomalyScoreCache.delete(cacheKey);
  });
  return pending;
}

async function resolveExploreAnomalyScore({
  detectionId,
  speciesId,
  detectionScore,
}: {
  detectionId: string;
  speciesId: string | null;
  detectionScore: number | null;
}): Promise<{ anomalyScore: number | null; anomalySource: ExploreAnomalySource }> {
  if (detectionScore != null) {
    return {
      anomalyScore: detectionScore,
      anomalySource: "detection",
    };
  }

  const weeklyScore = await loadWeeklyAnomalyScore(speciesId, detectionId);
  if (weeklyScore != null) {
    return {
      anomalyScore: weeklyScore,
      anomalySource: "weekly",
    };
  }

  return {
    anomalyScore: null,
    anomalySource: null,
  };
}

function extractObservationDateFromMediaUrl(value: string | null): string | null {
  if (typeof value !== "string") {
    return null;
  }

  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }

  try {
    const decodedPath = decodeURIComponent(new URL(trimmed).pathname);
    const matched = decodedPath.match(/\/(\d{4}-\d{2}-\d{2})\//);
    return matched?.[1] ?? null;
  } catch {
    const matched = trimmed.match(/\/(\d{4}-\d{2}-\d{2})\//);
    return matched?.[1] ?? null;
  }
}

async function sanitizeCachedDetection(
  detection: ExploreClientDetection,
): Promise<ExploreClientDetection> {
  const observationDate =
    extractObservationDateFromMediaUrl(detection.audioUrls.processed) ??
    extractObservationDateFromMediaUrl(detection.audioUrls.raw) ??
    extractObservationDateFromMediaUrl(detection.audioUrls.clip);
  const speciesId = resolveClientSpeciesId(detection);
  const hasProcessedClip = Boolean(
    detection.audioUrls.processed || detection.audioUrls.clip,
  );

  // Re-presign every object-storage URL from its stored path so the durable
  // on-disk snapshot serves working media indefinitely (presigned URLs expire
  // after an hour). External/relative URLs (BirdWeather CDN, the on-demand
  // artifact route) are returned untouched. The cached anomaly score is taken
  // as-is to keep this hot path free of per-detection DB round-trips.
  const [raw, processed, clip, waveform, spectrogram] = await Promise.all([
    refreshStoredMediaUrl(detection.audioUrls.raw),
    refreshStoredMediaUrl(detection.audioUrls.processed),
    refreshStoredMediaUrl(detection.audioUrls.clip),
    refreshStoredMediaUrl(detection.visualUrls.waveform),
    refreshStoredMediaUrl(detection.visualUrls.spectrogram),
  ]);

  return {
    ...detection,
    speciesId,
    anomalyScore: normalizeExploreScore(detection.anomalyScore),
    anomalySource: detection.anomalySource,
    audioUrls: {
      ...detection.audioUrls,
      raw,
      processed,
      clip,
    },
    visualUrls: buildVisualUrls({
      waveformUrl: waveform,
      spectrogramUrl: spectrogram,
      detectionId: detection.id,
      hasProcessedClip,
      observationDate,
    }),
  };
}

async function migrateExplorePageCacheSnapshot(
  snapshot: ExplorePageCacheSnapshot,
): Promise<ExplorePageCacheSnapshot | null> {
  if (typeof snapshot.createdAt !== "string" || !snapshot.payload) {
    return null;
  }

  const data = Array.isArray(snapshot.payload.data)
    ? await Promise.all(snapshot.payload.data.map(sanitizeCachedDetection))
    : [];

  return {
    version: EXPLORE_CACHE_SCHEMA_VERSION,
    createdAt: snapshot.createdAt,
    payload: {
      ...snapshot.payload,
      data,
    },
  };
}

async function readDefaultExplorePageCache(): Promise<ExplorePageCacheSnapshot | null> {
  if (defaultExplorePageCache) {
    return defaultExplorePageCache;
  }

  try {
    const raw = await readFile(DEFAULT_EXPLORE_CACHE_PATH, "utf-8");
    const parsed = JSON.parse(raw) as ExplorePageCacheSnapshot;
    if (!parsed) {
      return null;
    }

    const migratedSnapshot = await migrateExplorePageCacheSnapshot(parsed);
    if (!migratedSnapshot) {
      return null;
    }

    defaultExplorePageCache = migratedSnapshot;

    if (parsed.version !== EXPLORE_CACHE_SCHEMA_VERSION) {
      void writeDefaultExplorePageCache(migratedSnapshot.payload);
    }

    return migratedSnapshot;
  } catch {
    return null;
  }
}

async function writeDefaultExplorePageCache(payload: ExplorePageData): Promise<void> {
  const snapshot: ExplorePageCacheSnapshot = {
    version: EXPLORE_CACHE_SCHEMA_VERSION,
    createdAt: new Date().toISOString(),
    payload,
  };

  defaultExplorePageCache = snapshot;

  try {
    await mkdir(join(process.cwd(), ".cache"), { recursive: true });
    await writeFile(
      DEFAULT_EXPLORE_CACHE_PATH,
      JSON.stringify(snapshot),
      "utf-8",
    );
  } catch (error) {
    logExploreLoadFailure("default explore cache write", error);
  }
}

export async function loadExploreBootstrapPayload(
  searchParams: ExploreSearchParams,
): Promise<ExplorePageData | null> {
  const filters = normalizeExploreRequest(searchParams);
  if (!isDefaultExploreRequest(filters)) {
    return null;
  }

  const cachedSnapshot = await readDefaultExplorePageCache();
  if (!cachedSnapshot) {
    return null;
  }

  // Re-presign on serve (see loadExplorePageData) so the SSR-injected initial
  // payload never ships expired object-storage URLs to the client.
  const data = await Promise.all(
    cachedSnapshot.payload.data.map(sanitizeCachedDetection),
  );
  return { ...cachedSnapshot.payload, data };
}

async function presignObjectPath(
  bucket: "processed-audio" | "raw-audio" | "features",
  objectPath: string | null,
): Promise<string | null> {
  if (!objectPath || objectPath.includes("..")) return null;

  try {
    return await presignedGetObjectUrl(bucket, objectPath);
  } catch {
    return null;
  }
}

const PRESIGNABLE_BUCKETS = new Set<"processed-audio" | "raw-audio" | "features">([
  "processed-audio",
  "raw-audio",
  "features",
]);

// Recover {bucket, objectPath} from a stored object-storage URL so it can be
// re-presigned. Returns null for relative URLs (e.g. the on-demand artifact
// route) and for external URLs (e.g. the BirdWeather CDN), which must be left
// untouched.
function parseStoredObjectRef(
  value: string | null,
): { bucket: "processed-audio" | "raw-audio" | "features"; objectPath: string } | null {
  if (typeof value !== "string" || !value.trim()) return null;

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }

  const path = decodeURIComponent(parsed.pathname).replace(/^\/+/, "");
  const slash = path.indexOf("/");
  if (slash <= 0) return null;

  const bucket = path.slice(0, slash);
  const objectPath = path.slice(slash + 1);
  if (
    !PRESIGNABLE_BUCKETS.has(bucket as "processed-audio" | "raw-audio" | "features") ||
    !objectPath ||
    objectPath.includes("..")
  ) {
    return null;
  }

  return {
    bucket: bucket as "processed-audio" | "raw-audio" | "features",
    objectPath,
  };
}

// Presigned object-storage URLs expire after an hour, so any URL baked into the
// persisted default-page snapshot is stale on read. Re-presign it from its
// object path (the path survives in the URL even after the signature expires).
// Relative/external URLs are returned untouched.
async function refreshStoredMediaUrl(value: string | null): Promise<string | null> {
  if (value == null) return null;
  const ref = parseStoredObjectRef(value);
  if (!ref) return value;
  return (await presignObjectPath(ref.bucket, ref.objectPath)) ?? value;
}

export const loadExploreFilterOptions = unstable_cache(
  async (): Promise<ExploreFilterOptions> => {
    try {
      const raw = await readFileMeasured(
        join(process.cwd(), "public", "data", "explore_options.json"),
        "utf-8",
      );
      const opts = JSON.parse(raw) as {
        species?: string[];
        stations?: string[];
        locations?: string[];
      };

      return {
        species: Array.isArray(opts.species) ? opts.species : [],
        stations: Array.isArray(opts.stations) ? opts.stations : [],
        locations: Array.isArray(opts.locations) ? opts.locations : [],
      };
    } catch {
      try {
        const [speciesRes, stationRes, locationRes] = await Promise.all([
          query<{ common_name: string }>(
            `SELECT common_name
             FROM species_tbl
             WHERE common_name IS NOT NULL
             ORDER BY common_name`,
          ),
          query<{ station_name: string }>(
            `SELECT station_name
             FROM birdweather_stations_tbl
             WHERE station_name IS NOT NULL
             ORDER BY station_name`,
          ),
          query<{ station_state: string | null; station_country: string | null }>(
            `SELECT DISTINCT station_state, station_country
             FROM birdweather_stations_tbl
             WHERE station_state IS NOT NULL OR station_country IS NOT NULL
             ORDER BY station_state, station_country`,
          ),
        ]);

        return {
          species: speciesRes.rows.map((row) => row.common_name),
          stations: stationRes.rows.map((row) => row.station_name),
          locations: locationRes.rows
            .map((row) =>
              [row.station_state, row.station_country].filter(Boolean).join(", "),
            )
            .filter((value, index, values) => values.indexOf(value) === index)
            .sort(),
        };
      } catch (error) {
        logExploreLoadFailure("filter options", error);
        return {
          species: [],
          stations: [],
          locations: [],
        };
      }
    }
  },
  ["explore-filter-options"],
  { revalidate: 3600 },
);

const loadLatestDetectionDate = unstable_cache(
  async (): Promise<string | null> => {
    const result = await query<ExploreLatestDetectionRow>(
      `SELECT MAX(last_detection)::text AS latest_detected_at
       FROM birdweather_stations_tbl
       WHERE last_detection IS NOT NULL`,
    );

    return result.rows[0]?.latest_detected_at ?? null;
  },
  ["explore-latest-detection-date"],
  { revalidate: 60 },
);

// Newest day whose detections already have audio + spectrogram + waveform
// materialized. Resolved from object storage (cheap LIST), not the hypertable.
const loadLatestVisualArtifactDate = unstable_cache(
  async (): Promise<string | null> => {
    try {
      return await latestVisualArtifactDate();
    } catch {
      return null;
    }
  },
  ["explore-latest-visual-artifact-date"],
  { revalidate: 300 },
);

function normalizeExploreRequest(
  searchParams: ExploreSearchParams,
): ExploreNormalizedRequest {
  const page = readPositiveIntParam(searchParams.page, 1);
  const pageSize = DEFAULT_PAGE_SIZE;
  const sortCandidate = readSearchParam(searchParams.sort);
  const sort =
    sortCandidate === "date-asc" ||
    sortCandidate === "confidence-desc" ||
    sortCandidate === "confidence-asc" ||
    sortCandidate === "species-asc" ||
    sortCandidate === "species-desc"
      ? sortCandidate
      : DEFAULT_SORT;
  const dateFrom = readDateParam(searchParams.dateFrom);
  const dateTo = readDateParam(searchParams.dateTo);
  const { detectedAtFrom, detectedAtToExclusive } = buildDetectedAtBounds(
    dateFrom,
    dateTo,
  );

  return {
    page,
    pageSize,
    offset: (page - 1) * pageSize,
    species: readSearchParam(searchParams.species),
    station: readSearchParam(searchParams.station),
    location: readSearchParam(searchParams.location),
    minConf: readConfidenceParam(searchParams.minConf),
    dateFrom,
    dateTo,
    detectedAtFrom,
    detectedAtToExclusive,
    processedOnly: readSearchParam(searchParams.audio) !== "all",
    sort,
  };
}

async function resolveSpeciesId(species: string): Promise<string | null> {
  const result = await query<ExploreSpeciesLookupRow>(
    `SELECT species_id
     FROM species_tbl
     WHERE lower(common_name) = lower($1)
     ORDER BY species_id
     LIMIT 1`,
    [species],
  );

  return result.rows[0]?.species_id ?? null;
}

async function resolveStationIds(
  station: string | null,
  location: string | null,
): Promise<string[]> {
  const params: string[] = [];
  const whereParts: string[] = [];

  if (station) {
    params.push(station);
    whereParts.push(`station_name = $${params.length}`);
  }

  if (location) {
    params.push(location);
    whereParts.push(
      `CONCAT_WS(', ', station_state, station_country) = $${params.length}`,
    );
  }

  if (whereParts.length === 0) {
    return [];
  }

  const result = await query<ExploreStationLookupRow>(
    `SELECT station_id
     FROM birdweather_stations_tbl
     WHERE ${whereParts.join(" AND ")}
     ORDER BY station_id`,
    params,
  );

  return result.rows.map((row) => row.station_id);
}

function buildSortOrder(sort: ExploreSort): {
  orderBy: string;
  fastPathOrderBy: string;
  requiresSpeciesSort: boolean;
  pagedSelectColumns: string;
} {
  const sortMap: Record<ExploreSort, string> = {
    "date-desc": "b.detected_at DESC",
    "date-asc": "b.detected_at ASC",
    "confidence-desc": "b.confidence DESC",
    "confidence-asc": "b.confidence ASC",
    "species-asc": "sp.common_name ASC",
    "species-desc": "sp.common_name DESC",
  };
  const orderBy = sortMap[sort];

  return {
    orderBy,
    fastPathOrderBy: orderBy.replace(/\bb\./g, "p."),
    requiresSpeciesSort: orderBy.startsWith("sp."),
    pagedSelectColumns:
      sort === "confidence-desc" || sort === "confidence-asc"
        ? ", b.confidence"
        : "",
  };
}

function buildLookbackStartIso(
  latestDetectedAt: string,
  lookbackDays: number,
): string | null {
  const parsed = new Date(`${latestDetectedAt}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }

  parsed.setUTCDate(parsed.getUTCDate() - (lookbackDays - 1));
  return parsed.toISOString();
}

function shiftDateIso(dateValue: string, dayOffset: number): string | null {
  const parsed = new Date(`${dateValue}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }

  parsed.setUTCDate(parsed.getUTCDate() + dayOffset);
  return parsed.toISOString();
}

function shouldUseAdaptiveLookback(filters: ExploreNormalizedRequest): boolean {
  return (
    filters.sort === "date-desc" &&
    filters.dateFrom == null &&
    filters.dateTo == null
  );
}

async function executeDetectionQuery({
  filters,
  speciesId,
  stationIds,
  limit,
  offset,
  gate = "clip",
  detectedAtFromOverride = null,
  detectedAtToExclusiveOverride = null,
}: {
  filters: ExploreNormalizedRequest;
  speciesId: string | null;
  stationIds: string[] | null;
  limit: number;
  offset: number;
  gate?: ExploreInventoryGate;
  detectedAtFromOverride?: string | null;
  detectedAtToExclusiveOverride?: string | null;
}): Promise<ExploreDetectionRow[]> {
  const queryParams: Array<string | number | string[]> = [];
  const whereParts: string[] = [];

  if (filters.minConf != null) {
    queryParams.push(filters.minConf);
    whereParts.push(`b.confidence >= $${queryParams.length}`);
  }

  if (filters.dateFrom) {
    queryParams.push(filters.dateFrom);
    whereParts.push(`b.observation_date >= $${queryParams.length}`);
  }

  const effectiveDetectedAtFrom =
    detectedAtFromOverride ?? filters.detectedAtFrom;

  if (effectiveDetectedAtFrom) {
    queryParams.push(effectiveDetectedAtFrom);
    whereParts.push(`b.detected_at >= $${queryParams.length}`);
  }

  if (filters.dateTo) {
    queryParams.push(filters.dateTo);
    whereParts.push(`b.observation_date <= $${queryParams.length}`);
  }

  const effectiveDetectedAtToExclusive =
    detectedAtToExclusiveOverride ?? filters.detectedAtToExclusive;

  if (effectiveDetectedAtToExclusive) {
    queryParams.push(effectiveDetectedAtToExclusive);
    whereParts.push(`b.detected_at < $${queryParams.length}`);
  }

  if (filters.processedOnly) {
    whereParts.push("b.has_processed_clip = TRUE");
  }

  // Only the watchlist species the modeling pipeline scores (matches /alerts).
  queryParams.push(TARGET_SPECIES_IDS);
  whereParts.push(`b.species_id = ANY($${queryParams.length}::text[])`);

  if (gate === "visuals") {
    // Default inventory: only detections whose audio + spectrogram + waveform
    // are all already materialized in object storage, recorded in the
    // feature_artifacts manifest. Anchored to a single day by the caller, this
    // prunes to one hypertable chunk and stays fast despite the JSONB predicate
    // being unindexed. (The old `alert_score IS NOT NULL` gate had no usable
    // index, forced a full-hypertable scan, and excluded exactly this data —
    // the materialized visuals are not yet scored by the backfill.)
    whereParts.push(
      "(b.feature_artifacts ? 'spectrogram') AND " +
        "(b.feature_artifacts ? 'smooth_waveform' OR b.feature_artifacts ? 'waveform') AND " +
        "(b.feature_artifacts ? 'processed_clip')",
    );
  } else {
    // Filtered views stay browsable on any clip-bearing detection; visuals
    // render when present and fall back to on-demand generation otherwise.
    whereParts.push("b.has_processed_clip = TRUE");
  }

  if (speciesId) {
    queryParams.push(speciesId);
    whereParts.push(`b.species_id = $${queryParams.length}`);
  }

  if (stationIds && stationIds.length > 0) {
    queryParams.push(stationIds);
    whereParts.push(`b.station_id = ANY($${queryParams.length}::text[])`);
  }

  const whereClause = whereParts.length
    ? `WHERE ${whereParts.join(" AND ")}`
    : "";
  const { orderBy, fastPathOrderBy, requiresSpeciesSort, pagedSelectColumns } =
    buildSortOrder(
    filters.sort,
    );
  const limitParam = `$${queryParams.length + 1}`;
  const offsetParam = `$${queryParams.length + 2}`;

  const result = await query<ExploreDetectionRow>(
    requiresSpeciesSort
      ? `
          SELECT b.detection_id, b.detected_at, b.observation_date,
                 sp.common_name, sp.scientific_name,
                 b.confidence,
                 rs.station_name, rs.station_country, rs.station_state,
                 b.station_id, b.species_id,
                 b.audio_filename, b.audio_url, b.audio_start_time_s, b.audio_end_time_s,
                 sp.species_image_url, sp.species_thumbnail_url, sp.species_color,
               b.birdnet_alert_score, b.perch_v2_alert_score,
                 b.background_sound_clf,
                 b.feature_artifacts,
                 b.has_processed_clip
          FROM birdweather_tbl b
          LEFT JOIN species_tbl sp              ON sp.species_id = b.species_id
          LEFT JOIN birdweather_stations_tbl rs ON rs.station_id = b.station_id
          ${whereClause}
          ORDER BY ${orderBy}
          LIMIT ${limitParam} OFFSET ${offsetParam}
        `
      : `
          WITH paged AS (
             SELECT b.detection_id, b.detected_at${pagedSelectColumns}
            FROM birdweather_tbl b
            ${whereClause}
            ORDER BY ${orderBy}
            LIMIT ${limitParam} OFFSET ${offsetParam}
          )
           SELECT b.detection_id, b.detected_at, b.observation_date,
                 sp.common_name, sp.scientific_name,
             b.confidence,
                 rs.station_name, rs.station_country, rs.station_state,
             b.station_id, b.species_id,
             b.audio_filename, b.audio_url, b.audio_start_time_s, b.audio_end_time_s,
                 sp.species_image_url, sp.species_thumbnail_url, sp.species_color,
             b.birdnet_alert_score, b.perch_v2_alert_score,
             b.background_sound_clf,
             b.feature_artifacts,
             b.has_processed_clip
          FROM paged p
           JOIN birdweather_tbl b                ON b.detection_id = p.detection_id
           LEFT JOIN species_tbl sp              ON sp.species_id = b.species_id
           LEFT JOIN birdweather_stations_tbl rs ON rs.station_id = b.station_id
          ORDER BY ${fastPathOrderBy}
        `,
    [...queryParams, limit, offset],
  );

  return result.rows;
}

async function loadDetectionRows({
  filters,
  speciesId,
  stationIds,
}: {
  filters: ExploreNormalizedRequest;
  speciesId: string | null;
  stationIds: string[] | null;
}): Promise<{ rows: ExploreDetectionRow[]; hasNextPage: boolean }> {
  const queryPageSize = filters.pageSize + 1;

  // Default inventory (date-desc, no explicit filters): open on the newest day
  // that already has fully materialized visuals, gated on visual-completeness.
  // Anchoring detected_at to that single day prunes to one chunk, so this stays
  // fast and lands exactly on the latest audio that has spectrogram + waveform.
  const isDefaultInventory =
    filters.processedOnly &&
    filters.sort === "date-desc" &&
    filters.dateFrom == null &&
    filters.dateTo == null &&
    filters.minConf == null &&
    speciesId == null &&
    (stationIds == null || stationIds.length === 0);

  if (isDefaultInventory) {
    const latestVisualDate = await loadLatestVisualArtifactDate();
    if (latestVisualDate) {
      const fromIso = `${latestVisualDate}T00:00:00.000Z`;
      const toExclusiveIso = shiftDateIso(latestVisualDate, 1);
      const rows = await executeDetectionQuery({
        filters,
        speciesId,
        stationIds,
        gate: "visuals",
        limit: queryPageSize,
        offset: filters.offset,
        detectedAtFromOverride: fromIso,
        detectedAtToExclusiveOverride: toExclusiveIso,
      });

      return {
        rows: rows.slice(0, filters.pageSize),
        hasNextPage: rows.length > filters.pageSize,
      };
    }
    // If object storage is unavailable, fall through to the generic path.
  }

  if (shouldUseAdaptiveLookback(filters)) {
    const latestDetectedAt = await loadLatestDetectionDate();
    const requiredRows = filters.offset + queryPageSize;

    if (latestDetectedAt) {
      const lookaheadEndExclusive = shiftDateIso(
        latestDetectedAt,
        ADAPTIVE_LOOKAHEAD_PAD_DAYS + 1,
      );

      for (const windowDays of ADAPTIVE_LOOKBACK_WINDOWS_DAYS) {
        const lookbackStartIso = buildLookbackStartIso(
          latestDetectedAt,
          windowDays,
        );
        if (!lookbackStartIso || !lookaheadEndExclusive) {
          break;
        }

        const rows = await executeDetectionQuery({
          filters,
          speciesId,
          stationIds,
          limit: requiredRows,
          offset: 0,
          detectedAtFromOverride: lookbackStartIso,
          detectedAtToExclusiveOverride: lookaheadEndExclusive,
        });

        if (rows.length >= requiredRows) {
          const pageRows = rows.slice(
            filters.offset,
            filters.offset + filters.pageSize,
          );

          return {
            rows: pageRows,
            hasNextPage: rows.length > filters.offset + filters.pageSize,
          };
        }
      }
    }
  }

  const rows = await executeDetectionQuery({
    filters,
    speciesId,
    stationIds,
    limit: queryPageSize,
    offset: filters.offset,
  });

  return {
    rows: rows.slice(0, filters.pageSize),
    hasNextPage: rows.length > filters.pageSize,
  };
}

function toIsoDateString(value: unknown): string | null {
  if (value instanceof Date) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }
  if (typeof value === "string" && value.length >= 10) {
    const candidate = value.slice(0, 10);
    return /^\d{4}-\d{2}-\d{2}$/.test(candidate) ? candidate : null;
  }
  return null;
}

async function mapDetectionRow(
  row: ExploreDetectionRow,
): Promise<ExploreClientDetection> {
  const speciesId = row.species_id ? String(row.species_id) : null;
  const targetSpecies = speciesId
    ? TARGET_SPECIES_BY_BIRDWEATHER_ID.get(speciesId)
    : undefined;
  const scientificName =
    (typeof row.scientific_name === "string" && row.scientific_name.trim()) ||
    targetSpecies?.scientific ||
    null;
  const speciesName =
    (typeof row.common_name === "string" && row.common_name.trim()) ||
    targetSpecies?.english ||
    scientificName ||
    (row.species_id ? `Species ${row.species_id}` : "Unknown species");
  const mediaPlan = buildExploreMediaPlan(row);
  const [processedAudioUrl, rawAudioUrl, waveformUrl, spectrogramUrl] =
    await Promise.all([
      presignObjectPath("processed-audio", mediaPlan.processedAudioPath),
      presignObjectPath("raw-audio", mediaPlan.rawAudioPath),
      presignObjectPath("features", mediaPlan.waveformPath),
      presignObjectPath("features", mediaPlan.spectrogramPath),
    ]);

  const startTime = toFiniteNumber(row.audio_start_time_s);
  const endTime = toFiniteNumber(row.audio_end_time_s);
  const locParts = [row.station_state, row.station_country].filter(Boolean);
  const anomaly = await resolveExploreAnomalyScore({
    detectionId: String(row.detection_id),
    speciesId,
    detectionScore: readDetectionAnomalyScore(row),
  });

  return {
    id: String(row.detection_id),
    speciesId,
    time: new Date(row.detected_at).toLocaleString("en-GB", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }),
    species: speciesName,
    scientificName,
    confidence: row.confidence,
    station: row.station_name || row.station_id || "Unknown station",
    location: locParts.join(", ") || null,
    imageUrl: row.species_image_url || null,
    thumbnailUrl: row.species_thumbnail_url || null,
    speciesColor: row.species_color || null,
    startTime,
    endTime,
    clipDuration:
      startTime != null && endTime != null ? endTime - startTime : null,
    anomalyScore: anomaly.anomalyScore,
    anomalySource: anomaly.anomalySource,
    bgTags: extractBackgroundTags(row.background_sound_clf),
    audioUrls: {
      raw: rawAudioUrl,
      processed: processedAudioUrl,
      clip: processedAudioUrl,
      direct: mediaPlan.directAudioUrl,
    },
    visualUrls: buildVisualUrls({
      waveformUrl,
      spectrogramUrl,
      detectionId: row.detection_id,
      hasProcessedClip: row.has_processed_clip,
      observationDate: toIsoDateString(row.observation_date),
    }),
  };
}

function buildEmptyPageData({
  filters,
  options,
  stationNotInDb,
  loadFailed = false,
}: {
  filters: ExploreNormalizedRequest;
  options: ExploreFilterOptions;
  stationNotInDb: boolean;
  loadFailed?: boolean;
}): ExplorePageData {
  return {
    data: [],
    currentPage: filters.page,
    hasNextPage: false,
    pageSize: filters.pageSize,
    stationNotInDb,
    loadFailed,
    options,
  };
}

async function loadExplorePageDataLive({
  filters,
  options,
  stationNotInDb,
  startedAt,
  optionsMs,
}: {
  filters: ExploreNormalizedRequest;
  options: ExploreFilterOptions;
  stationNotInDb: boolean;
  startedAt: number;
  optionsMs: number;
}): Promise<ExplorePageData> {
  const lookupsStartedAt = Date.now();
  const [speciesId, stationIds] = await Promise.all([
    filters.species ? resolveSpeciesId(filters.species) : Promise.resolve(null),
    filters.station || filters.location
      ? resolveStationIds(filters.station, filters.location)
      : Promise.resolve(null),
  ]);
  const lookupsMs = Date.now() - lookupsStartedAt;

  if (filters.species && !speciesId) {
    return buildEmptyPageData({ filters, options, stationNotInDb });
  }

  if ((filters.station || filters.location) && (!stationIds || stationIds.length === 0)) {
    return buildEmptyPageData({ filters, options, stationNotInDb });
  }

  const queryStartedAt = Date.now();
  const { rows, hasNextPage } = await loadDetectionRows({
    filters,
    speciesId,
    stationIds,
  });
  const queryMs = Date.now() - queryStartedAt;
  const mapStartedAt = Date.now();
  const data = await Promise.all(rows.map((row) => mapDetectionRow(row)));
  const mapMs = Date.now() - mapStartedAt;

  logExploreTimings({
    optionsMs,
    lookupsMs,
    queryMs,
    mapMs,
    totalMs: Date.now() - startedAt,
    rows: data.length,
  });

  return {
    data,
    currentPage: filters.page,
    hasNextPage,
    pageSize: filters.pageSize,
    stationNotInDb,
    loadFailed: false,
    options,
  };
}

async function refreshDefaultExplorePageCache({
  filters,
  options,
  stationNotInDb,
  startedAt,
  optionsMs,
}: {
  filters: ExploreNormalizedRequest;
  options: ExploreFilterOptions;
  stationNotInDb: boolean;
  startedAt: number;
  optionsMs: number;
}): Promise<ExplorePageData | null> {
  if (defaultExplorePageRefreshPromise) {
    return defaultExplorePageRefreshPromise;
  }

  defaultExplorePageRefreshPromise = loadExplorePageDataLive({
    filters,
    options,
    stationNotInDb,
    startedAt,
    optionsMs,
  })
    .then(async (payload) => {
      if (!payload.loadFailed) {
        await writeDefaultExplorePageCache(payload);
      }
      return payload;
    })
    .catch((error) => {
      logExploreLoadFailure("default explore cache refresh", error);
      return null;
    })
    .finally(() => {
      defaultExplorePageRefreshPromise = null;
    });

  return defaultExplorePageRefreshPromise;
}

export async function loadExplorePageData(
  searchParams: ExploreSearchParams,
): Promise<ExplorePageData> {
  const startedAt = Date.now();
  const filters = normalizeExploreRequest(searchParams);
  const optionsStartedAt = Date.now();
  const options = await loadExploreFilterOptions();
  const optionsMs = Date.now() - optionsStartedAt;
  const stationNotInDb = Boolean(
    filters.station &&
      options.stations.length > 0 &&
      !options.stations.includes(filters.station),
  );
  const shouldUseDefaultCache = isDefaultExploreRequest(filters);

  try {
    if (shouldUseDefaultCache) {
      const cachedSnapshot = await readDefaultExplorePageCache();
      if (cachedSnapshot) {
        if (!isCacheFresh(cachedSnapshot)) {
          void refreshDefaultExplorePageCache({
            filters,
            options,
            stationNotInDb,
            startedAt,
            optionsMs,
          });
        }

        // The cache stores the expensive query result; object-storage URLs
        // expire hourly, so re-presign on every serve (CPU-only HMAC) to keep
        // media working regardless of how old the cached snapshot is.
        const data = await Promise.all(
          cachedSnapshot.payload.data.map(sanitizeCachedDetection),
        );
        return withExploreOptions(
          { ...cachedSnapshot.payload, data },
          options,
        );
      }
    }

    const payload = await loadExplorePageDataLive({
      filters,
      options,
      stationNotInDb,
      startedAt,
      optionsMs,
    });

    if (shouldUseDefaultCache && !payload.loadFailed) {
      await writeDefaultExplorePageCache(payload);
    }

    return payload;
  } catch (error) {
    logExploreLoadFailure("detection query", error);
    return buildEmptyPageData({
      filters,
      options,
      stationNotInDb,
      loadFailed: true,
    });
  }
}
