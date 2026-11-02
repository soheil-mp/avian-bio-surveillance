import { NextRequest, NextResponse } from "next/server";

import { query } from "@/lib/db";
import { withMeasuredApiRoute } from "@/lib/performance/server";
import { createSnapshotCache } from "@/lib/swr-snapshot";

type JsonRecord = Record<string, unknown>;

export type SpeciesTrackerClimateWindowKey =
    | "24h"
    | "week"
    | "month"
    | "season"
    | "year"
    | "total";

// One IQR/box per variable: the distribution of station-reported weather
// (BirdWeather) when this species was detected calling, over the window.
//
// Served entirely from the precomputed species_weather_envelope_tbl
// (refresh_species_weather_envelope), so a request is a primary-key lookup --
// no hypertable scan and no station_weather JSONB access at request time.
// Solar / drought are not in the BirdWeather feed; those join later from
// KNMI/CDS once ingested.
export interface SpeciesTrackerClimateStat {
    key: string;
    label: string;
    unit: string;
    p5: number | null;
    q1: number | null;
    median: number | null;
    q3: number | null;
    p95: number | null;
}

export interface SpeciesTrackerClimateResponse {
    window: SpeciesTrackerClimateWindowKey;
    trailingDays: number;
    sampleCount: number;
    variables: SpeciesTrackerClimateStat[];
}

const WINDOW_DAYS: Record<SpeciesTrackerClimateWindowKey, number> = {
    "24h": 1,
    week: 7,
    month: 31,
    season: 92,
    year: 366,
    total: 100000,
};

// Canonical variable order + display metadata. var_key matches the rows written
// by refresh_species_weather_envelope().
const WEATHER_VARS: ReadonlyArray<{ key: string; label: string; unit: string }> = [
    { key: "temperature", label: "Temperature", unit: "°C" },
    { key: "humidity", label: "Humidity", unit: "%" },
    { key: "wind", label: "Wind", unit: "m/s" },
    { key: "pressure", label: "Pressure", unit: "hPa" },
    { key: "cloud", label: "Cloud cover", unit: "%" },
];

function toNum(value: unknown): number | null {
    if (value === null || value === undefined) return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
}

async function loadClimateEnvelope(
    speciesId: string,
    windowKey: SpeciesTrackerClimateWindowKey,
): Promise<{ sampleCount: number; variables: SpeciesTrackerClimateStat[] }> {
    const result = await query<JsonRecord>(
        `SELECT var_key, p5, q1, median, q3, p95, sample_count
           FROM species_weather_envelope_tbl
          WHERE species_id = $1 AND window_key = $2`,
        [speciesId, windowKey],
    );

    const byKey = new Map<string, JsonRecord>();
    let sampleCount = 0;
    for (const row of result.rows) {
        const key = String(row.var_key);
        byKey.set(key, row);
        const n = Number(row.sample_count);
        if (Number.isFinite(n) && n > sampleCount) sampleCount = n;
    }

    const variables = WEATHER_VARS.map((meta) => {
        const row = byKey.get(meta.key);
        return {
            key: meta.key,
            label: meta.label,
            unit: meta.unit,
            p5: row ? toNum(row.p5) : null,
            q1: row ? toNum(row.q1) : null,
            median: row ? toNum(row.median) : null,
            q3: row ? toNum(row.q3) : null,
            p95: row ? toNum(row.p95) : null,
        };
    });

    return { sampleCount, variables };
}

const climateSnapshotCache = createSnapshotCache<SpeciesTrackerClimateResponse>({
    name: "species-tracker-climate",
    freshMs: 5 * 60_000,
    isEmpty: (payload) => payload.sampleCount === 0,
});

const handleGet = async (request: NextRequest) => {
    const speciesId = request.nextUrl.searchParams.get("species_id")?.trim() ?? "";
    const windowParam = (request.nextUrl.searchParams.get("window")?.trim() ?? "total") as SpeciesTrackerClimateWindowKey;

    if (!speciesId) {
        return NextResponse.json({ error: "species_id is required" }, { status: 400 });
    }

    const trailingDays = WINDOW_DAYS[windowParam];
    if (!trailingDays) {
        return NextResponse.json(
            { error: "window must be one of: 24h, week, month, season, year, total" },
            { status: 400 },
        );
    }

    try {
        const cacheKey = `${speciesId}::${windowParam}`;
        const payload = await climateSnapshotCache.get(
            cacheKey,
            async (): Promise<SpeciesTrackerClimateResponse> => {
                const { sampleCount, variables } = await loadClimateEnvelope(speciesId, windowParam);
                return {
                    window: windowParam,
                    trailingDays,
                    sampleCount,
                    variables,
                };
            },
        );
        return NextResponse.json(payload, {
            headers: { "Cache-Control": "public, s-maxage=300, stale-while-revalidate=600" },
        });
    } catch (error) {
        console.error("Failed to load species-tracker climate", error);
        return NextResponse.json({ error: "Failed to load species-tracker climate" }, { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
