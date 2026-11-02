import { NextRequest, NextResponse } from "next/server";

import {
  buildHotspotSummaryResponse,
  LATEST_WEEK_SENTINEL,
  normalizeHotspotGrain,
  normalizeHotspotSource,
  type HotspotGrain,
  type HotspotSource,
  type HotspotSummaryInputRow,
} from "@/app/alerts/hotspot-summary";
import { query } from "@/lib/db";
import { activeScoreRowsCte, loadLatestModelingRun } from "@/lib/modeling-db";
import { withMeasuredApiRoute } from "@/lib/performance/server";

export const dynamic = "force-dynamic";

const RESPONSE_HEADERS = {
  "Cache-Control": "public, s-maxage=300, stale-while-revalidate=300",
};

function readDateInput(value: string | null): string {
  const candidate = value?.trim() ?? "";
  return /^\d{4}-\d{2}-\d{2}$/.test(candidate) ? candidate : "";
}

const BIOACOUSTIC_SIGNAL_THRESHOLD = 0.34;

// Resolve the newest week that actually carries renderable signal for a source,
// scoped to the latest run so it reads one run's rows (index-friendly) instead
// of sorting the whole multi-year scores table. Mirrors the signal predicate
// used by buildHotspotSummaryResponse. Returns "" when no week has signal for
// the source (the overlay is then empty, returned immediately by the caller).
async function resolveLatestSignalWeek(grain: HotspotGrain): Promise<string> {
  // Mirror the cross-run "active score rows" view the render query uses: keep
  // the latest run's row per (species, location, week), then take the newest
  // week carrying a bioacoustic signal. Only the columns needed for the
  // signal test are selected, so the de-dup sort never touches the JSONB
  // payload (the dominant cost of the all-weeks scan).
  const result = await query<{ week: string | null }>(
    `WITH ranked AS (
       SELECT
         scores.period_start,
         scores.alert_level,
         scores.drift_score AS embedding_drift_score,
         scores.drift_score AS embedding_anomaly_score,
         scores.drift_score AS final_score
       FROM species_week_anomaly_tbl scores
       WHERE scores.location_type = $1
     )
     SELECT MAX(period_start)::text AS week
     FROM ranked
     WHERE alert_level IN ('amber', 'red')
        OR GREATEST(
             COALESCE(embedding_drift_score, 0),
             COALESCE(embedding_anomaly_score, 0),
             COALESCE(final_score, 0)
           ) >= ${BIOACOUSTIC_SIGNAL_THRESHOLD}`,
    [grain],
  );
  return result.rows[0]?.week ?? "";
}

// Resolving the latest signal week de-dups the scores table across runs, which
// is cheap warm but not free cold. The answer only changes when a new run
// lands, so memoize per (run, grain, source) with a short TTL: the first
// overlay request pays for it and rapid source/grain toggles reuse it. The
// run_id is part of the key, so a fresh run invalidates stale entries.
const SIGNAL_WEEK_TTL_MS = 60_000;
const latestSignalWeekCache = new Map<
  string,
  { promise: Promise<string>; at: number }
>();

function resolveLatestSignalWeekCached(
  runId: string,
  grain: HotspotGrain,
  source: HotspotSource,
): Promise<string> {
  const key = `${runId}::${grain}::${source}`;
  const now = Date.now();
  const cached = latestSignalWeekCache.get(key);
  if (cached && now - cached.at < SIGNAL_WEEK_TTL_MS) {
    return cached.promise;
  }
  const promise = resolveLatestSignalWeek(grain);
  latestSignalWeekCache.set(key, { promise, at: now });
  promise.catch(() => {
    const current = latestSignalWeekCache.get(key);
    if (current && current.promise === promise) {
      latestSignalWeekCache.delete(key);
    }
  });
  return promise;
}

const handleGet = async (request: NextRequest) => {
  const source = normalizeHotspotSource(request.nextUrl.searchParams.get("source"));
  const grain = normalizeHotspotGrain(request.nextUrl.searchParams.get("grain"));
  const dateFrom = readDateInput(request.nextUrl.searchParams.get("dateFrom"));
  const dateTo = readDateInput(request.nextUrl.searchParams.get("dateTo"));
  // `week` pins the response to a single period_start. Either an ISO date or
  // the literal "latest". Left unnormalized here; buildHotspotSummaryResponse
  // validates it against the weeks that actually carry signal.
  const week = request.nextUrl.searchParams.get("week");
  const run = await loadLatestModelingRun();

  const generatedAt =
    run?.generated_at instanceof Date
      ? run.generated_at.toISOString()
      : (run?.generated_at ?? null);

  if (!run) {
    return NextResponse.json(
      buildHotspotSummaryResponse({ rows: [], source, grain, week }),
      { headers: RESPONSE_HEADERS },
    );
  }

  // Single-week fast path (home-map overlay): when one week is requested and no
  // explicit date range is given, resolve the concrete week up front and push
  // it into SQL so the query scans one week instead of the whole multi-year
  // run. The alerts page sends dateFrom/dateTo (no `week`) and is unaffected.
  const requestedWeek = (week ?? "").trim();
  let pinnedWeek = "";
  if (!dateFrom && !dateTo && requestedWeek) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(requestedWeek)) {
      pinnedWeek = requestedWeek;
    } else if (requestedWeek.toLowerCase() === LATEST_WEEK_SENTINEL) {
      pinnedWeek = await resolveLatestSignalWeekCached(run.run_id, grain, source);
      if (!pinnedWeek) {
        return NextResponse.json(
          buildHotspotSummaryResponse({
            rows: [],
            source,
            grain,
            week,
            runId: run.run_id,
            generatedAt,
          }),
          { headers: RESPONSE_HEADERS },
        );
      }
    }
  }

  const effectiveDateFrom = dateFrom || pinnedWeek;
  const effectiveDateTo = dateTo || pinnedWeek;

  const params: Array<string> = [grain];
  const dateClauses: string[] = [];
  if (effectiveDateFrom) {
    params.push(effectiveDateFrom);
    dateClauses.push(`period_start >= $${params.length}::date`);
  }
  if (effectiveDateTo) {
    params.push(effectiveDateTo);
    dateClauses.push(`period_start <= $${params.length}::date`);
  }
  const dateWhere = dateClauses.length ? `AND ${dateClauses.join(" AND ")}` : "";

  const result = await query<HotspotSummaryInputRow>(
    `
      WITH ${activeScoreRowsCte(["scores.location_type = $1"])},
      score_agg AS (
        SELECT
          location_type,
          location_id,
          COALESCE(grid_cell_id, location_id) AS grid_cell_id,
          NULLIF(MAX(payload->>'location_name'), '') AS location_name,
          NULLIF(MAX(payload->>'admin_level'), '') AS admin_level,
          period_start::text AS period_start,
          MAX(
            GREATEST(
              COALESCE(embedding_drift_score, 0),
              COALESCE(embedding_anomaly_score, 0),
              COALESCE(final_score, 0)
            )
          ) AS bioacoustic_score,
          COUNT(*) FILTER (
            WHERE alert_level IN ('amber', 'red')
               OR GREATEST(
                    COALESCE(embedding_drift_score, 0),
                    COALESCE(embedding_anomaly_score, 0)
                  ) >= 0.34
          ) AS bioacoustic_alert_rows
        FROM active_score_rows
        WHERE location_type = $1
          ${dateWhere}
        GROUP BY location_type, location_id, COALESCE(grid_cell_id, location_id), period_start
      )
      SELECT
        location_type,
        location_id,
        location_name,
        admin_level,
        grid_cell_id,
        period_start,
        COALESCE(bioacoustic_score, 0) AS bioacoustic_score,
        COALESCE(bioacoustic_alert_rows, 0) AS bioacoustic_alert_rows
      FROM score_agg
      ORDER BY period_start DESC, location_type ASC, location_id ASC
    `,
    params,
  );

  return NextResponse.json(
    buildHotspotSummaryResponse({
      rows: result.rows,
      source,
      grain,
      week,
      runId: run.run_id,
      generatedAt,
    }),
    { headers: RESPONSE_HEADERS },
  );
};

export const GET = withMeasuredApiRoute(handleGet);
