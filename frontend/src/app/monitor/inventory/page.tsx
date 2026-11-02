import Link from 'next/link';
import { query } from '@/lib/db';
import { measurePageRender } from '@/lib/performance/server';
import { ChevronLeft } from 'lucide-react';
import { TABLE_DEFS } from './tables';
import InventoryRegistry from './InventoryRegistry';

export const dynamic = 'force-dynamic';

/* ── exported types (used by InventoryRegistry) ───────────── */

export type QualityMetricRow = {
    dimension: string;
    metric: string;
    value: number;
    threshold: number | null;
    status: 'PASS' | 'WARN' | 'FAIL' | 'INFO';
    score: number;
    detail: string | null;
};

export type DimensionScore = {
    dimension: string;
    score: number;
    passes: number;
    warns: number;
    fails: number;
};

export type TableInventory = {
    key: string;
    rows: number;
    columns: string[];
    sizeBytes: number;
    freshness: string | null;
    quality: {
        compositeScore: number;
        dimensions: DimensionScore[];
        metrics: QualityMetricRow[];
    } | null;
};

export type QualityRunInfo = {
    compositeScore: number;
    ranAt: string;
    totalMetrics: number;
    fails: number;
    warns: number;
    passes: number;
} | null;

/* ── helpers ──────────────────────────────────────────────── */

function fmtNum(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return n.toLocaleString();
}

function fmtBytes(b: number): string {
    if (b >= 1_073_741_824) return `${(b / 1_073_741_824).toFixed(1)} GB`;
    if (b >= 1_048_576) return `${(b / 1_048_576).toFixed(1)} MB`;
    if (b >= 1_024) return `${(b / 1_024).toFixed(1)} KB`;
    return `${b} B`;
}

/* ── data layer ───────────────────────────────────────────── */

const PAGE_SIZE = 25;

/* ── quality data ─────────────────────────────────────────── */

async function fetchQualityRun(): Promise<QualityRunInfo> {
    try {
        const res = await query(
            `SELECT run_id,
                    score                            AS composite_score,
                    (detail->>'started_at')::text    AS started_at,
                    (detail->>'metric_count')::int   AS metric_count,
                    (detail->>'fail_count')::int     AS fail_count,
                    (detail->>'warn_count')::int     AS warn_count
             FROM data_quality_tbl
             WHERE kind = 'summary' AND status = 'completed'
             ORDER BY run_id DESC LIMIT 1`,
        );
        if (res.rows.length === 0) return null;
        const r = res.rows[0];
        return {
            compositeScore: Number(r.composite_score),
            ranAt: String(r.started_at),
            totalMetrics: Number(r.metric_count),
            fails: Number(r.fail_count),
            warns: Number(r.warn_count),
            passes: 0,
        };
    } catch { return null; }
}

type RawMetricRow = {
    table_name: string;
    dimension: string;
    metric: string;
    value: number;
    threshold: number | null;
    status: string;
    score: number;
    detail: string | null;
};

async function fetchAllQualityMetrics(): Promise<Map<string, QualityMetricRow[]>> {
    try {
        const res = await query(
            `SELECT table_name, dimension, metric, value,
                    (detail->>'threshold')::numeric AS threshold,
                    status, score, detail->>'detail' AS detail
             FROM data_quality_tbl
             WHERE kind = 'metric'
               AND run_id = (SELECT MAX(run_id) FROM data_quality_tbl
                             WHERE kind = 'summary' AND status = 'completed')
             ORDER BY table_name, dimension, metric`,
        );
        const map = new Map<string, QualityMetricRow[]>();
        for (const row of res.rows as RawMetricRow[]) {
            const list = map.get(row.table_name) ?? [];
            list.push({
                dimension: row.dimension,
                metric: row.metric,
                value: Number(row.value),
                threshold: row.threshold != null ? Number(row.threshold) : null,
                status: row.status as QualityMetricRow['status'],
                score: Number(row.score),
                detail: row.detail,
            });
            map.set(row.table_name, list);
        }
        return map;
    } catch { return new Map(); }
}

function computeTableQuality(metrics: QualityMetricRow[]): {
    compositeScore: number;
    dimensions: DimensionScore[];
} {
    const dimMap = new Map<string, { scores: number[]; passes: number; warns: number; fails: number }>();
    for (const m of metrics) {
        if (m.status === 'INFO') continue;
        const d = dimMap.get(m.dimension) ?? { scores: [], passes: 0, warns: 0, fails: 0 };
        d.scores.push(m.score);
        if (m.status === 'PASS') d.passes++;
        else if (m.status === 'WARN') d.warns++;
        else if (m.status === 'FAIL') d.fails++;
        dimMap.set(m.dimension, d);
    }
    const dimOrder = ['completeness', 'accuracy', 'timeliness', 'consistency', 'uniqueness', 'validity'];
    const dimensions: DimensionScore[] = [];
    let totalScore = 0;
    let dimCount = 0;
    for (const [dimension, d] of dimMap) {
        const avg = d.scores.length > 0 ? d.scores.reduce((a, b) => a + b, 0) / d.scores.length : 0;
        dimensions.push({ dimension, score: Math.round(avg * 10) / 10, passes: d.passes, warns: d.warns, fails: d.fails });
        totalScore += avg;
        dimCount++;
    }
    dimensions.sort((a, b) => dimOrder.indexOf(a.dimension) - dimOrder.indexOf(b.dimension));
    return {
        compositeScore: dimCount > 0 ? Math.round((totalScore / dimCount) * 10) / 10 : 0,
        dimensions,
    };
}

/* ── fetch table summaries (batched — 3 queries total) ────── */

/** Order columns that are integers (not usable as freshness timestamps) */
const INT_ORDER_COLS = new Set(['year', 'metric_id', 'station_metric_id', 'grid_cell_id']);

async function fetchTableSummaries(
    qualityMap: Map<string, QualityMetricRow[]>,
): Promise<TableInventory[] | null> {
    try {
        const keys = TABLE_DEFS.map((t) => t.key);

        /* 1. Row counts + sizes — single CTE query */
        const statsQuery = query(
            `WITH ht AS (
                SELECT hypertable_name AS tn
                FROM timescaledb_information.hypertables
                WHERE hypertable_schema = 'public'
            )
            SELECT
                c.relname AS tn,
                CASE WHEN ht.tn IS NOT NULL
                     THEN GREATEST(approximate_row_count(format('public.%I', c.relname)::regclass), 0)::bigint
                     ELSE GREATEST(c.reltuples, 0)::bigint
                END AS row_count,
                CASE WHEN ht.tn IS NOT NULL
                     THEN hypertable_size(format('public.%I', c.relname)::regclass)::bigint
                     ELSE pg_total_relation_size(c.oid)::bigint
                END AS size_bytes
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN ht ON ht.tn = c.relname
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
              AND c.relname = ANY($1)`,
            [keys],
        );

        /* 2. Column names for all tables at once */
        const colsQuery = query(
            `SELECT table_name, column_name
             FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = ANY($1)
             ORDER BY table_name, ordinal_position`,
            [keys],
        );

        /* 3. Freshness — MAX of time-based order column per table */
        const freshnessSql = TABLE_DEFS.map((t) =>
            INT_ORDER_COLS.has(t.orderCol)
                ? `SELECT '${t.key}' AS tn, NULL::text AS ts`
                : `SELECT '${t.key}' AS tn, MAX(${t.orderCol})::text AS ts FROM ${t.key}`,
        ).join('\nUNION ALL\n');
        const freshnessQuery = query(freshnessSql);

        const [statsRes, colsRes, freshnessRes] = await Promise.all([
            statsQuery, colsQuery, freshnessQuery,
        ]);

        /* Build lookup maps */
        const statsMap = new Map<string, { rows: number; size: number }>();
        for (const r of statsRes.rows as { tn: string; row_count: string; size_bytes: string }[]) {
            statsMap.set(r.tn, { rows: Number(r.row_count), size: Number(r.size_bytes) });
        }

        const colsMap = new Map<string, string[]>();
        for (const r of colsRes.rows as { table_name: string; column_name: string }[]) {
            const list = colsMap.get(r.table_name) ?? [];
            list.push(r.column_name);
            colsMap.set(r.table_name, list);
        }

        const freshnessMap = new Map<string, string | null>();
        for (const r of freshnessRes.rows as { tn: string; ts: string | null }[]) {
            freshnessMap.set(r.tn, r.ts);
        }

        return TABLE_DEFS.map((t) => {
            const stats = statsMap.get(t.key) ?? { rows: 0, size: 0 };
            const metrics = qualityMap.get(t.key) ?? null;
            const quality = metrics ? { ...computeTableQuality(metrics), metrics } : null;
            return {
                key: t.key,
                rows: stats.rows,
                columns: colsMap.get(t.key) ?? [],
                sizeBytes: stats.size,
                freshness: freshnessMap.get(t.key) ?? null,
                quality,
            };
        });
    } catch { return null; }
}

/* ── page ──────────────────────────────────────────────────── */

export default async function InventoryPage() {
    return measurePageRender(async () => {
        const [qualityRun, qualityMap] = await Promise.all([fetchQualityRun(), fetchAllQualityMetrics()]);
        const tables = await fetchTableSummaries(qualityMap);

        /* Compute accurate pass count from metric data */
        const allMetrics = [...qualityMap.values()].flat();
        const passCount = allMetrics.filter((m) => m.status === 'PASS').length;
        if (qualityRun) qualityRun.passes = passCount;

        const totalRows = tables?.reduce((s, t) => s + t.rows, 0) ?? 0;
        const totalSize = tables?.reduce((s, t) => s + t.sizeBytes, 0) ?? 0;

        const scoreColor = !qualityRun
            ? 'rgba(255,255,255,0.2)'
            : qualityRun.compositeScore >= 80 ? '#34d399'
            : qualityRun.compositeScore >= 60 ? '#fbbf24'
            : '#f87171';

        return (
            <div className="h-full w-full overflow-y-auto">
                {/* ── header ─────────────────────────────── */}
                <div className="px-10 md:px-16 lg:px-24 xl:px-32 pt-12 pb-2">
                    <Link href="/monitor" className="inline-flex items-center gap-1 text-neutral-500 hover:text-neutral-300 transition-colors mb-4">
                        <ChevronLeft size={14} />
                        <span className="text-[10px] font-medium uppercase tracking-widest">Monitor</span>
                    </Link>
                    <h1 className="text-[24px] font-bold tracking-tight text-white">
                        Data Inventory
                    </h1>
                    <p className="text-[12px] mt-2 text-neutral-400 max-w-2xl leading-relaxed">
                        Registry of <span className="text-neutral-300 font-medium">{tables?.length ?? 0} data sources</span> with
                        row counts, storage, freshness, schema profiling, and per-table quality scores.
                    </p>
                </div>

                {/* ── system status bar ──────────────────── */}
                {tables && (
                    <div className="px-10 md:px-16 lg:px-24 xl:px-32 py-5">
                        <div className="flex items-center gap-6 flex-wrap px-5 py-4 rounded-lg bg-[#1a1a1a] border border-neutral-800">
                            <StatKpi label="Quality" value={qualityRun ? qualityRun.compositeScore.toFixed(0) : '—'} suffix="/100" valueColor={scoreColor} />
                            <Divider />
                            <StatKpi label="Sources" value={String(tables.length)} />
                            <Divider />
                            <StatKpi label="Records" value={fmtNum(totalRows)} />
                            <Divider />
                            <StatKpi label="Storage" value={fmtBytes(totalSize)} />
                            <Divider />
                            <StatKpi label="Checks" value={qualityRun ? `${qualityRun.passes}` : '—'} suffix={qualityRun ? ` pass · ${qualityRun.warns} warn · ${qualityRun.fails} fail` : ''} />
                        </div>
                        {qualityRun && (
                            <p className="text-[9px] mt-2.5 text-neutral-600">
                                Last quality assessment: {new Date(qualityRun.ranAt).toLocaleString('en-GB')} UTC — {qualityRun.totalMetrics} metrics evaluated
                            </p>
                        )}
                    </div>
                )}

                {/* ── registry ───────────────────────────── */}
                {!tables ? (
                    <div className="mx-10 md:mx-16 lg:mx-24 xl:mx-32 mt-4 px-5 py-4 rounded-lg border border-red-500/20 bg-red-500/5">
                        <span className="text-[12px] text-red-300/80">Unable to connect to the database.</span>
                    </div>
                ) : (
                    <div className="px-10 md:px-16 lg:px-24 xl:px-32 pb-10">
                        <InventoryRegistry tables={tables} pageSize={PAGE_SIZE} />
                    </div>
                )}
            </div>
        );
    }, { routePath: "/monitor/inventory" });
}

/* ── sub-components ────────────────────────────────────────── */

function StatKpi({ label, value, suffix, valueColor }: { label: string; value: string; suffix?: string; valueColor?: string }) {
    return (
        <div className="flex flex-col gap-0.5">
            <span className="text-[8px] font-medium text-neutral-600 uppercase tracking-widest">{label}</span>
            <div className="flex items-baseline gap-0.5">
                <span className="text-[15px] font-bold tabular-nums" style={{ color: valueColor ?? '#d4d4d4' }}>{value}</span>
                {suffix && <span className="text-[9px] text-neutral-600">{suffix}</span>}
            </div>
        </div>
    );
}

function Divider() {
    return <div className="w-px h-8 bg-neutral-800 hidden md:block" />;
}
