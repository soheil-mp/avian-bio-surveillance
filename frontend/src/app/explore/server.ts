export type SearchParamValue = string | string[] | undefined;
export type VisualMode = "bar-scope" | "smooth-scope" | "spectrogram";
export type BackgroundTag = { label: string; score: number };
export type BackgroundSoundEnvelope = { tags?: unknown } | null;
export type ExploreDetectedAtBounds = {
  detectedAtFrom: string | null;
  detectedAtToExclusive: string | null;
};
export type ExploreDetectionRow = {
  detection_id: string | number;
  detected_at: Date | string;
  observation_date: Date | string | null;
  common_name: string | null;
  scientific_name: string | null;
  confidence: number;
  station_name: string | null;
  station_country: string | null;
  station_state: string | null;
  station_id: string | null;
  species_id: string | number | null;
  audio_filename: string | null;
  audio_url: string | null;
  audio_start_time_s: number | string | null;
  audio_end_time_s: number | string | null;
  species_image_url: string | null;
  species_thumbnail_url: string | null;
  species_color: string | null;
  birdnet_alert_score?: number | string | null;
  perch_v2_alert_score?: number | string | null;
  background_sound_clf: BackgroundSoundEnvelope;
  feature_artifacts: unknown;
  has_processed_clip: boolean | null;
};
export type ExploreMediaRow = Pick<
  ExploreDetectionRow,
  | "observation_date"
  | "has_processed_clip"
  | "station_id"
  | "detection_id"
  | "audio_filename"
  | "audio_url"
  | "feature_artifacts"
>;
export type ExploreMediaPlan = {
  processedAudioPath: string | null;
  rawAudioPath: string | null;
  waveformPath: string | null;
  spectrogramPath: string | null;
  directAudioUrl: string | null;
};
export type ExploreVisualUrls = {
  waveform: string | null;
  spectrogram: string | null;
};
export type ExploreVisualKind = "waveform" | "spectrogram";
export type ExploreAnomalySource = "detection" | "weekly" | null;

export const DEFAULT_VISUAL_MODE: VisualMode = "bar-scope";
const DEFAULT_FEATURE_FALLBACK_MAX_DATE = "2025-01-29";
const FEATURE_FALLBACK_MAX_DATE =
  readFeatureFallbackMaxDate(
    process.env.EXPLORE_FEATURE_FALLBACK_MAX_DATE,
  ) ?? DEFAULT_FEATURE_FALLBACK_MAX_DATE;
export const EXPLORE_FEATURE_FALLBACK_MAX_DATE = FEATURE_FALLBACK_MAX_DATE;

function readFeatureFallbackMaxDate(value: string | undefined): string | null {
  const trimmed = value?.trim();
  if (!trimmed) {
    return null;
  }

  return /^\d{4}-\d{2}-\d{2}$/.test(trimmed) ? trimmed : null;
}

export function readSearchParam(value: SearchParamValue): string | null {
  const candidate = Array.isArray(value) ? value[0] : value;
  if (typeof candidate !== "string") return null;
  const trimmed = candidate.trim();
  return trimmed ? trimmed : null;
}

export function readPositiveIntParam(
  value: SearchParamValue,
  fallback: number,
): number {
  const candidate = readSearchParam(value);
  if (!candidate) return fallback;

  const parsed = Number.parseInt(candidate, 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export function readDateParam(value: SearchParamValue): string | null {
  const candidate = readSearchParam(value);
  if (!candidate || !/^\d{4}-\d{2}-\d{2}$/.test(candidate)) {
    return null;
  }

  const parsed = new Date(`${candidate}T00:00:00.000Z`);
  return Number.isNaN(parsed.getTime()) ? null : candidate;
}

function toUtcMidnightIso(
  dateValue: string | null,
  dayOffset = 0,
): string | null {
  if (!dateValue) return null;

  const parsed = new Date(`${dateValue}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }

  parsed.setUTCDate(parsed.getUTCDate() + dayOffset);
  return parsed.toISOString();
}

export function buildDetectedAtBounds(
  dateFrom: string | null,
  dateTo: string | null,
): ExploreDetectedAtBounds {
  return {
    detectedAtFrom: toUtcMidnightIso(dateFrom),
    detectedAtToExclusive: toUtcMidnightIso(dateTo, 1),
  };
}

export function readVisualMode(value: SearchParamValue): VisualMode {
  const candidate = readSearchParam(value);
  if (candidate === "smooth-scope" || candidate === "spectrogram") {
    return candidate;
  }
  return DEFAULT_VISUAL_MODE;
}

export function toDateString(value: Date | string): string {
  if (value instanceof Date) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }

  return String(value).split("T")[0];
}

export function artifactObjectPath(
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

  return objectPath;
}

export function processedClipFallbackPath(row: ExploreMediaRow): string | null {
  if (!row.observation_date || !row.has_processed_clip) return null;
  const stationId = row.station_id || "unknown_station";
  const dateStr = toDateString(row.observation_date);
  return `birdweather/${dateStr}/${stationId}/${row.detection_id}.flac`;
}

export function rawAudioFallbackPath(row: ExploreMediaRow): string | null {
  if (!row.observation_date || typeof row.audio_filename !== "string") {
    return null;
  }

  const filename = row.audio_filename.split("/").pop()?.trim();
  if (!filename) return null;

  const dateStr = toDateString(row.observation_date);
  return `${dateStr}/audio/${filename}`;
}

export function toFiniteNumber(value: unknown): number | null {
  if (value == null) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

export function readDetectionAnomalyScore(
  row: Pick<
    ExploreDetectionRow,
    "birdnet_alert_score" | "perch_v2_alert_score"
  >,
): number | null {
  const perchScore = toFiniteNumber(row.perch_v2_alert_score);
  const birdnetScore = toFiniteNumber(row.birdnet_alert_score);
  const candidates = [perchScore, birdnetScore].filter(
    (candidate): candidate is number => candidate != null,
  );
  if (candidates.length === 0) {
    return null;
  }

  return Math.min(Math.max(Math.max(...candidates), 0), 1);
}

export function readConfidenceParam(value: SearchParamValue): number | null {
  const candidate = readSearchParam(value);
  if (!candidate) return null;

  const numeric = toFiniteNumber(candidate);
  if (numeric == null) return null;

  const normalized = numeric > 1 ? numeric / 100 : numeric;
  return Math.min(Math.max(normalized, 0), 1);
}

export function toNonEmptyString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function isBackgroundTag(value: unknown): value is BackgroundTag {
  if (!value || typeof value !== "object") return false;

  const tag = value as { label?: unknown; score?: unknown };
  return typeof tag.label === "string" && typeof tag.score === "number";
}

export function featureChartFallbackPath(
  row: ExploreMediaRow,
  prefix: "waveforms" | "spectrograms",
  suffix = "",
): string | null {
  if (!row.observation_date || !row.has_processed_clip) return null;
  const stationId = row.station_id || "unknown_station";
  const dateStr = toDateString(row.observation_date);
  if (dateStr > FEATURE_FALLBACK_MAX_DATE) return null;
  return `${prefix}/birdweather/${dateStr}/${stationId}/${row.detection_id}${suffix}.png`;
}

function asArtifactManifest(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

export function extractBackgroundTags(
  bgClf: BackgroundSoundEnvelope,
): BackgroundTag[] {
  const rawTags = Array.isArray(bgClf?.tags)
    ? bgClf.tags.filter(isBackgroundTag)
    : [];

  return rawTags
    .filter((tag) => tag.score >= 0.15)
    .sort((left, right) => right.score - left.score)
    .slice(0, 5);
}

export function buildExploreMediaPlan(
  row: ExploreMediaRow,
): ExploreMediaPlan {
  const artifacts = asArtifactManifest(row.feature_artifacts);
  const processedAudioPath =
    artifactObjectPath(artifacts.processed_clip, "processed-audio") ||
    processedClipFallbackPath(row);
  const rawAudioPath = rawAudioFallbackPath(row);
  const waveformPath =
    artifactObjectPath(artifacts.smooth_waveform, "features") ||
    artifactObjectPath(artifacts.waveform, "features") ||
    featureChartFallbackPath(row, "waveforms", "_smooth");
  const spectrogramPath =
    artifactObjectPath(artifacts.spectrogram, "features") ||
    featureChartFallbackPath(row, "spectrograms");

  return {
    processedAudioPath,
    rawAudioPath,
    waveformPath,
    spectrogramPath,
    directAudioUrl: toNonEmptyString(row.audio_url),
  };
}

export function buildOnDemandVisualUrl(
  detectionId: string | number | null | undefined,
  kind: ExploreVisualKind,
  hasProcessedClip: boolean | null | undefined,
  observationDate?: string | null,
): string | null {
  const normalizedDetectionId =
    detectionId == null ? null : toNonEmptyString(String(detectionId));
  if (!normalizedDetectionId || !hasProcessedClip) {
    return null;
  }

  const params = new URLSearchParams({
    detectionId: normalizedDetectionId,
    kind,
  });
  // Pass the observation date so the artifact route can prune the detection
  // lookup to a single TimescaleDB chunk instead of scanning all ~700+ chunks
  // (an unbounded `WHERE detection_id = ...` is what exhausts the DB pool).
  if (typeof observationDate === "string" && /^\d{4}-\d{2}-\d{2}$/.test(observationDate)) {
    params.set("date", observationDate);
  }
  return `/api/explore/artifact?${params.toString()}`;
}

export function buildVisualUrls({
  waveformUrl,
  spectrogramUrl,
  detectionId,
  hasProcessedClip,
  observationDate,
}: {
  waveformUrl: string | null;
  spectrogramUrl: string | null;
  detectionId: string | number | null | undefined;
  hasProcessedClip: boolean | null | undefined;
  observationDate?: string | null;
}): ExploreVisualUrls {
  return {
    waveform:
      waveformUrl ??
      buildOnDemandVisualUrl(detectionId, "waveform", hasProcessedClip, observationDate),
    spectrogram:
      spectrogramUrl ??
      buildOnDemandVisualUrl(detectionId, "spectrogram", hasProcessedClip, observationDate),
  };
}