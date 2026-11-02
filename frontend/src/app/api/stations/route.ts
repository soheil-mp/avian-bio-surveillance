import { NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { withMeasuredApiRoute } from '@/lib/performance/server';
import { unstable_cache } from 'next/cache';

type StationTopSpeciesRow = {
    name?: string | null;
    count?: number | string | null;
    thumbnail_url?: string | null;
};
type StationQueryRow = {
    station_id: string;
    station_name: string;
    station_type: string;
    station_country: string;
    station_state: string;
    lat: number | string;
    lon: number | string;
    total_detections: number | string | null;
    unique_species: number | string | null;
    unique_days: number | string | null;
    first_detection: string | null;
    last_detection: string | null;
    avg_confidence: number | string | null;
    high_confidence_detections: number | string | null;
    monthly_detections: Record<string, number> | null;
    top_species: unknown;
    lifetime_availability_pct: number | string | null;
    availability_90d_pct: number | string | null;
    days_since_last: number | string | null;
    station_status: string | null;
    detections_per_day: number | string | null;
    coverage_span_days: number | string | null;
    ndvi_mean: number | string | null;
    elevation_mean: number | string | null;
    esa_worldcover_label: string | null;
};

function isStationTopSpeciesRow(value: unknown): value is StationTopSpeciesRow {
    return !!value && typeof value === 'object';
}

/**
 * GET /api/stations
 *
 * Returns all BirdWeather stations in the NL bbox with pre-aggregated stats
 * from birdweather_stations_tbl. The per-cell GEE enrichment (grid_cell_id,
 * 90-day NDVI mean, elevation mean, latest ESA land cover) is materialised onto
 * the same table by refresh_station_gee_enrichment() (db/routines/060), so this
 * is now a single-table read rather than a live fn_point_to_cell pass + two GEE
 * aggregations joined per request. Result is cached for 15 minutes
 * (revalidate: 900) and single-flighted below.
 *
 * Shape matches the Station interface in SatelliteMap.tsx.
 */

const loadStations = unstable_cache(
    async () => {
    const res = await query<StationQueryRow>(`
                                SELECT rs.station_id,
                                             rs.station_name,
                                             COALESCE(rs.station_type, 'unknown')                                  AS station_type,
                                             COALESCE(rs.station_country, '')                                       AS station_country,
                                             COALESCE(rs.station_state,   '')                                       AS station_state,
                                             rs.lat,
                                             rs.lon,
                                             rs.total_detections,
                                             rs.unique_species,
                                             rs.unique_days,
                                             TO_CHAR(rs.first_detection, 'YYYY-MM-DD')                              AS first_detection,
                                             TO_CHAR(rs.last_detection,  'YYYY-MM-DD')                              AS last_detection,
                                             rs.avg_confidence::float                                                AS avg_confidence,
                                             rs.high_confidence_detections,
                                             COALESCE(rs.monthly_detections, '{}'::jsonb)                           AS monthly_detections,
                                             COALESCE(rs.top_species,        '[]'::jsonb)                           AS top_species,
                                             COALESCE(rs.lifetime_availability_pct, 0)::float                       AS lifetime_availability_pct,
                                             COALESCE(rs.availability_90d_pct, 0)::float                           AS availability_90d_pct,
                                             rs.days_since_last,
                                             CASE
                                                 WHEN rs.days_since_last IS NULL THEN 'unknown'
                                                 WHEN rs.days_since_last <= 3 THEN 'active'
                                                 WHEN rs.days_since_last <= 14 THEN 'stale'
                                                 ELSE 'dormant'
                                             END                                                                     AS station_status,
                                             ROUND((rs.total_detections::numeric
                                                            / GREATEST(rs.unique_days, 1)) * 10) / 10.0                     AS detections_per_day,
                                             GREATEST(
                                                     1,
                                                     (rs.last_detection - rs.first_detection)::int + 1
                                             )                                                                       AS coverage_span_days,
                                             rs.gee_ndvi_90d_mean    AS ndvi_mean,
                                             rs.gee_elevation_m_mean AS elevation_mean,
                                             rs.gee_land_cover_label AS esa_worldcover_label
                                FROM birdweather_stations_tbl rs
                                WHERE rs.lat IS NOT NULL AND rs.lon IS NOT NULL
                                    AND rs.lat BETWEEN 50.75 AND 53.70
                                    AND rs.lon BETWEEN 3.33  AND 7.22
                                ORDER BY rs.total_detections DESC
        `);

        return res.rows.map((row) => ({
            id: row.station_id,
            name: row.station_name,
            type: row.station_type,
            country: row.station_country,
            state: row.station_state,
            coords: { lat: Number(row.lat), lon: Number(row.lon) },
            stats: {
                total_detections: Number(row.total_detections) || 0,
                unique_species: Number(row.unique_species) || 0,
                unique_days: Number(row.unique_days) || 0,
                first_detection: row.first_detection,
                last_detection: row.last_detection,
                avg_confidence: row.avg_confidence != null ? Number(row.avg_confidence) : null,
                monthly_detections: row.monthly_detections,
                top_species: (Array.isArray(row.top_species) ? row.top_species : [])
                    .filter(isStationTopSpeciesRow)
                    .map((speciesRow) => ({
                    name: speciesRow.name ?? '',
                    count: speciesRow.count != null ? Number(speciesRow.count) : 0,
                    thumbnail_url:
                        typeof speciesRow.thumbnail_url === 'string'
                            ? speciesRow.thumbnail_url
                            : null,
                })),
                high_confidence_detections:
                    row.high_confidence_detections != null
                        ? Number(row.high_confidence_detections)
                        : 0,
                detections_per_day: Number(row.detections_per_day) || 0,
                coverage_span_days: Number(row.coverage_span_days) || 0,
                lifetime_availability_pct:
                    row.lifetime_availability_pct != null
                        ? Number(row.lifetime_availability_pct)
                        : 0,
                availability_90d_pct:
                    row.availability_90d_pct != null ? Number(row.availability_90d_pct) : 0,
                days_since_last:
                    row.days_since_last != null ? Number(row.days_since_last) : null,
                station_status: row.station_status ?? 'unknown',
                gee_ndvi: row.ndvi_mean != null ? Number(row.ndvi_mean) : null,
                gee_elevation_m: row.elevation_mean != null ? Number(row.elevation_mean) : null,
                gee_land_cover: row.esa_worldcover_label ?? null,
            },
            in_database: true,
        }));
    },
    ['stations-dashboard-v1'],
    { revalidate: 900 },
);

// Single-flight guard: collapse concurrent cache-cold / revalidation calls into
// one in-process execution. unstable_cache persists the result for 15 min but
// does NOT dedupe simultaneous misses, so without this a map mount + dev
// double-render + multiple tabs + the background revalidate can each run the
// full station query at once, contending on the pool and ballooning each other
// from ~0.5s to tens of seconds (and starving every other route).
let stationsInFlight: ReturnType<typeof loadStations> | null = null;
function loadStationsDeduped(): ReturnType<typeof loadStations> {
    if (!stationsInFlight) {
        stationsInFlight = loadStations().finally(() => {
            stationsInFlight = null;
        });
    }
    return stationsInFlight;
}

const handleGet = async (_request: Request) => {
    try {
        const stations = await loadStationsDeduped();

        return NextResponse.json(stations, {
            headers: {
                'Cache-Control': 'public, s-maxage=900, stale-while-revalidate=600',
            },
        });
    } catch (e) {
        console.error('Failed to fetch stations from DB:', e);
        return NextResponse.json([], { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
