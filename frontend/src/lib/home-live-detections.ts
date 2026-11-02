import "server-only";

import {
  TARGET_SPECIES_BY_BIRDWEATHER_ID,
  TARGET_SPECIES_LIST,
} from "@/config/target-species";
import { query } from "@/lib/db";
import { activeScoreRowsCte } from "@/lib/modeling-db";
import type {
  FeedFilters,
  FeedFilterOptions,
  FeedFilterWeek,
} from "@/lib/home-feed-filters";
import type { LiveDetection } from "@/components/home/LiveDetectionsTable";

// Bounded cap on rendered rows. Ordering happens in SQL (see fetchLiveDetections),
// so for either mode this is the latest-N / score-ordered-N of the bounded
// detection window: enough to show a normal slice in full, small enough to keep
// the payload and the unvirtualised feed DOM fast. Raise with care; every row is
// a rich card.
const LIVE_FEED_LIMIT = 150;

// The live feed is a detection feed first. Per-detection anomaly scores
// (`perch_v2_alert_score` / `birdnet_alert_score`) are optional enrichment and
// may lag behind the newest processed audio, so score presence must not gate the
// default feed or ordinary filters.
const DEFAULT_LOOKAHEAD_DAYS = 1;
const SCORE_FILTER_LOOKBACK_DAYS = 120;
// Tight window for relaxed narrowing filters (e.g. the live-map station
// spotlight): recent detections only, so a station with no target audio in
// range resolves in a few weeks of chunks instead of months.
const NARROW_LOOKBACK_DAYS = 30;
const NARROW_LOOKAHEAD_DAYS = 3;
// A spatial filter gets a wider lookback so station/grid spotlights
// surface enough recent detections from their own matching slice.
const SPATIAL_LOOKBACK_DAYS = 45;
const SPATIAL_LOOKAHEAD_DAYS = 1;
// Within that bounded pool we surface the trailing week of actual processed
// detections, anchored on the newest matching detection. This makes the default
// feed reflect the current data slice even when anomaly enrichment is absent.
const FEED_TRAILING_DAYS = 7;
const FILTER_WEEK_LOOKBACK_DAYS = 730;
const DAY_MS = 86_400_000;

// Ordering modes the feed UI can request. Resolved in SQL so each mode returns
// the correct bounded set (latest rows vs scored rows first), rather than
// re-sorting a recency-truncated slice.
export type FeedSort = "latest" | "anomaly";

const TARGET_SPECIES_IDS = Array.from(TARGET_SPECIES_BY_BIRDWEATHER_ID.keys());

type DetectionAnchorRow = { detected_at: Date | string | null };

type FeedRow = {
  detection_id: string;
  detected_at: Date | string;
  observation_date: Date | string | null;
  confidence: number | string | null;
  species_id: string | null;
  station_id: string | null;
  processed_clip: string | null;
  alert_score: number | string | null;
  common_name: string | null;
  scientific_name: string | null;
  species_image_url: string | null;
  species_thumbnail_url: string | null;
  station_name: string | null;
  station_state: string | null;
  station_country: string | null;
};

function formatFeedTime(detectedAt: Date | string): string {
  const date = new Date(detectedAt);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "UTC",
  });
}

function mediaProxyUrl(bucket: string, objectPath: string): string {
  const params = new URLSearchParams({ bucket, path: objectPath });
  return `/api/audio-clip?${params.toString()}`;
}

function toDateString(value: Date | string): string {
  if (value instanceof Date) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }
  return String(value).split("T")[0];
}

// Mirror of the alerts loader: prefer the stored processed-clip artifact, else
// fall back to the deterministic processed-audio object path.
function processedAudioUrl(row: FeedRow): string | null {
  const artifact = typeof row.processed_clip === "string" ? row.processed_clip.trim() : "";
  if (artifact) {
    const prefix = "processed-audio/";
    const objectPath = artifact.startsWith(prefix) ? artifact.slice(prefix.length) : artifact;
    if (objectPath && !objectPath.includes("..")) {
      return mediaProxyUrl("processed-audio", objectPath);
    }
  }
  if (!row.observation_date) return null;
  const stationId = row.station_id || "unknown_station";
  const dateStr = toDateString(row.observation_date);
  return mediaProxyUrl(
    "processed-audio",
    `birdweather/${dateStr}/${stationId}/${row.detection_id}.flac`,
  );
}

function weeklyScoreJoinSql(dateExpression: string): string {
  return `
    LEFT JOIN LATERAL (
      SELECT GREATEST(
        COALESCE(scores.embedding_drift_score, 0),
        COALESCE(scores.embedding_anomaly_score, 0),
        COALESCE(scores.final_score, 0)
      ) AS score
      FROM active_score_rows scores
      WHERE scores.species_id = b.species_id
        AND scores.grid_cell_id = rs.grid_cell_id
        AND scores.period_start <= ${dateExpression}
        AND COALESCE(scores.period_end, scores.period_start) >= ${dateExpression}
      ORDER BY score DESC
      LIMIT 1
    ) weekly_score ON TRUE
  `;
}

function rowToLiveDetection(row: FeedRow): LiveDetection {
  const speciesId = row.species_id ? String(row.species_id) : null;
  const target = speciesId ? TARGET_SPECIES_BY_BIRDWEATHER_ID.get(speciesId) : undefined;
  const species =
    (row.common_name && row.common_name.trim()) ||
    target?.english ||
    (row.scientific_name && row.scientific_name.trim()) ||
    (speciesId ? `Species ${speciesId}` : "Unknown species");
  const scientificName =
    (row.scientific_name && row.scientific_name.trim()) || target?.scientific || null;
  const location =
    [row.station_state, row.station_country].filter(Boolean).join(", ") || null;
  const rawScore = row.alert_score === null ? null : Number(row.alert_score);
  const detectedAtDate = new Date(row.detected_at);
  const detectedAtIso = Number.isNaN(detectedAtDate.getTime())
    ? null
    : detectedAtDate.toISOString();

  return {
    id: row.detection_id,
    time: formatFeedTime(row.detected_at),
    detectedAt: detectedAtIso,
    observationDate: row.observation_date ? toDateString(row.observation_date) : null,
    species,
    scientificName,
    confidence: Number(row.confidence) || 0,
    station: row.station_name || row.station_id || "Unknown station",
    location,
    stationType: "app",
    stationQuality: 0,
    imageUrl: row.species_image_url || row.species_thumbnail_url || null,
    thumbnailUrl: null,
    silhouetteUrl: null,
    audioUrls: { raw: null, processed: processedAudioUrl(row), direct: null },
    anomalyScore:
      rawScore !== null && Number.isFinite(rawScore)
        ? Math.max(0, Math.min(1, rawScore))
        : null,
  };
}

type DetectionAnchorFilters = {
  speciesIds: string[];
  stationIds?: string[] | null;
  minConfidence?: number | null;
  minScore?: number | null;
};

// Resolve the newest processed BirdWeather detection for the active feed slice.
// Anomaly-score presence is required only when the caller explicitly applies a
// minimum anomaly filter.
async function resolveDetectionAnchor(
  filters: DetectionAnchorFilters,
): Promise<Date | null> {
  const params: unknown[] = [];
  const bind = (value: unknown): string => {
    params.push(value);
    return `$${params.length}`;
  };

  const where: string[] = [
    "b.has_processed_clip",
    `b.species_id = ANY(${bind(filters.speciesIds)}::text[])`,
  ];
  if (filters.stationIds && filters.stationIds.length > 0) {
    where.push(`b.station_id = ANY(${bind(filters.stationIds)}::text[])`);
  }
  if (filters.minConfidence != null && filters.minConfidence > 0) {
    where.push(`b.confidence >= ${bind(filters.minConfidence)}`);
  }
  const needsWeeklyScore = filters.minScore != null && filters.minScore > 0;
  if (needsWeeklyScore) {
    where.push(
      `COALESCE(b.perch_v2_alert_score, b.birdnet_alert_score, weekly_score.score) >= ${bind(filters.minScore)}`,
    );
  }

  const result = await query<DetectionAnchorRow>(
    `
    SELECT b.detected_at
    FROM birdweather_tbl b
    ${needsWeeklyScore ? "LEFT JOIN birdweather_stations_tbl rs ON rs.station_id = b.station_id" : ""}
    ${needsWeeklyScore
      ? `LEFT JOIN LATERAL (
          SELECT COALESCE(scores.drift_score, 0) AS score
          FROM species_week_anomaly_tbl scores
          WHERE scores.species_id = b.species_id
            AND scores.grid_cell_id = rs.grid_cell_id
            AND scores.period_start <= COALESCE(b.observation_date, b.detected_at::date)
            AND COALESCE(scores.period_end, scores.period_start) >= COALESCE(b.observation_date, b.detected_at::date)
          ORDER BY scores.drift_score DESC NULLS LAST
          LIMIT 1
        ) weekly_score ON TRUE`
      : ""}
    WHERE ${where.join("\n      AND ")}
    ORDER BY b.detected_at DESC
    LIMIT 1
  `,
    params,
  );
  const value = result.rows[0]?.detected_at ?? null;
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * The live feed shows the latest processed detections for the target species.
 * Anomaly scores are optional enrichment: the row carries one when the
 * per-detection projection has written it, otherwise the UI omits the score.
 * Runs inside a single-flight background refresh (see home-live-detections-cache).
 */
export async function fetchLiveDetections(
  sort: FeedSort = "latest",
): Promise<LiveDetection[]> {
  const anchor = await resolveDetectionAnchor({ speciesIds: TARGET_SPECIES_IDS });
  if (!anchor) return [];

  // Bounded candidate pool: the trailing week around the newest matching
  // processed detection so the planner only touches a handful of chunks.
  const from = new Date(anchor.getTime() - FEED_TRAILING_DAYS * DAY_MS);
  const to = new Date(anchor.getTime() + DEFAULT_LOOKAHEAD_DAYS * DAY_MS);

  // Order in SQL so each mode returns the correct bounded set: "latest" → the
  // most recent of the week; "anomaly" → scored rows first by anomaly score,
  // with unscored rows retained after them. `sort` is a closed enum mapped to a
  // constant fragment here, so no caller input reaches the query.
  const orderBy =
    sort === "anomaly"
      ? "alert_score DESC NULLS LAST, detected_at DESC"
      : "detected_at DESC";

  const result = await query<FeedRow>(
    `
    WITH ${activeScoreRowsCte([
      "scores.species_id = ANY($3::text[])",
      "scores.grid_cell_id IS NOT NULL",
      "scores.period_start <= ($2::timestamptz)::date",
      "COALESCE(scores.period_end, scores.period_start) >= ($1::timestamptz)::date",
    ])}
    SELECT
      b.detection_id,
      b.detected_at,
      b.observation_date,
      b.confidence,
      b.species_id,
      b.station_id,
      b.feature_artifacts->>'processed_clip' AS processed_clip,
      COALESCE(b.perch_v2_alert_score, b.birdnet_alert_score, weekly_score.score) AS alert_score,
      sp.common_name,
      sp.scientific_name,
      sp.species_image_url,
      sp.species_thumbnail_url,
      rs.station_name,
      rs.station_state,
      rs.station_country
    FROM birdweather_tbl b
    LEFT JOIN birdweather_stations_tbl rs ON rs.station_id = b.station_id
    ${weeklyScoreJoinSql("COALESCE(b.observation_date, b.detected_at::date)")}
    LEFT JOIN species_tbl sp ON sp.species_id = b.species_id
    WHERE b.detected_at >= $1::timestamptz
      AND b.detected_at <  $2::timestamptz
      AND b.has_processed_clip
      AND b.species_id = ANY($3::text[])
    ORDER BY ${orderBy}
    LIMIT $4
  `,
    [
      from.toISOString(),
      to.toISOString(),
      TARGET_SPECIES_IDS,
      LIVE_FEED_LIMIT,
    ],
  );

  return result.rows.map(rowToLiveDetection);
}

// ─────────────────────────────────────────────────────────────────────────────
// Feed filter panel: option lists + bounded filtered query
// ─────────────────────────────────────────────────────────────────────────────

function parseYmd(value: string): { y: number; m: number; d: number } {
  const [y, m, d] = value.split("-").map((part) => Number.parseInt(part, 10));
  return { y, m: (m || 1) - 1, d: d || 1 };
}

const WEEK_MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatWeekLabel(start: string, end: string): string {
  const s = parseYmd(start);
  const e = parseYmd(end);
  if (s.y === e.y && s.m === e.m) {
    return `${s.d}–${e.d} ${WEEK_MONTHS[s.m]} ${s.y}`;
  }
  if (s.y === e.y) {
    return `${s.d} ${WEEK_MONTHS[s.m]} – ${e.d} ${WEEK_MONTHS[e.m]} ${s.y}`;
  }
  return `${s.d} ${WEEK_MONTHS[s.m]} ${s.y} – ${e.d} ${WEEK_MONTHS[e.m]} ${e.y}`;
}

/**
 * Cheap option lists for the feed filter panel. Every query hits a small
 * reference/derived table (never the many-chunk hypertable): processed-clip
 * weeks from species_tracker_tbl, stations/grids from the station reference
 * table, and species from the static target-species config.
 */
export async function fetchFeedFilterOptions(): Promise<FeedFilterOptions> {
  const weeksResult = await query<{ start: string; end: string }>(
    `
    SELECT DISTINCT period_start::text AS start, period_end::text AS end
    FROM species_tracker_tbl
    WHERE row_kind = 'species'
      AND period_grain = 'week'
      AND species_id = ANY($1::text[])
      AND processed_clip_count > 0
      AND period_start >= (
        SELECT MAX(period_start) - $2::int
        FROM species_tracker_tbl
        WHERE row_kind = 'species'
          AND period_grain = 'week'
          AND species_id = ANY($1::text[])
          AND processed_clip_count > 0
      )
    ORDER BY start DESC
    LIMIT 110
  `,
    [TARGET_SPECIES_IDS, FILTER_WEEK_LOOKBACK_DAYS],
  );
  const weeks: FeedFilterWeek[] = weeksResult.rows.map((row) => ({
    id: row.start,
    start: row.start,
    end: row.end,
    label: formatWeekLabel(row.start, row.end),
  }));

  const stationsResult = await query<{ station_id: string; station_name: string | null }>(
    `
    SELECT station_id, station_name
    FROM birdweather_stations_tbl
    WHERE station_name IS NOT NULL AND station_name <> ''
      AND total_detections > 0
    ORDER BY total_detections DESC NULLS LAST, station_name ASC
    LIMIT 1000
  `,
  );
  const stations = stationsResult.rows.map((row) => ({
    id: row.station_id,
    label: row.station_name || row.station_id,
  }));

  const gridsResult = await query<{ grid_cell_id: string }>(
    `
    SELECT DISTINCT grid_cell_id
    FROM birdweather_stations_tbl
    WHERE grid_cell_id IS NOT NULL
    ORDER BY grid_cell_id ASC
  `,
  );
  const grids = gridsResult.rows.map((row) => ({
    id: row.grid_cell_id,
    label: row.grid_cell_id.replace(/^nl-5km-/, ""),
  }));

  const species = TARGET_SPECIES_LIST.map((entry) => ({
    id: String(entry.birdweatherId),
    label: entry.english,
  })).sort((a, b) => a.label.localeCompare(b.label));

  return { weeks, species, stations, grids };
}

/**
 * Bounded, filtered variant of the live feed. A selected week bounds the scan to
 * that single week; otherwise the query anchors on the newest processed
 * detection matching the selected filter set. Anomaly scores remain optional
 * unless the user explicitly sets a minimum anomaly score.
 */
export async function fetchFilteredLiveDetections(
  filters: FeedFilters,
): Promise<LiveDetection[]> {
  const hasNarrowing = Boolean(
    filters.speciesId ||
    filters.stationId ||
    filters.gridCellId ||
    (filters.minConfidence != null && filters.minConfidence > 0) ||
    (filters.minScore != null && filters.minScore > 0),
  );
  const requiresScore = filters.minScore != null && filters.minScore > 0;
  const speciesIds = filters.speciesId ? [filters.speciesId] : TARGET_SPECIES_IDS;

  // Resolve grid / station selection to concrete station ids,
  // keeping the hypertable predicate on the indexed station_id column.
  let cellIds: string[] | null = null;
  if (filters.gridCellId) {
    cellIds = [filters.gridCellId];
  }

  let stationFilter: string[] | null = null;
  if (cellIds) {
    const stationRows = await query<{ station_id: string }>(
      `
      SELECT station_id
      FROM birdweather_stations_tbl
      WHERE grid_cell_id = ANY($1::text[])
    `,
      [cellIds],
    );
    let rows = stationRows.rows;
    if (filters.stationId) {
      rows = rows.filter((row) => row.station_id === filters.stationId);
    }
    stationFilter = rows.map((row) => row.station_id);
    if (stationFilter.length === 0) return [];
  } else if (filters.stationId) {
    stationFilter = [filters.stationId];
  }

  // Compute the bounded detected_at window.
  let fromIso: string;
  let toIso: string;
  if (filters.week) {
    const start = new Date(`${filters.week}T00:00:00.000Z`);
    if (Number.isNaN(start.getTime())) return [];
    fromIso = start.toISOString();
    toIso = new Date(start.getTime() + 7 * DAY_MS).toISOString();
  } else {
    const anchor = await resolveDetectionAnchor({
      speciesIds,
      stationIds: stationFilter,
      minConfidence: filters.minConfidence,
      minScore: filters.minScore,
    });
    if (!anchor) return [];
    const isSpatialFilter = Boolean(stationFilter);
    const lookbackDays = isSpatialFilter
      ? SPATIAL_LOOKBACK_DAYS
      : requiresScore
        ? SCORE_FILTER_LOOKBACK_DAYS
        : hasNarrowing
          ? NARROW_LOOKBACK_DAYS
          : FEED_TRAILING_DAYS;
    const lookaheadDays = isSpatialFilter
      ? SPATIAL_LOOKAHEAD_DAYS
      : hasNarrowing
        ? NARROW_LOOKAHEAD_DAYS
        : DEFAULT_LOOKAHEAD_DAYS;
    fromIso = new Date(anchor.getTime() - lookbackDays * DAY_MS).toISOString();
    toIso = new Date(anchor.getTime() + lookaheadDays * DAY_MS).toISOString();
  }

  const params: unknown[] = [];
  const bind = (value: unknown): string => {
    params.push(value);
    return `$${params.length}`;
  };

  const fromPlaceholder = bind(fromIso);
  const toPlaceholder = bind(toIso);
  const speciesPlaceholder = bind(speciesIds);
  const cteWhere: string[] = [
    `b.detected_at >= ${fromPlaceholder}::timestamptz`,
    `b.detected_at <  ${toPlaceholder}::timestamptz`,
    `b.has_processed_clip`,
    `b.species_id = ANY(${speciesPlaceholder}::text[])`,
  ];
  if (filters.minConfidence != null && filters.minConfidence > 0) {
    cteWhere.push(`b.confidence >= ${bind(filters.minConfidence)}`);
  }
  if (stationFilter) {
    cteWhere.push(`b.station_id = ANY(${bind(stationFilter)}::text[])`);
  }
  if (filters.minScore != null && filters.minScore > 0) {
    cteWhere.push(
      `COALESCE(b.perch_v2_alert_score, b.birdnet_alert_score, weekly_score.score) >= ${bind(filters.minScore)}`,
    );
  }

  const orderBy =
    filters.sort === "anomaly"
      ? "alert_score DESC NULLS LAST, detected_at DESC"
      : filters.sort === "confidence"
        ? "confidence DESC NULLS LAST, detected_at DESC"
        : "detected_at DESC";

  const limitPlaceholder = bind(LIVE_FEED_LIMIT);

  // The per-row weekly-score LATERAL join (and its `active_score_rows` CTE) is
  // only needed when the anomaly score drives ordering ("anomaly" sort) or
  // filtering (an explicit minimum score). For a station/grid spotlight
  // ("latest" sort) or a "confidence" sort with no minimum score, it is pure
  // display enrichment that the bounded row set and its order never depend on,
  // so we skip it — dropping a correlated subquery per candidate row and an
  // extra CTE from the hottest interactive query. Rows still surface their
  // per-detection score (perch_v2 / birdnet) when present; only the weekly
  // fallback is omitted on that path.
  const needsScore = filters.sort === "anomaly" || requiresScore;
  const scoreCte = needsScore
    ? `WITH ${activeScoreRowsCte([
        `scores.species_id = ANY(${speciesPlaceholder}::text[])`,
        "scores.grid_cell_id IS NOT NULL",
        `scores.period_start <= (${toPlaceholder}::timestamptz)::date`,
        `COALESCE(scores.period_end, scores.period_start) >= (${fromPlaceholder}::timestamptz)::date`,
      ])}`
    : "";
  const scoreJoin = needsScore
    ? weeklyScoreJoinSql("COALESCE(b.observation_date, b.detected_at::date)")
    : "";
  const alertScoreExpr = needsScore
    ? "COALESCE(b.perch_v2_alert_score, b.birdnet_alert_score, weekly_score.score)"
    : "COALESCE(b.perch_v2_alert_score, b.birdnet_alert_score)";

  const result = await query<FeedRow>(
    `
    ${scoreCte}
    SELECT
      b.detection_id,
      b.detected_at,
      b.observation_date,
      b.confidence,
      b.species_id,
      b.station_id,
      b.feature_artifacts->>'processed_clip' AS processed_clip,
      ${alertScoreExpr} AS alert_score,
      sp.common_name,
      sp.scientific_name,
      sp.species_image_url,
      sp.species_thumbnail_url,
      rs.station_name,
      rs.station_state,
      rs.station_country
    FROM birdweather_tbl b
    LEFT JOIN birdweather_stations_tbl rs ON rs.station_id = b.station_id
    ${scoreJoin}
    LEFT JOIN species_tbl sp ON sp.species_id = b.species_id
    WHERE ${cteWhere.join("\n      AND ")}
    ORDER BY ${orderBy}
    LIMIT ${limitPlaceholder}
  `,
    params,
  );

  return result.rows.map(rowToLiveDetection);
}
