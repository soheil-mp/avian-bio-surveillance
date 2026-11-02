import { NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { withMeasuredApiRoute } from '@/lib/performance/server';
import { unstable_cache } from 'next/cache';

type NetworkHealthStatus = 'active' | 'stale' | 'dormant' | 'unknown';
type NetworkHealthRow = {
    station_id: string;
    station_name: string;
    station_type: string;
    total_detections: number | string | null;
    unique_species: number | string | null;
    last_detection: string | null;
    lifetime_availability_pct: number | string | null;
    availability_90d_pct: number | string | null;
    station_status: NetworkHealthStatus;
};

/**
 * GET /api/network-health
 *
 * Returns per-station availability info and a simple recency status.
 */
const loadNetworkHealth = unstable_cache(
    async () => {
        const res = await query<NetworkHealthRow>(`
            SELECT station_id,
                   station_name,
                   COALESCE(station_type, 'unknown')                                     AS station_type,
                   total_detections,
                   unique_species,
                   TO_CHAR(last_detection, 'YYYY-MM-DD')                                  AS last_detection,
                                     COALESCE(lifetime_availability_pct, 0)::float                          AS lifetime_availability_pct,
                                     COALESCE(availability_90d_pct, 0)::float                               AS availability_90d_pct,
                   CASE
                       WHEN days_since_last IS NULL THEN 'unknown'
                       WHEN days_since_last <= 3 THEN 'active'
                       WHEN days_since_last <= 14 THEN 'stale'
                       ELSE 'dormant'
                   END                                                                     AS station_status
            FROM   birdweather_stations_tbl
            ORDER  BY last_detection DESC NULLS LAST
        `);

        return res.rows.map((row) => ({
            id: row.station_id,
            name: row.station_name,
            type: row.station_type,
            detections: Number(row.total_detections) || 0,
            species: Number(row.unique_species) || 0,
            quality: (Number(row.lifetime_availability_pct) || 0) / 100,
            availability90dPct: Number(row.availability_90d_pct) || 0,
            lifetimeAvailabilityPct: Number(row.lifetime_availability_pct) || 0,
            last_detection: row.last_detection,
            status: row.station_status,
        }));
    },
    ['network-health'],
    { revalidate: 900 },
);

const handleGet = async (_request: Request) => {
    try {
        const stations = await loadNetworkHealth();

        return NextResponse.json(stations, {
            headers: { 'Cache-Control': 'public, s-maxage=900, stale-while-revalidate=600' },
        });
    } catch (e) {
        console.error('network-health: DB error', e);
        return NextResponse.json([], { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
