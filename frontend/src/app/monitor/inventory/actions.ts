'use server';

import { query } from '@/lib/db';
import { measureServerAction } from '@/lib/performance/server';
import { TABLE_DEFS } from './tables';

/* ── types ────────────────────────────────────────────────── */

export type ColumnProfile = {
    name: string;
    dataType: string;
    nullable: boolean;
    nullPct: number;
    distinctCount: number;
    minVal: string | null;
    maxVal: string | null;
};

export type Filter = { column: string; value: string };
export type SortDir = 'asc' | 'desc';

/* ── helpers ──────────────────────────────────────────────── */

function serializeRows(rows: Record<string, unknown>[]): Record<string, unknown>[] {
    return rows.map((row) => {
        const obj: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(row)) {
            if (v instanceof Date) obj[k] = v.toISOString();
            else if (typeof v === 'bigint') obj[k] = Number(v);
            else obj[k] = v;
        }
        return obj;
    });
}

/** Get the validated column names for a table — used as a whitelist */
async function getValidColumns(tableKey: string): Promise<string[]> {
    const res = await query<{ column_name: string }>(
        `SELECT column_name FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = $1
         ORDER BY ordinal_position`,
        [tableKey],
    );
    return res.rows.map((row) => row.column_name);
}

/* ── column profiling ─────────────────────────────────────── */

export async function fetchColumnProfile(tableKey: string): Promise<ColumnProfile[]> {
    return measureServerAction('monitor.inventory.fetchColumnProfile', async () => {
        const def = TABLE_DEFS.find((t) => t.key === tableKey);
        if (!def) throw new Error('Invalid table');

        /*
         * Use pg_stats + pg_attribute (catalog) instead of live aggregation.
         * This is instant — reads pre-computed statistics from ANALYZE.
         * Avoids the expensive COUNT(DISTINCT col) full-table scan.
         */
        const res = await query(
            `SELECT
                a.attname                                AS name,
                format_type(a.atttypid, a.atttypmod)     AS data_type,
                NOT a.attnotnull                         AS nullable,
                COALESCE(ROUND(s.null_frac * 100), 0)::int AS null_pct,
                CASE
                    WHEN s.n_distinct >= 0 THEN s.n_distinct::bigint
                    ELSE ROUND(ABS(s.n_distinct) * c.reltuples)::bigint
                END AS distinct_count,
                s.most_common_vals::text  AS mcv,
                s.histogram_bounds::text  AS histogram
            FROM pg_attribute a
            JOIN pg_class c     ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stats s
                ON s.schemaname = n.nspname
               AND s.tablename  = c.relname
               AND s.attname    = a.attname
            WHERE c.relname = $1
              AND n.nspname = 'public'
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum`,
            [def.key],
        );

        type Row = {
            name: string; data_type: string; nullable: boolean;
            null_pct: number; distinct_count: string | null;
            mcv: string | null; histogram: string | null;
        };

        return (res.rows as Row[]).map((r): ColumnProfile => {
            /* Extract approximate min/max from histogram bounds if available */
            let minVal: string | null = null;
            let maxVal: string | null = null;
            if (r.histogram) {
                const inner = r.histogram.slice(1, -1); /* strip surrounding {} */
                const parts = inner.split(',');
                if (parts.length >= 2) {
                    minVal = parts[0].trim();
                    maxVal = parts[parts.length - 1].trim();
                }
            }
            return {
                name: r.name,
                dataType: r.data_type,
                nullable: r.nullable,
                nullPct: r.null_pct,
                distinctCount: Number(r.distinct_count ?? 0),
                minVal,
                maxVal,
            };
        });
    });
}

/* ── lazy-loaded table detail (called on expand) ──────────── */

export type TableDetail = {
    earliest: string | null;
    latest: string | null;
    lastIngestion: string | null;
    initialRows: Record<string, unknown>[];
};

export async function fetchTableDetail(
    tableKey: string,
    pageSize: number = 25,
): Promise<TableDetail> {
    return measureServerAction('monitor.inventory.fetchTableDetail', async () => {
        const def = TABLE_DEFS.find((t) => t.key === tableKey);
        if (!def) throw new Error('Invalid table');

        const safe = async (p: Promise<{ rows: { v: unknown }[] }>): Promise<string | null> => {
            try { const r = await p; return r.rows[0]?.v ? String(r.rows[0].v) : null; } catch { return null; }
        };
        const lastIngestionPromise = def.key === 'grid_cells_tbl'
            ? Promise.resolve(null)
            : safe(query(`SELECT ${def.orderCol}::text AS v FROM ${def.key} ORDER BY ${def.orderCol} DESC LIMIT 1`));

        const [earliest, latest, lastIngestion, dataRes] = await Promise.all([
            safe(query(`SELECT ${def.orderCol}::text AS v FROM ${def.key} ORDER BY ${def.orderCol} ASC LIMIT 1`)),
            safe(query(`SELECT ${def.orderCol}::text AS v FROM ${def.key} ORDER BY ${def.orderCol} DESC LIMIT 1`)),
            lastIngestionPromise,
            query(`SELECT * FROM ${def.key} ORDER BY ${def.orderCol} DESC LIMIT ${Number(pageSize)}`).catch(() => ({ rows: [] as Record<string, unknown>[] })),
        ]);

        return {
            earliest,
            latest,
            lastIngestion,
            initialRows: serializeRows(dataRes.rows as Record<string, unknown>[]),
        };
    });
}

/* ── filtered + sorted page fetch ─────────────────────────── */

export async function fetchPage(
    tableKey: string,
    page: number,
    pageSize: number = 25,
    filters: Filter[] = [],
    sortCol?: string,
    sortDir: SortDir = 'desc',
): Promise<{ columns: string[]; rows: Record<string, unknown>[]; total: number }> {
    return measureServerAction('monitor.inventory.fetchPage', async () => {
        const def = TABLE_DEFS.find((t) => t.key === tableKey);
        if (!def) throw new Error('Invalid table');

        /* Validate column names against the actual schema (whitelist) */
        const validCols = await getValidColumns(def.key);

        /* Build WHERE clause from filters — only allow validated column names */
        const whereParts: string[] = [];
        const params: unknown[] = [];
        let paramIdx = 1;

        for (const f of filters) {
            if (!f.value.trim() || !validCols.includes(f.column)) continue;
            const quotedCol = `"${f.column.replace(/"/g, '""')}"`;
            whereParts.push(`${quotedCol}::text ILIKE $${paramIdx}`);
            params.push(`%${f.value}%`);
            paramIdx++;
        }

        const whereClause = whereParts.length > 0 ? `WHERE ${whereParts.join(' AND ')}` : '';

        /* Validate sort column */
        const safeSortCol = sortCol && validCols.includes(sortCol) ? sortCol : def.orderCol;
        const safeSortDir = sortDir === 'asc' ? 'ASC' : 'DESC';
        const quotedSort = `"${safeSortCol.replace(/"/g, '""')}"`;

        const offset = page * pageSize;
        params.push(pageSize, offset);

        /* def.key from hardcoded TABLE_DEFS, column names validated against information_schema */
        const [dataRes, countRes] = await Promise.all([
            query(
                `SELECT * FROM ${def.key} ${whereClause} ORDER BY ${quotedSort} ${safeSortDir} LIMIT $${paramIdx} OFFSET $${paramIdx + 1}`,
                params,
            ),
            query(
                `SELECT COUNT(*)::bigint AS v FROM ${def.key} ${whereClause}`,
                params.slice(0, -2),  /* exclude LIMIT/OFFSET params */
            ),
        ]);

        return {
            columns: validCols,
            rows: serializeRows(dataRes.rows),
            total: Number(countRes.rows[0]?.v ?? 0),
        };
    });
}
