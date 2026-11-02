import { unstable_cache } from "next/cache";
import { NextRequest, NextResponse } from "next/server";

import { query } from "@/lib/db";
import { withMeasuredApiRoute } from "@/lib/performance/server";
import { createSnapshotCache } from "@/lib/swr-snapshot";

type JsonRecord = Record<string, unknown>;

export type SpeciesTrackerHabitatWindowKey =
    | "24h"
    | "week"
    | "month"
    | "season"
    | "year"
    | "total";

// One point per month: detection-weighted habitat metrics across the grid cells
// where this species is detected. Each field is a seasonal-rhythm series.
export interface SpeciesTrackerHabitatSeriesPoint {
    bucketStart: string;
    label: string;
    ndvi: number | null;
    gpp: number | null;
    lstDay: number | null;
    lstNight: number | null;
    soilMoisture: number | null;
    precip: number | null;
    et: number | null;
    snowCover: number | null;
    no2: number | null;
}

// Detection-weighted land-cover mix (Dynamic World probability bands,
// renormalized to 100%) over the window.
export interface SpeciesTrackerHabitatComposition {
    key: string;
    label: string;
    pct: number;
}

// Detection-weighted scalar habitat indicators over the window.
export interface SpeciesTrackerHabitatIndicator {
    key: string;
    label: string;
    value: number | null;
    unit: string;
}

export interface SpeciesTrackerHabitatResponse {
    window: SpeciesTrackerHabitatWindowKey;
    trailingDays: number;
    cellCount: number;
    sampleCount: number;
    series: SpeciesTrackerHabitatSeriesPoint[];
    composition: SpeciesTrackerHabitatComposition[];
    indicators: SpeciesTrackerHabitatIndicator[];
}

// Trailing-day spans, matching the Climate envelope route. GEE composites are
// roughly biweekly/monthly, so 24h/week resolve to "the latest composite that
// falls in the trailing range" rather than sub-daily data; month/season/year/
// total are the meaningful windows.
const WINDOW_DAYS: Record<SpeciesTrackerHabitatWindowKey, number> = {
    "24h": 1,
    week: 7,
    month: 31,
    season: 92,
    year: 366,
    total: 100000,
};

// Dynamic World land-cover bands, in display order. Probabilities are
// detection-weighted and renormalized to 100% across these classes.
const DW_BANDS: ReadonlyArray<{ key: string; column: string; label: string }> = [
    { key: "trees", column: "dw_trees_prob", label: "Trees" },
    { key: "grass", column: "dw_grass_prob", label: "Grass" },
    { key: "crops", column: "dw_crops_prob", label: "Crops" },
    { key: "shrub", column: "dw_shrub_and_scrub_prob", label: "Shrub" },
    { key: "built", column: "dw_built_prob", label: "Built-up" },
    { key: "water", column: "dw_water_prob", label: "Water" },
    { key: "flooded", column: "dw_flooded_vegetation_prob", label: "Wetland" },
    { key: "bare", column: "dw_bare_prob", label: "Bare" },
    { key: "snow", column: "dw_snow_and_ice_prob", label: "Snow/ice" },
];

function parseWindow(value: string | null): SpeciesTrackerHabitatWindowKey {
    switch (value) {
        case "24h":
        case "week":
        case "month":
        case "season":
        case "year":
            return value;
        default:
            return "total";
    }
}

function numberOrNull(value: unknown): number | null {
    if (value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function monthLabel(bucketStart: string): string {
    const parsed = new Date(`${bucketStart}T00:00:00Z`);
    if (!Number.isFinite(parsed.getTime())) return bucketStart;
    return parsed.toLocaleDateString("en-GB", {
        month: "short",
        year: "2-digit",
        timeZone: "UTC",
    });
}

// Detection-weighted mean SQL over the windowed rows: SUM(metric * weight) /
// SUM(weight where metric present). The per-cell weight (g.w, carried through
// the windowed CTE) is the species' all-time detection total in that cell, so
// the metric leans toward where the bird is actually active. The window bounds
// the GEE observation dates, not the weight.
function weightedMean(column: string): string {
    return `SUM(g.${column} * g.w) / NULLIF(SUM(g.w) FILTER (WHERE g.${column} IS NOT NULL), 0)`;
}

async function loadHabitat(
    speciesId: string,
    windowKey: SpeciesTrackerHabitatWindowKey,
): Promise<SpeciesTrackerHabitatResponse> {
    const trailingDays = WINDOW_DAYS[windowKey];
    const empty: SpeciesTrackerHabitatResponse = {
        window: windowKey,
        trailingDays,
        cellCount: 0,
        sampleCount: 0,
        series: [],
        composition: [],
        indicators: [],
    };

    if (!speciesId) return empty;

    // Shared CTE prelude: the species' grid-cell weight map, the GEE anchor
    // date, and the windowed join. Reused by both queries so the window
    // semantics stay identical. The species' cells are a subset of the GEE grid
    // (verified), so the join never widens the row set.
    const prelude = `
        WITH sp_cells AS (
            SELECT grid_cell_id, SUM(total_detections)::numeric AS w
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'grid'
              AND species_id = $1
              AND grid_cell_id IS NOT NULL
            GROUP BY grid_cell_id
        ),
        bounds AS (
            SELECT MAX(observation_date) AS max_d FROM google_earth_engine_tbl
        ),
        windowed AS (
            SELECT g.*, c.w
            FROM google_earth_engine_tbl g
            JOIN sp_cells c USING (grid_cell_id)
            CROSS JOIN bounds b
            WHERE g.observation_date > (b.max_d - $2::int)
        )
    `;

    const dwSelect = DW_BANDS.map(
        (band) => `${weightedMean(band.column)} AS ${band.key}`,
    ).join(",\n                    ");

    const [seriesResult, aggregateResult] = await Promise.all([
        query<JsonRecord>(
            `${prelude}
             SELECT date_trunc('month', g.observation_date)::date::text AS bucket_start,
                    ${weightedMean("ndvi")} AS ndvi,
                    ${weightedMean("modis_gpp_kg_c_m2")} AS gpp,
                    ${weightedMean("lst_day_celsius")} AS lst_day,
                    ${weightedMean("lst_night_celsius")} AS lst_night,
                    ${weightedMean("soil_moisture")} AS soil_moisture,
                    ${weightedMean("total_precipitation_mm")} AS precip,
                    ${weightedMean("et_mm_8day")} AS et,
                    ${weightedMean("modis_snow_cover_pct")} AS snow_cover,
                    ${weightedMean("no2_mol_m2")} AS no2
             FROM windowed g
             GROUP BY 1
             ORDER BY 1`,
            [speciesId, trailingDays],
        ),
        query<JsonRecord>(
            `${prelude}
             SELECT COUNT(*)::int AS sample_count,
                    COUNT(DISTINCT g.grid_cell_id)::int AS cell_count,
                    ${dwSelect},
                    ${weightedMean("forest_cover_pct")} AS forest_cover_pct,
                    ${weightedMean("human_modification_index")} AS human_modification_index,
                    ${weightedMean("elevation_m")} AS elevation_m,
                    ${weightedMean("slope_degrees")} AS slope_degrees,
                    ${weightedMean("soil_ph")} AS soil_ph
             FROM windowed g`,
            [speciesId, trailingDays],
        ),
    ]);

    const series: SpeciesTrackerHabitatSeriesPoint[] = seriesResult.rows.map((row) => {
        const bucketStart = String(row.bucket_start ?? "");
        return {
            bucketStart,
            label: monthLabel(bucketStart),
            ndvi: numberOrNull(row.ndvi),
            gpp: numberOrNull(row.gpp),
            lstDay: numberOrNull(row.lst_day),
            lstNight: numberOrNull(row.lst_night),
            soilMoisture: numberOrNull(row.soil_moisture),
            precip: numberOrNull(row.precip),
            et: numberOrNull(row.et),
            snowCover: numberOrNull(row.snow_cover),
            no2: numberOrNull(row.no2),
        };
    });

    const agg = aggregateResult.rows[0] ?? {};
    const sampleCount = numberOrNull(agg.sample_count) ?? 0;
    const cellCount = numberOrNull(agg.cell_count) ?? 0;

    // Renormalize the weighted DW probabilities to a 0–100% composition.
    const rawBands = DW_BANDS.map((band) => ({
        key: band.key,
        label: band.label,
        value: Math.max(0, numberOrNull(agg[band.key]) ?? 0),
    }));
    const bandTotal = rawBands.reduce((sum, band) => sum + band.value, 0);
    const composition: SpeciesTrackerHabitatComposition[] =
        bandTotal > 0
            ? rawBands.map((band) => ({
                  key: band.key,
                  label: band.label,
                  pct: Math.round((band.value / bandTotal) * 1000) / 10,
              }))
            : [];

    const indicators: SpeciesTrackerHabitatIndicator[] = [
        { key: "elevation", label: "Elevation", value: numberOrNull(agg.elevation_m), unit: "m" },
        { key: "slope", label: "Slope", value: numberOrNull(agg.slope_degrees), unit: "°" },
        { key: "forest", label: "Forest cover", value: numberOrNull(agg.forest_cover_pct), unit: "%" },
        { key: "hmi", label: "Human footprint", value: numberOrNull(agg.human_modification_index), unit: "idx" },
        { key: "soilph", label: "Soil pH", value: numberOrNull(agg.soil_ph), unit: "" },
    ];

    return {
        window: windowKey,
        trailingDays,
        cellCount,
        sampleCount,
        series,
        composition,
        indicators,
    };
}

const loadHabitatCached = unstable_cache(
    loadHabitat,
    ["species-tracker-habitat-v2"],
    { revalidate: 900 },
);

const habitatSnapshotCache = createSnapshotCache<SpeciesTrackerHabitatResponse>({
    name: "species-tracker-habitat",
    freshMs: 15 * 60_000,
    isEmpty: (payload) => payload.sampleCount === 0 && payload.cellCount === 0,
});

const handleGet = async (request: NextRequest) => {
    try {
        const speciesId = request.nextUrl.searchParams.get("species_id") ?? "";
        const windowKey = parseWindow(request.nextUrl.searchParams.get("window"));
        const cacheKey = `${speciesId || "default"}::${windowKey}`;
        const payload = await habitatSnapshotCache.get(
            cacheKey,
            () => loadHabitatCached(speciesId, windowKey),
        );
        return NextResponse.json(payload, {
            headers: { "Cache-Control": "public, s-maxage=900, stale-while-revalidate=900" },
        });
    } catch (error) {
        console.error("Failed to load species tracker habitat", error);
        return NextResponse.json({ error: "Failed to load habitat" }, { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
