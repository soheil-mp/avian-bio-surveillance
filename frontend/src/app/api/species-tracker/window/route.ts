import { NextRequest, NextResponse } from "next/server";

import { query } from "@/lib/db";
import { withMeasuredApiRoute } from "@/lib/performance/server";
import { createSnapshotCache } from "@/lib/swr-snapshot";

type JsonRecord = Record<string, unknown>;

export type SpeciesTrackerWindowKey =
    | "24h"
    | "week"
    | "month"
    | "season"
    | "year"
    | "total";

export interface SpeciesTrackerWindowHotspot {
    stationId: string;
    stationName: string | null;
    lat: number | null;
    lon: number | null;
    detections: number;
    activeDays: number;
    avgConfidence: number | null;
    lastDetectedAt: string | null;
}

export interface SpeciesTrackerWindowResponse {
    window: SpeciesTrackerWindowKey;
    grain: "day" | "week" | "month" | "year";
    periodsRequested: number;
    hourly: Array<{ hour: number; detections: number }>;
    hotspots: SpeciesTrackerWindowHotspot[];
}

// Each duration maps to a precomputed aggregate grain plus how many of the most
// recent buckets to fold together. 24h/week/month/year read a single latest
// bucket; "season" sums the latest 3 months; "total" sums every year bucket.
const WINDOW_GRAINS: Record<
    SpeciesTrackerWindowKey,
    { grain: "day" | "week" | "month" | "year"; periods: number }
> = {
    "24h": { grain: "day", periods: 1 },
    week: { grain: "week", periods: 1 },
    month: { grain: "month", periods: 1 },
    season: { grain: "month", periods: 3 },
    year: { grain: "year", periods: 1 },
    total: { grain: "year", periods: 100000 },
};

function numberOrNull(value: unknown): number | null {
    if (value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function integerOrZero(value: unknown): number {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
}

function textOrNull(value: unknown): string | null {
    return typeof value === "string" && value.length > 0 ? value : null;
}

// Hourly distribution for the window, summed across the picked network buckets.
// hourly_detection_counts lives in extras on every network grain row, so this is
// a small indexed read against the aggregate (idx_spt_frontend_network_species).
async function loadWindowHourly(
    speciesId: string,
    grain: string,
    periods: number,
): Promise<Array<{ hour: number; detections: number }>> {
    const result = await query<JsonRecord>(`
        WITH picked AS (
            SELECT period_start
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'network'
              AND location_id = 'nl'
              AND species_id = $1
              AND period_grain = $2
            ORDER BY period_start DESC
            LIMIT $3
        )
        SELECT
            (h.key)::int AS hour,
            COALESCE(SUM((h.value)::bigint), 0)::bigint AS detections
        FROM species_tracker_tbl t
        JOIN picked pk ON pk.period_start = t.period_start
        CROSS JOIN LATERAL jsonb_each_text(
            COALESCE(t.extras -> 'hourly_detection_counts', '{}'::jsonb)
        ) AS h(key, value)
        WHERE t.row_kind = 'species'
          AND t.location_type = 'network'
          AND t.location_id = 'nl'
          AND t.species_id = $1
          AND t.period_grain = $2
        GROUP BY (h.key)::int
        ORDER BY (h.key)::int
    `, [speciesId, grain, periods]);

    return result.rows
        .map((row) => ({ hour: integerOrZero(row.hour), detections: integerOrZero(row.detections) }))
        .filter((row) => row.hour >= 0 && row.hour < 24);
}

// Top stations for the window. Station rows now exist at day/week/month/year
// grains (see refresh_species_tracker), so this folds the latest N buckets of
// the requested grain and ranks by summed detections. Detection-weighted mean
// confidence; active_days summed across buckets (calendar-distinct).
async function loadWindowHotspots(
    speciesId: string,
    grain: string,
    periods: number,
): Promise<SpeciesTrackerWindowHotspot[]> {
    const result = await query<JsonRecord>(`
        WITH picked AS (
            SELECT DISTINCT period_start
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'station'
              AND species_id = $1
              AND period_grain = $2
            ORDER BY period_start DESC
            LIMIT $3
        )
        SELECT
            t.station_id,
            COALESCE(station.station_name, station.station_location, t.station_id) AS name,
            station.lat,
            station.lon,
            SUM(t.total_detections)::bigint AS detections,
            SUM(t.active_days)::int AS active_days,
            CASE
                WHEN SUM(t.total_detections) > 0
                THEN SUM(t.confidence_mean * t.total_detections) / NULLIF(SUM(t.total_detections), 0)
                ELSE AVG(t.confidence_mean)
            END AS mean_confidence,
            MAX(t.last_detection_at)::text AS last_detected
        FROM species_tracker_tbl t
        JOIN picked pk ON pk.period_start = t.period_start
        LEFT JOIN birdweather_stations_tbl station
            ON station.station_id = t.station_id
        WHERE t.row_kind = 'species'
          AND t.location_type = 'station'
          AND t.species_id = $1
          AND t.period_grain = $2
        GROUP BY t.station_id, station.station_name, station.station_location, station.lat, station.lon
        ORDER BY detections DESC, active_days DESC, t.station_id ASC
        LIMIT 40
    `, [speciesId, grain, periods]);

    return result.rows.map((row) => ({
        stationId: textOrNull(row.station_id) ?? "unknown",
        stationName: textOrNull(row.name) ?? "Unknown station",
        lat: numberOrNull(row.lat),
        lon: numberOrNull(row.lon),
        detections: integerOrZero(row.detections),
        activeDays: integerOrZero(row.active_days),
        avgConfidence: numberOrNull(row.mean_confidence),
        lastDetectedAt: textOrNull(row.last_detected),
    }));
}

// Disk-backed stale-while-revalidate, keyed by species + window + parts. This
// route had no app-level cache, so every request re-ran the aggregate queries
// and paid the cold-plan cost whenever Postgres' plan/buffers went cold. Now the
// first request for a key blocks once; the rest serve instantly from disk.
const windowSnapshotCache = createSnapshotCache<SpeciesTrackerWindowResponse>({
    name: "species-tracker-window",
    freshMs: 5 * 60_000,
    isEmpty: (payload) => payload.hourly.length === 0 && payload.hotspots.length === 0,
});

const handleGet = async (request: NextRequest) => {
    const speciesId = request.nextUrl.searchParams.get("species_id")?.trim() ?? "";
    const windowParam = (request.nextUrl.searchParams.get("window")?.trim() ?? "total") as SpeciesTrackerWindowKey;
    const parts = request.nextUrl.searchParams.get("parts")?.trim() ?? "both";

    if (!speciesId) {
        return NextResponse.json({ error: "species_id is required" }, { status: 400 });
    }

    const config = WINDOW_GRAINS[windowParam];
    if (!config) {
        return NextResponse.json(
            { error: "window must be one of: 24h, week, month, season, year, total" },
            { status: 400 },
        );
    }

    const wantHourly = parts === "hourly" || parts === "both";
    const wantStations = parts === "stations" || parts === "both";

    try {
        const cacheKey = `${speciesId}::${windowParam}::${parts}`;
        const payload = await windowSnapshotCache.get(
            cacheKey,
            async (): Promise<SpeciesTrackerWindowResponse> => {
                const [hourly, hotspots] = await Promise.all([
                    wantHourly ? loadWindowHourly(speciesId, config.grain, config.periods) : Promise.resolve([]),
                    wantStations ? loadWindowHotspots(speciesId, config.grain, config.periods) : Promise.resolve([]),
                ]);
                return {
                    window: windowParam,
                    grain: config.grain,
                    periodsRequested: config.periods,
                    hourly,
                    hotspots,
                };
            },
        );

        return NextResponse.json(payload, {
            headers: { "Cache-Control": "public, s-maxage=180, stale-while-revalidate=300" },
        });
    } catch (error) {
        console.error("Failed to load species-tracker window", error);
        return NextResponse.json({ error: "Failed to load species-tracker window" }, { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
