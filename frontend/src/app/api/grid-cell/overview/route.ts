import { unstable_cache } from "next/cache";
import { NextRequest, NextResponse } from "next/server";

import { query } from "@/lib/db";
import { activeScoreRowsCte } from "@/lib/modeling-db";
import { withMeasuredApiRoute } from "@/lib/performance/server";

type Num = number | null;
type Row = Record<string, unknown>;

export type GridAlertLevel = "green" | "amber" | "red";

export interface GridCellIdentity {
    gridCellId: string;
    referenceLabel: string;
    gridSizeKm: number | null;
    gridSystem: string | null;
    lat: number | null;
    lon: number | null;
    bbox: { minLat: number; minLon: number; maxLat: number; maxLon: number } | null;
}

export interface GridHabitatComposition {
    key: string;
    label: string;
    pct: number;
}
export interface GridHabitatSeriesPoint {
    bucketStart: string;
    label: string;
    ndvi: Num;
}
export interface GridHabitatBlock {
    latestDate: string | null;
    ndvi: Num;
    forestPct: Num;
    builtPct: Num;
    humanFootprint: Num;
    elevationM: Num;
    lstDayC: Num;
    landCoverLabel: string | null;
    composition: GridHabitatComposition[];
    ndviSeries: GridHabitatSeriesPoint[];
}

export interface GridNearest {
    referenceLabel: string;
    distanceKm: number;
    bearing: string;
    headline: string;
}

export interface GridAlertSpecies {
    speciesEnglish: string;
    amber: number;
    red: number;
    peakScore: Num;
}
export interface GridAlertSeriesPoint {
    bucketStart: string;
    label: string;
    amber: number;
    red: number;
}
export interface GridAlertsBlock {
    hasData: boolean;
    latestPeriod: string | null;
    latestLevel: GridAlertLevel | null;
    red: number;
    amber: number;
    green: number;
    avgScore: Num;
    weeksActive: number;
    speciesAffected: number;
    peakPeriod: string | null;
    topSpecies: GridAlertSpecies[];
    series: GridAlertSeriesPoint[];
    nearest: GridNearest | null;
}

export interface GridSpeciesTop {
    speciesId: string;
    commonName: string | null;
    thumbnailUrl: string | null;
    detections: number;
}
export interface GridSpeciesSeriesPoint {
    bucketStart: string;
    label: string;
    detections: number;
}
export interface GridSpeciesBlock {
    hasData: boolean;
    totalDetections: number;
    stationCount: number;
    firstDetection: string | null;
    lastDetection: string | null;
    top: GridSpeciesTop[];
    series: GridSpeciesSeriesPoint[];
}

export interface GridCellOverviewResponse {
    identity: GridCellIdentity;
    habitat: GridHabitatBlock | null;
    alerts: GridAlertsBlock;
    species: GridSpeciesBlock;
}

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

function toNum(value: unknown): Num {
    if (value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}
function toInt(value: unknown): number {
    return toNum(value) ?? 0;
}
function monthLabel(bucketStart: string): string {
    const parsed = new Date(`${bucketStart}T00:00:00Z`);
    if (!Number.isFinite(parsed.getTime())) return bucketStart;
    return parsed.toLocaleDateString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" });
}

// Compass bearing (8-point) from origin → target, for the "nearest monitored
// cell" hint shown when a clicked cell has no surveillance of its own.
function bearingLabel(fromLat: number, fromLon: number, toLat: number, toLon: number): string {
    const dLon = ((toLon - fromLon) * Math.PI) / 180;
    const y = Math.sin(dLon) * Math.cos((toLat * Math.PI) / 180);
    const x =
        Math.cos((fromLat * Math.PI) / 180) * Math.sin((toLat * Math.PI) / 180) -
        Math.sin((fromLat * Math.PI) / 180) * Math.cos((toLat * Math.PI) / 180) * Math.cos(dLon);
    const deg = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
}

// Nearest cell (excluding self) that carries the given surveillance signal,
// using grid-cell centroids. `signalSql` filters the source rows; `headlineSql`
// is the count surfaced in the hint. Returns null if nothing qualifies.
async function loadNearest(
    gridCellId: string,
    table: string,
    signalSql: string,
    headlineSql: string,
    headlineSuffix: string,
): Promise<GridNearest | null> {
    const result = await query<Row>(
        `WITH origin AS (SELECT lat, lon FROM grid_cells_tbl WHERE grid_cell_id = $1)
         SELECT gc.grid_row, gc.grid_col, gc.lat, gc.lon, o.lat AS o_lat, o.lon AS o_lon,
                ${headlineSql} AS headline,
                2 * 6371 * asin(sqrt(
                    power(sin(radians(gc.lat - o.lat) / 2), 2) +
                    cos(radians(o.lat)) * cos(radians(gc.lat)) *
                    power(sin(radians(gc.lon - o.lon) / 2), 2))) AS dist_km
         FROM ${table} t
         JOIN grid_cells_tbl gc ON gc.grid_cell_id = t.grid_cell_id
         CROSS JOIN origin o
         WHERE t.grid_cell_id <> $1 AND ${signalSql}
         GROUP BY gc.grid_row, gc.grid_col, gc.lat, gc.lon, o.lat, o.lon
         ORDER BY dist_km ASC
         LIMIT 1`,
        [gridCellId],
    );
    const row = result.rows[0];
    if (!row) return null;
    const oLat = toNum(row.o_lat);
    const oLon = toNum(row.o_lon);
    const tLat = toNum(row.lat);
    const tLon = toNum(row.lon);
    const dist = toNum(row.dist_km);
    if (oLat == null || oLon == null || tLat == null || tLon == null || dist == null) return null;
    return {
        referenceLabel: `R${toInt(row.grid_row)} · C${toInt(row.grid_col)}`,
        distanceKm: Math.round(dist),
        bearing: bearingLabel(oLat, oLon, tLat, tLon),
        headline: `${toInt(row.headline)} ${headlineSuffix}`,
    };
}

async function loadOverview(gridCellId: string): Promise<GridCellOverviewResponse> {
    const [
        identityR,
        habitatR,
        ndviR,
        alertSumR,
        alertLevelR,
        alertSpeciesR,
        alertSeriesR,
        speciesTopR,
        speciesSumR,
        speciesSeriesR,
        stationsR,
    ] = await Promise.all([
        query<Row>(
            `SELECT lat, lon, bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon,
                    grid_size_km, grid_system, grid_row, grid_col
             FROM grid_cells_tbl WHERE grid_cell_id = $1`,
            [gridCellId],
        ),
        query<Row>(
            `SELECT observation_date::text AS d, ndvi, forest_cover_pct, dw_built_prob,
                    human_modification_index, elevation_m, lst_day_celsius, dynamic_world_label,
                    dw_trees_prob, dw_grass_prob, dw_crops_prob, dw_shrub_and_scrub_prob,
                    dw_water_prob, dw_flooded_vegetation_prob, dw_bare_prob, dw_snow_and_ice_prob
             FROM google_earth_engine_tbl
             WHERE grid_cell_id = $1 AND dw_trees_prob IS NOT NULL
             ORDER BY observation_date DESC LIMIT 1`,
            [gridCellId],
        ),
        query<Row>(
            `SELECT date_trunc('month', observation_date)::date::text AS m, avg(ndvi) AS ndvi
             FROM google_earth_engine_tbl
             WHERE grid_cell_id = $1 AND ndvi IS NOT NULL
             GROUP BY 1 ORDER BY 1`,
            [gridCellId],
        ),
        query<Row>(
            `WITH ${activeScoreRowsCte(["scores.grid_cell_id = $1"])}
             SELECT count(*) FILTER (WHERE alert_level = 'red')::int AS red,
                    count(*) FILTER (WHERE alert_level = 'amber')::int AS amber,
                    count(*) FILTER (WHERE alert_level = 'green')::int AS green,
                    count(DISTINCT period_start) FILTER (WHERE alert_level IN ('amber','red'))::int AS weeks_active,
                    count(DISTINCT species_id) FILTER (WHERE alert_level IN ('amber','red'))::int AS species_affected,
                    avg(final_score) AS avg_score,
                    max(period_start)::text AS latest
             FROM active_score_rows WHERE grid_cell_id = $1`,
            [gridCellId],
        ),
        query<Row>(
            `WITH ${activeScoreRowsCte(["scores.grid_cell_id = $1"])}
             SELECT CASE
                      WHEN bool_or(alert_level = 'red') THEN 'red'
                      WHEN bool_or(alert_level = 'amber') THEN 'amber'
                      WHEN count(*) > 0 THEN 'green'
                    END AS lvl
             FROM active_score_rows
             WHERE grid_cell_id = $1
               AND period_start = (SELECT max(period_start)
                                   FROM active_score_rows WHERE grid_cell_id = $1)`,
            [gridCellId],
        ),
        query<Row>(
            `WITH ${activeScoreRowsCte(["scores.grid_cell_id = $1"])}
             SELECT species_english,
                    count(*) FILTER (WHERE alert_level = 'amber')::int AS amber,
                    count(*) FILTER (WHERE alert_level = 'red')::int AS red,
                    max(final_score) AS peak
             FROM active_score_rows
             WHERE grid_cell_id = $1 AND alert_level IN ('amber','red')
             GROUP BY 1 ORDER BY red DESC, amber DESC, peak DESC NULLS LAST LIMIT 5`,
            [gridCellId],
        ),
        query<Row>(
            `WITH ${activeScoreRowsCte(["scores.grid_cell_id = $1"])}
             SELECT date_trunc('month', period_start)::date::text AS m,
                    count(*) FILTER (WHERE alert_level = 'amber')::int AS amber,
                    count(*) FILTER (WHERE alert_level = 'red')::int AS red
             FROM active_score_rows
             WHERE grid_cell_id = $1 GROUP BY 1 ORDER BY 1`,
            [gridCellId],
        ),
        query<Row>(
            `SELECT t.species_id,
                    sp.common_name,
                    COALESCE(sp.species_thumbnail_url, sp.phylopic_thumb_url) AS thumb,
                    SUM(t.total_detections)::bigint AS det
             FROM species_tracker_tbl t
             LEFT JOIN species_tbl sp ON sp.species_id = t.species_id
             WHERE t.row_kind = 'species' AND t.location_type = 'grid' AND t.grid_cell_id = $1
             GROUP BY 1, 2, 3 ORDER BY det DESC LIMIT 6`,
            [gridCellId],
        ),
        query<Row>(
            `SELECT SUM(total_detections)::bigint AS det,
                    MIN(first_detection_at)::text AS first_d,
                    MAX(last_detection_at)::text AS last_d
             FROM species_tracker_tbl
             WHERE row_kind = 'species' AND location_type = 'grid' AND grid_cell_id = $1`,
            [gridCellId],
        ),
        query<Row>(
            `SELECT date_trunc('month', period_start)::date::text AS m, SUM(total_detections)::bigint AS det
             FROM species_tracker_tbl
             WHERE row_kind = 'species' AND location_type = 'grid' AND grid_cell_id = $1
             GROUP BY 1 ORDER BY 1`,
            [gridCellId],
        ),
        query<Row>(
            `SELECT count(*)::int AS stations FROM birdweather_stations_tbl WHERE grid_cell_id = $1 AND total_detections > 0`,
            [gridCellId],
        ),
    ]);

    // ---- identity ----
    const idRow = identityR.rows[0] ?? {};
    const gridRow = toNum(idRow.grid_row);
    const gridCol = toNum(idRow.grid_col);
    const referenceLabel =
        gridRow != null && gridCol != null ? `R${gridRow} · C${gridCol}` : gridCellId;
    const bMinLat = toNum(idRow.bbox_min_lat);
    const bMinLon = toNum(idRow.bbox_min_lon);
    const bMaxLat = toNum(idRow.bbox_max_lat);
    const bMaxLon = toNum(idRow.bbox_max_lon);
    const identity: GridCellIdentity = {
        gridCellId,
        referenceLabel,
        gridSizeKm: toNum(idRow.grid_size_km),
        gridSystem: (idRow.grid_system as string) ?? null,
        lat: toNum(idRow.lat),
        lon: toNum(idRow.lon),
        bbox:
            bMinLat != null && bMinLon != null && bMaxLat != null && bMaxLon != null
                ? { minLat: bMinLat, minLon: bMinLon, maxLat: bMaxLat, maxLon: bMaxLon }
                : null,
    };

    // ---- habitat ----
    let habitat: GridHabitatBlock | null = null;
    const habRow = habitatR.rows[0];
    const ndviSeries: GridHabitatSeriesPoint[] = ndviR.rows.map((row) => {
        const bucketStart = String(row.m ?? "");
        return { bucketStart, label: monthLabel(bucketStart), ndvi: toNum(row.ndvi) };
    });
    if (habRow || ndviSeries.length > 0) {
        const rawBands = DW_BANDS.map((band) => ({
            key: band.key,
            label: band.label,
            value: Math.max(0, toNum(habRow?.[band.column]) ?? 0),
        }));
        const bandTotal = rawBands.reduce((sum, band) => sum + band.value, 0);
        const composition =
            bandTotal > 0
                ? rawBands.map((band) => ({
                      key: band.key,
                      label: band.label,
                      pct: Math.round((band.value / bandTotal) * 1000) / 10,
                  }))
                : [];
        const builtProb = toNum(habRow?.dw_built_prob);
        habitat = {
            latestDate: (habRow?.d as string) ?? (ndviSeries.at(-1)?.bucketStart ?? null),
            ndvi: ndviSeries.at(-1)?.ndvi ?? toNum(habRow?.ndvi),
            forestPct: toNum(habRow?.forest_cover_pct),
            builtPct: builtProb == null ? null : builtProb * 100,
            humanFootprint: toNum(habRow?.human_modification_index),
            elevationM: toNum(habRow?.elevation_m),
            lstDayC: toNum(habRow?.lst_day_celsius),
            landCoverLabel: (habRow?.dynamic_world_label as string) ?? null,
            composition,
            ndviSeries,
        };
    }

    // ---- alerts ----
    const alertSum = alertSumR.rows[0] ?? {};
    const red = toInt(alertSum.red);
    const amber = toInt(alertSum.amber);
    const green = toInt(alertSum.green);
    const alerts: GridAlertsBlock = {
        hasData: red + amber + green > 0,
        latestPeriod: (alertSum.latest as string) ?? null,
        latestLevel: (alertLevelR.rows[0]?.lvl as GridAlertLevel) ?? null,
        red,
        amber,
        green,
        avgScore: toNum(alertSum.avg_score),
        weeksActive: toInt(alertSum.weeks_active),
        speciesAffected: toInt(alertSum.species_affected),
        peakPeriod: null,
        topSpecies: alertSpeciesR.rows.map((row) => ({
            speciesEnglish: String(row.species_english ?? "—"),
            amber: toInt(row.amber),
            red: toInt(row.red),
            peakScore: toNum(row.peak),
        })),
        series: alertSeriesR.rows.map((row) => {
            const bucketStart = String(row.m ?? "");
            return {
                bucketStart,
                label: monthLabel(bucketStart),
                amber: toInt(row.amber),
                red: toInt(row.red),
            };
        }),
        nearest: null,
    };

    // Peak month = the series bucket with the most amber+red activity.
    const peakPoint = alerts.series.reduce<GridAlertSeriesPoint | null>((best, point) => {
        const total = point.amber + point.red;
        if (total <= 0) return best;
        if (!best || total > best.amber + best.red) return point;
        return best;
    }, null);
    alerts.peakPeriod = peakPoint ? peakPoint.label : null;

    // ---- species / activity ----
    const speciesSum = speciesSumR.rows[0] ?? {};
    const totalDetections = toInt(speciesSum.det);
    const species: GridSpeciesBlock = {
        hasData: totalDetections > 0,
        totalDetections,
        stationCount: toInt(stationsR.rows[0]?.stations),
        firstDetection: (speciesSum.first_d as string) ?? null,
        lastDetection: (speciesSum.last_d as string) ?? null,
        top: speciesTopR.rows.map((row) => ({
            speciesId: String(row.species_id ?? ""),
            commonName: (row.common_name as string) ?? null,
            thumbnailUrl: (row.thumb as string) ?? null,
            detections: toInt(row.det),
        })),
        series: speciesSeriesR.rows.map((row) => {
            const bucketStart = String(row.m ?? "");
            return { bucketStart, label: monthLabel(bucketStart), detections: toInt(row.det) };
        }),
    };

    // When a cell carries no surveillance of its own, surface the nearest cell
    // that does, as spatial context (only ~190–250 of 1,834 cells are monitored).
    if (identity.lat != null && identity.lon != null && !alerts.hasData) {
        alerts.nearest = await loadNearest(
            gridCellId,
            "species_week_anomaly_tbl",
            "t.alert_level IN ('amber','red')",
            "count(*) FILTER (WHERE t.alert_level IN ('amber','red'))",
            "amber/red",
        );
    }

    return { identity, habitat, alerts, species };
}

const loadOverviewCached = unstable_cache(loadOverview, ["grid-cell-overview-v4"], {
    revalidate: 900,
});

const handleGet = async (request: NextRequest) => {
    try {
        const gridCellId = request.nextUrl.searchParams.get("grid_cell_id")?.trim() ?? "";
        if (!gridCellId) {
            return NextResponse.json({ error: "grid_cell_id is required" }, { status: 400 });
        }
        const payload = await loadOverviewCached(gridCellId);
        return NextResponse.json(payload, {
            headers: { "Cache-Control": "public, s-maxage=900, stale-while-revalidate=600" },
        });
    } catch (error) {
        console.error("Grid cell overview API error:", error);
        return NextResponse.json({ error: "Failed to load grid cell overview" }, { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
