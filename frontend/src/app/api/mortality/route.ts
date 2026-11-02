import { NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { withMeasuredApiRoute } from '@/lib/performance/server';
import { unstable_cache } from 'next/cache';

/**
 * GET /api/mortality
 *
 * Returns SOVON dead-bird counts per species per month for the current year
 * and a historical baseline (average of last 5 years). Used by the
 * BiosurveillancePanel mortality-pulse visualization.
 */
const loadMortality = unstable_cache(
    async () => {
        // Anchor to the most recent year present in sovon_tbl rather than the
        // wall clock. The data is historical, so new Date().getFullYear() points
        // at a year with no rows — the current-year series goes empty while the
        // baseline still fills, which reads as a spurious "everything's quiet".
        const yearRow = await query<{ year: number }>(`SELECT MAX(year)::int AS year FROM sovon_tbl`);
        const currentYear = yearRow.rows[0]?.year ?? new Date().getFullYear();
        const res = await query(`
            WITH current_year AS (
                SELECT species_english AS species, month, dead_bird_count AS count
                FROM   sovon_tbl
                WHERE  year = $1
            ),
            baseline AS (
                SELECT species_english AS species, month,
                       ROUND(AVG(dead_bird_count)) AS avg_count
                FROM   sovon_tbl
                WHERE  year BETWEEN $1 - 5 AND $1 - 1
                GROUP  BY species_english, month
            ),
            combined AS (
                SELECT COALESCE(c.species, b.species) AS species,
                       COALESCE(c.month, b.month)     AS month,
                       COALESCE(c.count, 0)            AS count,
                       COALESCE(b.avg_count, 0)        AS baseline
                FROM   current_year c
                FULL OUTER JOIN baseline b
                  ON c.species = b.species AND c.month = b.month
            ),
            species_thumb AS (
                -- species_tbl is canonical (one row per species). Sovon keys
                -- on the English common name, so we still join on that.
                SELECT common_name, species_thumbnail_url
                FROM   species_tbl
                WHERE  species_thumbnail_url IS NOT NULL
            )
            SELECT cm.species, cm.month, cm.count, cm.baseline,
                   COALESCE(t.species_thumbnail_url, '') AS thumbnail
            FROM   combined cm
            LEFT JOIN species_thumb t ON t.common_name = cm.species
            ORDER  BY cm.species, cm.month
        `, [currentYear]);

        return res.rows;
    },
    ['mortality-panel'],
    { revalidate: 3600 },
);

const handleGet = async (_request: Request) => {
    try {
        const rows = await loadMortality();

        return NextResponse.json(rows, {
            headers: { 'Cache-Control': 'public, s-maxage=3600, stale-while-revalidate=1800' },
        });
    } catch (e) {
        console.error('mortality: DB error', e);
        return NextResponse.json([], { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
