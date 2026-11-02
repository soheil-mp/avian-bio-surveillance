import { NextRequest, NextResponse } from "next/server";

import { loadAlertsData } from "@/app/alerts/data";
import { findAlertDetailPoint } from "@/app/alerts/embedding-map";
import type { AlertReviewContext, AlertsModelingSummary, AlertVisualPoint } from "@/app/alerts/types";
import { query } from "@/lib/db";
import { loadLatestModelingRun } from "@/lib/modeling-db";
import { withMeasuredApiRoute } from "@/lib/performance/server";
import type { SearchParamValue } from "@/app/alerts/server";

export const dynamic = "force-dynamic";

function searchParamsToRecord(request: NextRequest): Record<string, SearchParamValue> {
  const params: Record<string, SearchParamValue> = {};

  request.nextUrl.searchParams.forEach((value, key) => {
    if (key === "detectionId") return;

    const current = params[key];
    if (Array.isArray(current)) {
      current.push(value);
    } else if (typeof current === "string") {
      params[key] = [current, value];
    } else {
      params[key] = value;
    }
  });

  return params;
}

type DetailRow = {
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
  pc1: number | string | null;
  pc2: number | string | null;
  alert_score: number | string | null;
  embedding_backend: string | null;
  model_alert_level: string | null;
  model_final_score: number | string | null;
  model_anomaly_score: number | string | null;
  embedding_drift_score: number | string | null;
  embedding_anomaly_score: number | string | null;
  embedding_outlier_share: number | string | null;
  embedding_clip_count_scored: number | string | null;
  population_anomaly_score: number | string | null;
  population_drop_score: number | string | null;
  population_spike_score: number | string | null;
  model_period_start: Date | string | null;
  model_period_end: Date | string | null;
  anomaly_streams: string[] | null;
};

function toNumber(value: number | string | null | undefined): number {
  if (value == null) return 0;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

function toNullableNumber(value: number | string | null | undefined): number | null {
  if (value == null) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function toIso(value: Date | string | null | undefined): string | null {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toISOString();
}

function toDateString(value: Date | string | null | undefined): string | null {
  if (!value) return null;
  if (value instanceof Date) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }
  return String(value).split("T")[0] || null;
}

function mediaProxyUrl(bucket: string, objectPath: string): string | null {
  if (!objectPath || objectPath.includes("..")) return null;
  const params = new URLSearchParams({ bucket, path: objectPath });
  return `/api/audio-clip?${params.toString()}`;
}

function artifactProxyUrl(value: string | null, bucket: "processed-audio" | "features"): string | null {
  const artifact = value?.trim() ?? "";
  if (!artifact) return null;
  const prefix = `${bucket}/`;
  return mediaProxyUrl(bucket, artifact.startsWith(prefix) ? artifact.slice(prefix.length) : artifact);
}

function processedClipFallback(row: DetailRow): string | null {
  const date = toDateString(row.observation_date);
  if (!date) return null;
  return mediaProxyUrl(
    "processed-audio",
    `birdweather/${date}/${row.station_id || "unknown_station"}/${row.detection_id}.flac`,
  );
}

function featureChartFallback(row: DetailRow, kind: "waveforms" | "spectrograms", suffix = ""): string | null {
  const date = toDateString(row.observation_date);
  if (!date) return null;
  return mediaProxyUrl(
    "features",
    `birdweather/${date}/${row.station_id || "unknown_station"}/${kind}/${row.detection_id}${suffix}.png`,
  );
}

function reviewContextFromRow(row: DetailRow): AlertReviewContext | null {
  if (row.model_period_start == null && row.model_final_score == null) return null;
  const finalScore = Math.max(
    toNumber(row.model_final_score),
    toNumber(row.model_anomaly_score),
    toNumber(row.embedding_drift_score),
    toNumber(row.embedding_anomaly_score),
  );
  return {
    finalScore,
    reviewPriority: row.model_alert_level?.trim() || "green",
    reviewReason: "weekly model evidence",
    alertLevel: row.model_alert_level?.trim() || "green",
    requiresAudioReview: false,
    requiresContextReview: false,
    qualityReviewFlag: false,
    qualityMaskReason: null,
    weeklyPeriodStart: toDateString(row.model_period_start),
    weeklyPeriodEnd: toDateString(row.model_period_end),
    backgroundNoiseSummary: null,
    audioQualityFlag: null,
    anomalyStreams: row.anomaly_streams ?? [],
    countAnomalyScore: toNumber(row.population_anomaly_score),
    countDropScore: toNumber(row.population_drop_score),
    countSpikeScore: toNumber(row.population_spike_score),
    embeddingDriftScore: Math.max(
      toNumber(row.embedding_drift_score),
      toNumber(row.embedding_anomaly_score),
    ),
    embeddingOutlierShare: toNullableNumber(row.embedding_outlier_share),
    embeddingClipCountScored: toNumber(row.embedding_clip_count_scored),
    embeddingBaselineSize: 0,
    embeddingBaselineLevel: null,
    baselineLevel: null,
    baselineSampleSize: 0,
    observedActivity: null,
    observedTotalDetections: null,
    expectedDetectionsPerActiveDay: null,
    expectedTotalDetections: null,
    sovonLagMaxScore: 0,
    sovonLagDeadBirds: 0,
    droughtDryContextFlag: false,
    droughtWetContextFlag: false,
    contextBoost: 0,
  };
}

async function latestModelingSummary(): Promise<AlertsModelingSummary> {
  const run = await loadLatestModelingRun();
  const stats = (run?.stats ?? {}) as Record<string, unknown>;
  const num = (key: string) => {
    const value = Number(stats[key]);
    return Number.isFinite(value) ? value : 0;
  };
  return {
    available: !!run,
    runId: run?.run_id ?? null,
    generatedAt:
      run?.generated_at instanceof Date
        ? run.generated_at.toISOString()
        : run?.generated_at ?? null,
    rowsScored: num("rows_scored"),
    alertsGreen: num("alerts_green"),
    alertsAmber: num("alerts_amber"),
    alertsRed: num("alerts_red"),
    sparseBaselineRows: num("sparse_baseline_rows"),
    queueRows: 0,
    linkedDetections: 0,
    clusterRows: num("cluster_rows"),
    clusterExemplarRows: num("cluster_exemplar_rows"),
    clusteredDetections: 0,
    topReviewReasons: [],
    coverageLabel: "Detail loaded directly from live detection and active weekly score rows.",
    trendFocusLabel: null,
    trendSeries: [],
    hotspots: [],
  };
}

async function loadDirectDetailPoint(
  detectionId: string,
  backend: "birdnet" | "perch_v2",
  observationDate: string | null,
): Promise<AlertVisualPoint | null> {
  const column = backend === "birdnet" ? "birdnet" : "perch_v2";
  const params: unknown[] = [detectionId];
  const clauses = ["b.detection_id = $1", "b.has_processed_clip"];
  if (observationDate && /^\d{4}-\d{2}-\d{2}$/.test(observationDate)) {
    // birdweather_tbl is a TimescaleDB hypertable partitioned by detected_at.
    // Filtering only on detection_id / observation_date can't prune chunks, so
    // a single-row lookup scans all ~700 chunks (multi-second). Add an explicit
    // detected_at range around the observation day (±1 day absorbs any tz skew
    // between observation_date and the UTC detected_at) so the planner prunes to
    // the one chunk that holds this detection.
    const obs = new Date(`${observationDate}T00:00:00Z`);
    const lo = new Date(obs);
    lo.setUTCDate(lo.getUTCDate() - 1);
    const hi = new Date(obs);
    hi.setUTCDate(hi.getUTCDate() + 2);
    params.push(observationDate);
    const dParam = `$${params.length}`;
    params.push(lo.toISOString());
    const loParam = `$${params.length}`;
    params.push(hi.toISOString());
    const hiParam = `$${params.length}`;
    clauses.push(`b.observation_date = ${dParam}::date`);
    clauses.push(`b.detected_at >= ${loParam}::timestamptz`);
    clauses.push(`b.detected_at < ${hiParam}::timestamptz`);
  }

  const result = await query<DetailRow>(
    `
    SELECT
      b.detection_id,
      b.detected_at,
      b.observation_date,
      b.confidence,
      b.species_id,
      b.station_id,
      b.audio_url,
      b.feature_artifacts->>'processed_clip' AS processed_clip,
      b.feature_artifacts->>'smooth_waveform' AS smooth_waveform,
      b.feature_artifacts->>'waveform' AS waveform,
      b.feature_artifacts->>'spectrogram' AS spectrogram,
      b.${column}_pc1 AS pc1,
      b.${column}_pc2 AS pc2,
      COALESCE(
        b.${column}_alert_score,
        weekly_score.score
      ) AS alert_score,
      COALESCE(b.feature_artifacts->'embeddings'->'${column}'->>'backend', '${column}') AS embedding_backend,
      rs.station_name,
      rs.station_state,
      rs.station_country,
      rs.lat AS station_lat,
      rs.lon AS station_lon,
      rs.grid_cell_id,
      sp.common_name,
      sp.scientific_name,
      sp.species_image_url,
      sp.species_thumbnail_url,
      weekly_score.alert_level AS model_alert_level,
      weekly_score.final_score AS model_final_score,
      weekly_score.model_anomaly_score,
      weekly_score.embedding_drift_score,
      weekly_score.embedding_anomaly_score,
      weekly_score.embedding_outlier_share,
      weekly_score.embedding_clip_count_scored,
      weekly_score.population_anomaly_score,
      weekly_score.population_drop_score,
      weekly_score.population_spike_score,
      weekly_score.period_start AS model_period_start,
      weekly_score.period_end AS model_period_end,
      weekly_score.anomaly_streams
    FROM birdweather_tbl b
    LEFT JOIN birdweather_stations_tbl rs ON rs.station_id = b.station_id
    LEFT JOIN species_tbl sp ON sp.species_id = b.species_id
    LEFT JOIN LATERAL (
      -- Lean cutover: read the single bioacoustic anomaly table directly (no
      -- run join). Old column names are exposed as aliases — the anomaly signal
      -- is drift; population signals were dropped and surface as NULL.
      SELECT
        scores.alert_level,
        scores.drift_score AS final_score,
        scores.drift_score AS model_anomaly_score,
        scores.drift_score AS embedding_drift_score,
        scores.drift_score AS embedding_anomaly_score,
        scores.outlier_share AS embedding_outlier_share,
        scores.clip_count AS embedding_clip_count_scored,
        NULL::double precision AS population_anomaly_score,
        NULL::double precision AS population_drop_score,
        NULL::double precision AS population_spike_score,
        scores.period_start,
        scores.period_end,
        NULL::text[] AS anomaly_streams,
        COALESCE(scores.drift_score, 0) AS score
      FROM species_week_anomaly_tbl scores
      WHERE scores.species_id = b.species_id
        AND scores.grid_cell_id = rs.grid_cell_id
        AND scores.period_start <= COALESCE(b.observation_date, b.detected_at::date)
        AND COALESCE(scores.period_end, scores.period_start) >= COALESCE(b.observation_date, b.detected_at::date)
      ORDER BY scores.drift_score DESC NULLS LAST, scores.anomaly_row_id DESC
      LIMIT 1
    ) weekly_score ON TRUE
    WHERE ${clauses.join("\n      AND ")}
    ORDER BY b.detected_at DESC
    LIMIT 1
    `,
    params,
  );

  const row = result.rows[0];
  if (!row) return null;
  const pc1 = toNullableNumber(row.pc1);
  const pc2 = toNullableNumber(row.pc2);
  const alertScore = Math.max(0, Math.min(1, toNumber(row.alert_score)));
  const species =
    row.common_name?.trim() ||
    row.scientific_name?.trim() ||
    (row.species_id ? `Species ${row.species_id}` : "Unknown species");
  return {
    id: row.detection_id,
    speciesId: row.species_id ? String(row.species_id) : null,
    species,
    scientificName: row.scientific_name?.trim() || null,
    imageUrl: row.species_image_url || row.species_thumbnail_url || null,
    station: row.station_name || row.station_id || "Unknown station",
    location: [row.station_state, row.station_country].filter(Boolean).join(", ") || null,
    gridCellId: row.grid_cell_id || null,
    latitude: toNullableNumber(row.station_lat),
    longitude: toNullableNumber(row.station_lon),
    detectedAt: toIso(row.detected_at) ?? String(row.detected_at),
    confidence: toNumber(row.confidence),
    embeddingBackend: row.embedding_backend || backend,
    audioUrls: {
      processed: artifactProxyUrl(row.processed_clip, "processed-audio") || processedClipFallback(row),
      source: row.audio_url || null,
    },
    visualUrls: {
      // Emit the recorded artifact path when present, else null. Recorded
      // artifacts and on-disk PNGs are 1:1, so the old path-guessing fallback
      // only ever 404'd; a null lets the modal render the clip live instead.
      waveform: artifactProxyUrl(row.smooth_waveform, "features"),
      waveformRaw: artifactProxyUrl(row.waveform, "features"),
      spectrogram: artifactProxyUrl(row.spectrogram, "features"),
    },
    backgroundTags: [],
    reviewContext: reviewContextFromRow(row),
    clusterContext: null,
    x: pc1 ?? 0.5,
    y: pc2 ?? 0.5,
    alertScore,
  };
}

const handleGet = async (request: NextRequest) => {
  const detectionId = request.nextUrl.searchParams.get("detectionId")?.trim();
  if (!detectionId) {
    return NextResponse.json({ error: "Missing detectionId" }, { status: 400 });
  }

  const backend =
    request.nextUrl.searchParams.get("backend") === "birdnet" ? "birdnet" : "perch_v2";
  const observationDate = request.nextUrl.searchParams.get("observationDate")?.trim() || null;

  try {
    const directPoint = await loadDirectDetailPoint(detectionId, backend, observationDate);
    if (directPoint) {
      return NextResponse.json({
        point: directPoint,
        modeling: await latestModelingSummary(),
      });
    }

    const record = searchParamsToRecord(request);
    // Bound the query to the clicked detection's day so it resolves regardless
    // of where the default window sits (and stays fast via chunk pruning). The
    // per-detection scoring frontier advances as the projection runs, so the
    // home Live Feed can surface detections beyond the default alerts window.
    if (observationDate) {
      record.dateFrom = observationDate;
      record.dateTo = observationDate;
    }
    record.focusDetectionId = detectionId;
    const data = await loadAlertsData(record);
    const point = findAlertDetailPoint(data, detectionId);
    if (!point) {
      return NextResponse.json({ error: "Detection not found" }, { status: 404 });
    }

    return NextResponse.json({ point, modeling: data.modeling });
  } catch {
    return NextResponse.json(
      { error: "Failed to load alerts embedding detail" },
      { status: 500 },
    );
  }
};

export const GET = withMeasuredApiRoute(handleGet);
