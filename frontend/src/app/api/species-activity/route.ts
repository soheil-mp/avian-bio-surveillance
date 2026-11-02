import { NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { withMeasuredApiRoute } from '@/lib/performance/server';
import { unstable_cache } from 'next/cache';

/**
 * GET /api/species-activity
 *
 * Returns detection counts per species over the last 30 days from the
 * birdweather_daily_cagg_tbl continuous aggregate. Used by the
 * BiosurveillancePanel species-radar visualization.
 */
const loadSpeciesActivity = unstable_cache(
    async () => {
        const res = await query(`
            WITH daily AS (
                SELECT bucket::date              AS day,
                       species_id,
                       SUM(detection_count)::int AS detections
                FROM   birdweather_daily_cagg_tbl
                -- Anchor to the cagg's newest bucket, not the wall clock: the
                -- detection data is historical, so NOW() - 30d matches nothing
                -- and the species radar comes back empty.
                WHERE  bucket >= (SELECT MAX(bucket) FROM birdweather_daily_cagg_tbl) - INTERVAL '30 days'
                  AND  species_id IS NOT NULL
                GROUP  BY bucket::date, species_id
            )
            SELECT d.day,
                   COALESCE(
                       NULLIF(sp.common_name, ''),
                       CONCAT('Species ', d.species_id)
                   )                                       AS species,
                   d.detections,
                   COALESCE(sp.species_thumbnail_url, '')  AS thumbnail
            FROM   daily d
            LEFT JOIN species_tbl sp ON sp.species_id = d.species_id
            ORDER  BY COALESCE(NULLIF(sp.common_name, ''), CONCAT('Species ', d.species_id)), d.day
        `);

        return res.rows;
    },
    ['species-activity'],
    { revalidate: 900 },
);

const handleGet = async (_request: Request) => {
    try {
        const rows = await loadSpeciesActivity();

        return NextResponse.json(rows, {
            headers: { 'Cache-Control': 'public, s-maxage=900, stale-while-revalidate=600' },
        });
    } catch (e) {
        console.error('species-activity: DB error', e);
        return NextResponse.json([], { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
