import { NextRequest, NextResponse } from "next/server";

import { query } from "@/lib/db";
import { withMeasuredApiRoute } from "@/lib/performance/server";

import { lastCompleteUtcYear } from "../helpers";

type JsonRecord = Record<string, unknown>;

export type SpeciesTrackerDrilldownKind = "hour" | "station";

export interface SpeciesTrackerDrilldownClip {
    detectionId: string;
    detectedAt: string | null;
    confidence: number | null;
    stationId: string | null;
    stationName: string | null;
    audioUrls: {
        raw: string | null;
        processed: string | null;
        direct: string | null;
    };
}

export interface SpeciesTrackerHourDrilldownResponse {
    kind: "hour";
    year: number;
    hour: number;
    label: string;
    detections: number;
    activeDays: number;
    activeStations: number;
    representativeClip: SpeciesTrackerDrilldownClip | null;
}

export interface SpeciesTrackerStationDrilldownResponse {
    kind: "station";
    year: number;
    stationId: string;
    stationName: string | null;
    detections: number;
    activeDays: number;
    activeStations: number;
    avgConfidence: number | null;
    lastDetectedAt: string | null;
    representativeClip: SpeciesTrackerDrilldownClip | null;
}

export type SpeciesTrackerDrilldownResponse =
    | SpeciesTrackerHourDrilldownResponse
    | SpeciesTrackerStationDrilldownResponse;

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

function formatHourLabel(hour: number): string {
    const normalized = ((hour % 24) + 24) % 24;
    return `${String(normalized).padStart(2, "0")}:00-${String(normalized).padStart(2, "0")}:59 UTC`;
}

function selectedYearSql(locationType: "network" | "station"): string {
    const locationClause = locationType === "network"
        ? "AND tracker.location_id = 'nl'"
        : "";

    return `
        WITH preferred_year AS (
            SELECT make_date($2::int, 1, 1) AS year_start
        ), selected_year AS (
            SELECT COALESCE(
                (
                    SELECT tracker.period_start
                    FROM species_tracker_tbl tracker
                    CROSS JOIN preferred_year
                    WHERE tracker.row_kind = 'species'
                      AND tracker.location_type = '${locationType}'
                                            ${locationClause}
                      AND tracker.period_grain = 'year'
                      AND tracker.species_id = $1
                      AND tracker.period_start = preferred_year.year_start
                    LIMIT 1
                ),
                (
                    SELECT MAX(tracker.period_start)
                    FROM species_tracker_tbl tracker
                    WHERE tracker.row_kind = 'species'
                      AND tracker.location_type = '${locationType}'
                                            ${locationClause}
                      AND tracker.period_grain = 'year'
                      AND tracker.species_id = $1
                )
            ) AS year_start
        )
    `;
}

async function loadHourDrilldown(speciesId: string, hour: number): Promise<SpeciesTrackerHourDrilldownResponse> {
    const year = lastCompleteUtcYear();
    const summary = await query<JsonRecord>(`
                ${selectedYearSql("network")}
        SELECT
                        COALESCE((tracker.extras -> 'hourly_detection_counts' ->> $3)::int, 0) AS detections,
                        tracker.active_days::int AS active_days,
            COALESCE(tracker.species_station_count, 0)::int AS active_stations,
            tracker.last_detection_at::text AS last_detected_at
                FROM species_tracker_tbl tracker
                JOIN selected_year
                    ON selected_year.year_start = tracker.period_start
                WHERE tracker.row_kind = 'species'
                    AND tracker.location_type = 'network'
                    AND tracker.location_id = 'nl'
                    AND tracker.period_grain = 'year'
                    AND tracker.species_id = $1
        `, [speciesId, year, String(hour)]);
    const detections = integerOrZero(summary.rows[0]?.detections);
    const activeDays = integerOrZero(summary.rows[0]?.active_days);
    const activeStations = integerOrZero(summary.rows[0]?.active_stations);
    return {
        kind: "hour",
        year,
        hour,
        label: formatHourLabel(hour),
        detections,
        activeDays,
        activeStations,
        representativeClip: null,
    };
}

async function loadStationDrilldown(speciesId: string, stationId: string): Promise<SpeciesTrackerStationDrilldownResponse> {
    const year = lastCompleteUtcYear();
    const summary = await query<JsonRecord>(`
                ${selectedYearSql("station")}
        SELECT
                        COALESCE(station.station_name, station.station_location, tracker.station_id) AS station_name,
                        tracker.total_detections::bigint AS detections,
                        tracker.active_days::int AS active_days,
                        COALESCE(tracker.species_station_count, 1)::int AS active_stations,
                        tracker.confidence_mean::double precision AS avg_confidence,
                        tracker.last_detection_at::text AS last_detected_at
                FROM species_tracker_tbl tracker
        LEFT JOIN birdweather_stations_tbl station
                    ON station.station_id = tracker.station_id
                JOIN selected_year
                    ON selected_year.year_start = tracker.period_start
                WHERE tracker.row_kind = 'species'
                    AND tracker.location_type = 'station'
                    AND tracker.period_grain = 'year'
                    AND tracker.species_id = $1
                    AND tracker.station_id = $3
        `, [speciesId, year, stationId]);
    const row = summary.rows[0] ?? {};

    return {
        kind: "station",
        year,
        stationId,
        stationName: textOrNull(row.station_name),
        detections: integerOrZero(row.detections),
        activeDays: integerOrZero(row.active_days),
        activeStations: integerOrZero(row.active_stations),
        avgConfidence: numberOrNull(row.avg_confidence),
        lastDetectedAt: textOrNull(row.last_detected_at),
        representativeClip: null,
    };
}

const handleGet = async (request: NextRequest) => {
    const speciesId = request.nextUrl.searchParams.get("species_id")?.trim() ?? "";
    const kind = request.nextUrl.searchParams.get("kind")?.trim() ?? "";

    if (!speciesId) {
        return NextResponse.json({ error: "species_id is required" }, { status: 400 });
    }

    try {
        if (kind === "hour") {
            const hour = Number(request.nextUrl.searchParams.get("hour"));
            if (!Number.isInteger(hour) || hour < 0 || hour > 23) {
                return NextResponse.json({ error: "hour must be an integer between 0 and 23" }, { status: 400 });
            }
            return NextResponse.json(await loadHourDrilldown(speciesId, hour), {
                headers: { "Cache-Control": "public, s-maxage=300, stale-while-revalidate=300" },
            });
        }

        if (kind === "station") {
            const stationId = request.nextUrl.searchParams.get("station_id")?.trim() ?? "";
            if (!stationId) {
                return NextResponse.json({ error: "station_id is required for station drilldown" }, { status: 400 });
            }
            return NextResponse.json(await loadStationDrilldown(speciesId, stationId), {
                headers: { "Cache-Control": "public, s-maxage=300, stale-while-revalidate=300" },
            });
        }

        return NextResponse.json({ error: "kind must be 'hour' or 'station'" }, { status: 400 });
    } catch (error) {
        console.error("Failed to load species-tracker drilldown", error);
        return NextResponse.json({ error: "Failed to load species-tracker drilldown" }, { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);