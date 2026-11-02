import "server-only";

import { query } from "@/lib/db";

export type ModelingRunRow = {
  run_id: string;
  run_kind: string;
  generated_at: Date | string | null;
  config: Record<string, unknown> | null;
  stats: Record<string, unknown> | null;
  source_coverage: Record<string, unknown> | null;
  validation_report: Record<string, unknown> | null;
  quality_report: Record<string, unknown> | null;
};

export type ModelingAvailableWeek = {
  periodStart: string;
  periodEnd: string;
};

type PayloadRow = {
  payload: Record<string, unknown> | null;
};

type LatestRunResult<T> = {
  run: ModelingRunRow | null;
  rows: T[];
};

function valueToRecordString(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  if (Array.isArray(value)) return value.map(valueToRecordString).filter(Boolean).join(",");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function payloadToStringRecord(
  payload: Record<string, unknown> | null | undefined,
): Record<string, string> {
  if (!payload) return {};
  return Object.fromEntries(
    Object.entries(payload).map(([key, value]) => [key, valueToRecordString(value)]),
  );
}

// ---------------------------------------------------------------------------
// Lean bioacoustic scoring layer
// ---------------------------------------------------------------------------
// The platform was refactored to a single, run-independent bioacoustic anomaly
// table (`species_week_anomaly_tbl`). There is no per-run versioning, no fusion
// / population / cluster signal, and the mortality evidence lives in a permanent
// reference table (`sovon_tbl`). The helpers below replace the previous run_id
// "latest run" machinery (modeling_runs_tbl + DISTINCT-ON-by-generated_at).

/**
 * `active_score_rows` CTE over the lean table, preserving the `scores.*` shape
 * that external callers filter on (e.g. `scores.location_type`,
 * `scores.grid_cell_id`, `scores.species_id`). The lean table already holds at
 * most one row per (species, location, period) via its UNIQUE constraint, so no
 * run join or DISTINCT ON is needed. `anomaly_row_id` is also exposed as
 * `score_row_id` for backward compatibility with older ORDER BY / tie-breaks.
 */
export function activeScoreRowsCte(extraFilters: string[] = []): string {
  const whereClause = extraFilters.length
    ? `\n      WHERE ${extraFilters.join("\n        AND ")}`
    : "";
  return `
    active_score_rows AS (
      SELECT
        scores.*,
        scores.anomaly_row_id AS score_row_id,
        -- Compatibility aliases for the lean cutover: the single anomaly signal
        -- is bioacoustic drift, so the old fusion/embedding score names map to
        -- it. Population signals were dropped and surface as NULL (callers that
        -- still reference them render empty until their panels are removed).
        scores.drift_score AS final_score,
        scores.drift_score AS model_anomaly_score,
        scores.drift_score AS embedding_drift_score,
        scores.drift_score AS embedding_anomaly_score,
        scores.outlier_share AS embedding_outlier_share,
        scores.clip_count AS embedding_clip_count_scored,
        NULL::double precision AS population_anomaly_score,
        NULL::double precision AS population_spike_score,
        NULL::double precision AS population_drop_score
      FROM species_week_anomaly_tbl scores${whereClause}
    )
  `;
}

// Resolving freshness is cheap, but several loaders need the run envelope in a
// single request. Memoize a short TTL so concurrent callers share one query.
const LATEST_RUN_TTL_MS = 60_000;
let syntheticRunCache:
  | { promise: Promise<ModelingRunRow | null>; at: number }
  | null = null;

async function loadLeanRunUncached(): Promise<ModelingRunRow | null> {
  const result = await query<{
    generated_at: string | null;
    row_count: number | string | null;
  }>(
    `SELECT MAX(computed_at)::text AS generated_at, COUNT(*) AS row_count
     FROM species_week_anomaly_tbl`,
  );
  const row = result.rows[0];
  if (!row || row.row_count == null || Number(row.row_count) === 0) {
    return null;
  }
  // Synthetic envelope: keeps provenance-reading callers working without a
  // modeling_runs_tbl. The model-performance page reads static evaluation
  // artifacts instead of a stored validation_report.
  return {
    run_id: "lean_bioacoustic",
    run_kind: "anomaly_scoring",
    generated_at: row.generated_at,
    config: null,
    stats: { scored_rows: Number(row.row_count) },
    source_coverage: null,
    validation_report: null,
    quality_report: null,
  };
}

export async function loadLatestModelingRun(
  _runKind = "anomaly_scoring",
): Promise<ModelingRunRow | null> {
  const now = Date.now();
  if (syntheticRunCache && now - syntheticRunCache.at < LATEST_RUN_TTL_MS) {
    return syntheticRunCache.promise;
  }
  const promise = loadLeanRunUncached();
  syntheticRunCache = { promise, at: now };
  // Evict on failure so the next caller retries instead of caching a rejection.
  promise.catch(() => {
    syntheticRunCache = null;
  });
  return promise;
}

/** Test-only: clear the memoized run resolution so each test starts cold. */
export function resetLatestModelingRunCacheForTests(): void {
  syntheticRunCache = null;
}

// Detection-level evidence is only consumed for recent windows: the trend view
// shows ~6 weeks, hotspots use the latest week, and the embedding canvas plots
// the resolved (recent) window. Loading every week means detoasting ~106k large
// JSONB payloads (~23s server-side on its own), which blew past the 30s
// statement timeout and 500'd the embedding-map and evaluation endpoints.
// Bounding to the most recent weeks keeps the load several times smaller while
// still covering every consumer; the complete week list for the picker is
// sourced separately from loadLatestModelingAvailableWeeks().
const RECENT_SCORE_PAYLOAD_WEEKS = 16;

export async function loadLatestModelingScorePayloads(): Promise<
  LatestRunResult<Record<string, string>>
> {
  const run = await loadLatestModelingRun();
  if (!run) {
    return { run: null, rows: [] };
  }
  // Single canonical table — no run join, no DISTINCT ON. The stored payload
  // carries the full evidence context (mortality + bioacoustic detail). We
  // overlay the lean table's canonical bioacoustic columns so the
  // displayed drift / outlier / alert values always match the lean scoring —
  // the stored payload's alert_level predates the bioacoustic-only alert rule.
  // Bounded to the most recent weeks (RECENT_SCORE_PAYLOAD_WEEKS) so the JSONB
  // detoast stays well under the statement timeout. Ordering is applied in JS
  // afterward to avoid a several-hundred-MB on-disk sort of the payloads (an
  // ORDER BY here spilled ~578MB to disk and took ~40s).
  const result = await query<PayloadRow>(
    `SELECT (
        (COALESCE(payload, '{}'::jsonb)
           - ARRAY['population_anomaly_score','population_spike_score','population_drop_score'])
        || jsonb_build_object(
             'alert_level', alert_level,
             'embedding_drift_score', drift_score,
             'embedding_outlier_share', outlier_share,
             'embedding_clip_count_scored', clip_count,
             'final_score', drift_score,
             'model_anomaly_score', drift_score,
             'embedding_anomaly_score', drift_score
           )
      ) AS payload
     FROM species_week_anomaly_tbl
     WHERE period_start >= (
             SELECT MAX(period_start) FROM species_week_anomaly_tbl
           ) - make_interval(weeks => $1::int)`,
    [RECENT_SCORE_PAYLOAD_WEEKS],
  );
  const rows = result.rows.map((row) => payloadToStringRecord(row.payload));
  // Preserve the previous ASC ordering on (period_start, species_id,
  // location_id) so downstream grouping/tie-breaks are unchanged.
  rows.sort(
    (left, right) =>
      (left.period_start ?? "").localeCompare(right.period_start ?? "") ||
      (left.species_id ?? "").localeCompare(right.species_id ?? "") ||
      (left.location_id ?? "").localeCompare(right.location_id ?? ""),
  );
  return { run, rows };
}

/**
 * Embedding clusters were dropped in the lean refactor. Callers receive an
 * empty set and render nothing for the (now removed) cluster view.
 */
export async function loadLatestModelingClusterPayloads(
  _runId: string,
): Promise<Record<string, string>[]> {
  return [];
}

export async function loadLatestModelingAvailableWeeks(): Promise<
  ModelingAvailableWeek[]
> {
  const result = await query<{ period_start: string; period_end: string }>(
    `SELECT DISTINCT
       period_start::text AS period_start,
       COALESCE(period_end, period_start)::text AS period_end
     FROM species_week_anomaly_tbl
     WHERE period_start IS NOT NULL
     ORDER BY period_start ASC, period_end ASC`,
  );
  return result.rows.map((row) => ({
    periodStart: row.period_start,
    periodEnd: row.period_end,
  }));
}

export async function loadWeeklyAnomalyScoreForDetection(
  speciesId: string,
  detectionId: string,
): Promise<number | null> {
  // Bioacoustic drift is the single anomaly signal now and is already 0..1.
  const result = await query<{ score: number | string | null }>(
    `SELECT drift_score AS score
     FROM species_week_anomaly_tbl
     WHERE species_id = $1
       AND top_detection_ids @> ARRAY[$2]::text[]
     ORDER BY drift_score DESC NULLS LAST, period_start DESC
     LIMIT 1`,
    [speciesId, detectionId],
  );
  const value = result.rows[0]?.score;
  const numeric = value == null ? null : Number(value);
  return numeric != null && Number.isFinite(numeric) && numeric > 0
    ? Math.max(0, Math.min(1, numeric))
    : null;
}

export async function loadFeaturePayloadsForSpecies(
  speciesId: string,
): Promise<Record<string, unknown>[]> {
  // Population/abundance features were dropped with the count-baseline model.
  // The homepage activity builder is repointed at the surviving activity
  // reference (species_tracker_tbl). Per-(location, week) community baselines
  // are derived across all species so a species' share is still computable.
  // NOTE: validate grain/location-type alignment when reworking the
  // species-tracker route UI; this preserves the prior field contract
  // (total_detections / active_days / species_station_count / community_* /
  // period_day_count / month_start).
  const result = await query<Record<string, unknown>>(
    `WITH per_row AS (
       SELECT species_id, location_type, location_id, period_start, period_end,
              period_day_count, total_detections, active_days,
              active_station_count, species_station_count
       FROM species_tracker_tbl
       WHERE period_grain = 'week'
     ),
     community AS (
       SELECT location_type, location_id, period_start,
              SUM(total_detections) AS community_total_detections,
              MAX(active_days) AS community_active_days,
              MAX(active_station_count) AS community_active_station_count
       FROM per_row
       GROUP BY location_type, location_id, period_start
     )
     SELECT
       pr.species_id,
       pr.period_start::text AS period_start,
       date_trunc('month', pr.period_start)::date::text AS month_start,
       pr.total_detections,
       pr.active_days,
       pr.species_station_count,
       c.community_total_detections,
       c.community_active_days,
       c.community_active_station_count,
       pr.period_day_count AS period_day_count
     FROM per_row pr
     JOIN community c USING (location_type, location_id, period_start)
     WHERE pr.species_id = $1
     ORDER BY pr.period_start ASC`,
    [speciesId],
  );
  return result.rows;
}

/**
 * The model-performance page now reads the static evaluation artifacts
 * (evaluation_runs/phase1/...). The lean schema stores no per-run
 * validation_report, so this returns null and the page falls back accordingly.
 */
export async function loadLatestModelPerformanceJson(): Promise<string | null> {
  return null;
}
