import { NextResponse } from 'next/server';

import { query } from '@/lib/db';
import { withMeasuredApiRoute } from '@/lib/performance/server';

export const dynamic = 'force-dynamic';

type DebugRow = {
    pid?: number;
    state?: string;
    elapsed?: unknown;
    wait_event_type?: string | null;
    wait_event?: string | null;
    computed_at?: string | null;
    last_detection_date?: string | null;
    bytes?: string | number | null;
    files?: string | number | null;
};

type QueryErrorPayload = {
    error: string;
};

function disabledResponse() {
    return NextResponse.json({ error: 'Not found' }, { status: 404 });
}

const handleGet = async () => {
    if (process.env.NODE_ENV === 'production') {
        return disabledResponse();
    }

    const [activityResult, rowResult, tmpResult] = await Promise.all([
        query<DebugRow>(`
            SELECT
                pid,
                state,
                now() - query_start AS elapsed,
                wait_event_type,
                wait_event
            FROM pg_stat_activity
            WHERE pid <> pg_backend_pid()
              AND query LIKE '%refresh_species_tracker%'
            ORDER BY query_start
        `).catch((error: unknown) => ({
            error: error instanceof Error ? error.message : String(error),
        } satisfies QueryErrorPayload)),
        query<DebugRow>(`
            SELECT
                computed_at::text AS computed_at,
                last_detection_at::date::text AS last_detection_date
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'network'
              AND location_id = 'nl'
              AND period_grain = 'year'
              AND species_id = '66'
            ORDER BY period_start DESC
            LIMIT 1
        `).catch((error: unknown) => ({
            error: error instanceof Error ? error.message : String(error),
        } satisfies QueryErrorPayload)),
        query<DebugRow>(`
            SELECT
                COALESCE(SUM(size), 0)::bigint AS bytes,
                COUNT(*)::int AS files
            FROM pg_ls_tmpdir()
        `).catch((error: unknown) => ({
            error: error instanceof Error ? error.message : String(error),
        } satisfies QueryErrorPayload)),
    ]);

    const activityError = 'error' in activityResult ? activityResult.error : null;
    const rowError = 'error' in rowResult ? rowResult.error : null;
    const tmpError = 'error' in tmpResult ? tmpResult.error : null;
    const trackerRow = 'error' in rowResult ? null : (rowResult.rows[0] ?? null);
    const tmpRow = 'error' in tmpResult ? {} : (tmpResult.rows[0] ?? {});

    return NextResponse.json({
        activeRefreshes: 'error' in activityResult ? [] : activityResult.rows.map((row) => ({
            pid: row.pid ?? null,
            state: row.state ?? null,
            elapsed: row.elapsed === undefined || row.elapsed === null ? null : String(row.elapsed),
            waitEventType: row.wait_event_type ?? null,
            waitEvent: row.wait_event ?? null,
        })),
        trackerRow: trackerRow === null ? null : {
            computedAt: trackerRow.computed_at ?? null,
            lastDetectionDate: trackerRow.last_detection_date ?? null,
        },
        tempFiles: {
            bytes: Number(tmpRow.bytes ?? 0),
            files: Number(tmpRow.files ?? 0),
        },
        errors: {
            activity: activityError,
            trackerRow: rowError,
            tempFiles: tmpError,
        },
    });
};

export const GET = withMeasuredApiRoute(handleGet);