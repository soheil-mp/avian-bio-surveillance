import Link from 'next/link';
import { query } from '@/lib/db';
import { measurePageRender } from '@/lib/performance/server';
import { ChevronLeft, ShieldCheck, Clock } from 'lucide-react';
import QualityDashboard, { type QualityDashboardData } from './QualityDashboard';

export const dynamic = 'force-dynamic';

/* ── constants ────────────────────────────────────────────── */

const TABLE_SHORT: Record<string, string> = {
    birdweather_tbl: 'BirdWeather',
    knmi_tbl: 'KNMI',
    sovon_tbl: 'Sovon',
    ebird_tbl: 'eBird',
    movebank_tbl: 'Movebank',
    google_earth_engine_grid_tbl: 'Earth Engine',
    seismic_tbl: 'Seismic',
};

const DIM_INFO: Record<string, { name: string; description: string }> = {
    completeness: {
        name: 'Completeness',
        description: 'Non-null rates and temporal coverage, with readiness-style coverage indicators reported separately.',
    },
    timeliness: {
        name: 'Timeliness',
        description: 'Data freshness relative to expected ingestion cadence for each source.',
    },
    accuracy: {
        name: 'Accuracy',
        description: 'Confidence distributions, coordinate validation, z-score outliers, and quality flags.',
    },
    consistency: {
        name: 'Consistency',
        description: 'Cross-field validation, referential integrity, and cross-source alignment reported mainly as diagnostics.',
    },
    uniqueness: {
        name: 'Uniqueness',
        description: 'Primary-key uniqueness and near-duplicate detection across all tables.',
    },
    validity: {
        name: 'Validity',
        description: 'Domain-range conformance, schema drift, and value format checks.',
    },
};

const DIM_ORDER = ['completeness', 'timeliness', 'accuracy', 'consistency', 'uniqueness', 'validity'] as const;

/* ── types for DB rows ────────────────────────────────────── */

type RunRow = {
    run_id: number;
    started_at: string;
    finished_at: string;
    composite_score: number;
    metric_count: number;
    station_count: number;
    fail_count: number;
    warn_count: number;
};

type MetricRow = {
    table_name: string;
    dimension: string;
    metric: string;
    value: number;
    threshold: number | null;
    status: string;
    detail: string | null;
    score: number;
};

type StationStatRow = {
    source: string;
    count: string;
    avg_completeness: string;
    avg_confidence: string | null;
    avg_freshness_days: string;
    avg_species: string;
};

/* ── data layer (data_quality_tbl + birdweather_stations_tbl) ── */

async function getLatestRun(): Promise<RunRow | null> {
    try {
        const r = await query(`
            SELECT run_id,
                   (detail->>'started_at')::text   AS started_at,
                   (detail->>'finished_at')::text   AS finished_at,
                   score                            AS composite_score,
                   (detail->>'metric_count')::int   AS metric_count,
                   (detail->>'station_count')::int  AS station_count,
                   (detail->>'fail_count')::int     AS fail_count,
                   (detail->>'warn_count')::int     AS warn_count
            FROM data_quality_tbl
            WHERE kind = 'summary' AND status = 'completed'
            ORDER BY run_id DESC
            LIMIT 1
        `);
        return (r.rows[0] as RunRow) ?? null;
    } catch {
        return null;
    }
}

async function getRunMetrics(runId: number): Promise<MetricRow[]> {
    try {
        const r = await query(
            `SELECT table_name, dimension, metric, value,
                    (detail->>'threshold')::numeric AS threshold,
                    status, detail->>'detail' AS detail, score
             FROM data_quality_tbl
             WHERE kind = 'metric' AND run_id = $1
             ORDER BY dimension, table_name, metric`,
            [runId],
        );
        return r.rows as MetricRow[];
    } catch {
        return [];
    }
}

async function getStationStats(): Promise<StationStatRow[]> {
    try {
        const r = await query(
            `SELECT station_type                     AS source,
                    COUNT(*)::text                    AS count,
                    AVG(COALESCE(availability_90d_pct, lifetime_availability_pct))::text AS avg_completeness,
                    AVG(avg_confidence)::text          AS avg_confidence,
                    AVG(days_since_last)::text         AS avg_freshness_days,
                    AVG(unique_species)::text           AS avg_species
             FROM birdweather_stations_tbl
             GROUP BY station_type`,
            [],
        );
        return r.rows as StationStatRow[];
    } catch {
        return [];
    }
}

/* ── transform metrics → dashboard data ───────────────────── */

function buildDashboardData(
    run: RunRow,
    metrics: MetricRow[],
    stationRows: StationStatRow[],
): QualityDashboardData {
    /* Dimension scores */
    const dimensions = DIM_ORDER.map((dim) => {
        const scored = metrics.filter((m) => m.dimension === dim && m.status !== 'INFO');
        const avg = scored.length
            ? Math.round(scored.reduce((s, m) => s + Number(m.score ?? 0), 0) / scored.length)
            : 0;
        return {
            key: dim,
            name: DIM_INFO[dim]?.name ?? dim,
            score: avg,
            description: DIM_INFO[dim]?.description ?? '',
        };
    });

    /* Heatmap: table × dimension */
    const heatmap: QualityDashboardData['heatmap'] = [];
    const tables = [...new Set(metrics.map((m) => m.table_name))];
    for (const table of tables) {
        for (const dim of DIM_ORDER) {
            const cell = metrics.filter(
                (m) => m.table_name === table && m.dimension === dim && m.status !== 'INFO',
            );
            if (cell.length === 0) continue;
            heatmap.push({
                table,
                shortName: TABLE_SHORT[table] ?? table,
                dimension: dim,
                score: Math.round(cell.reduce((s, m) => s + Number(m.score ?? 0), 0) / cell.length),
                metricCount: cell.length,
            });
        }
    }

    /* Issues (FAIL first, then WARN) */
    const issues = metrics
        .filter((m) => m.status === 'FAIL' || m.status === 'WARN')
        .sort((a, b) => (a.status === 'FAIL' ? 0 : 1) - (b.status === 'FAIL' ? 0 : 1))
        .map((m) => ({
            table: m.table_name,
            shortName: TABLE_SHORT[m.table_name] ?? m.table_name,
            dimension: m.dimension,
            dimName: DIM_INFO[m.dimension]?.name ?? m.dimension,
            metric: m.metric,
            status: m.status,
            value: Number(m.value),
            detail: m.detail,
            score: Number(m.score ?? 0),
        }));

    /* Station stats */
    const stationStats = stationRows.map((r) => ({
        source: String(r.source),
        count: Number(r.count),
        avgCompleteness: Math.round(Number(r.avg_completeness ?? 0)),
        avgConfidence:
            r.avg_confidence != null ? Number(Number(r.avg_confidence).toFixed(2)) : null,
        avgFreshnessDays: Math.round(Number(r.avg_freshness_days ?? 0)),
        avgSpecies: Math.round(Number(r.avg_species ?? 0)),
    }));

    /* Source cards — per-table overview with row counts, freshness, per-dimension scores */
    const sourceCards = tables.map((table) => {
        const shortName = TABLE_SHORT[table] ?? table;
        const heatCells = heatmap.filter((h) => h.table === table);
        const avgScore = heatCells.length
            ? Math.round(heatCells.reduce((s, c) => s + c.score, 0) / heatCells.length)
            : 0;
        const totalRowsMetric = metrics.find(
            (m) => m.table_name === table && m.metric === 'total_rows',
        );
        const freshnessMetric = metrics.find(
            (m) => m.table_name === table && m.metric === 'data_freshness_hours',
        );
        return {
            table,
            shortName,
            avgScore,
            totalRows: totalRowsMetric ? Number(totalRowsMetric.value) : null,
            freshnessHours: freshnessMetric ? Number(freshnessMetric.value) : null,
            issueCount: issues.filter((i) => i.table === table).length,
            dimensions: DIM_ORDER.map((dim) => ({
                key: dim,
                score: heatmap.find((h) => h.table === table && h.dimension === dim)?.score ?? 0,
            })),
        };
    });

    /* Metric breakdown — pass / warn / fail / info counts */
    const metricBreakdown = {
        pass: metrics.filter((m) => m.status === 'PASS').length,
        warn: metrics.filter((m) => m.status === 'WARN').length,
        fail: metrics.filter((m) => m.status === 'FAIL').length,
        info: metrics.filter((m) => m.status === 'INFO').length,
    };

    return {
        compositeScore: Math.round(Number(run.composite_score)),
        dimensions,
        heatmap,
        issues,
        stationStats,
        sourceCards,
        metricBreakdown,
    };
}

/* ── helper: human-readable time-ago ──────────────────────── */

function timeAgo(iso: string): string {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60_000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
}

/* ── page ──────────────────────────────────────────────────── */

export default async function QualityPage() {
    return measurePageRender(async () => {
        const run = await getLatestRun();

        let data: QualityDashboardData | null = null;
        let error: string | null = null;

        if (run) {
            try {
                const [metrics, stationRows] = await Promise.all([
                    getRunMetrics(run.run_id),
                    getStationStats(),
                ]);
                data = buildDashboardData(run, metrics, stationRows);
            } catch {
                error = 'Failed to load quality metrics from the latest audit run.';
            }
        }

        return (
            <div className="h-full w-full overflow-y-auto">
                {/* ── breadcrumb + title ──────────────────── */}
                <div className="px-10 md:px-16 lg:px-24 xl:px-32 pt-12 pb-2">
                    <Link
                        href="/monitor"
                        className="inline-flex items-center gap-1 text-slate-500 hover:text-slate-300 transition-colors mb-4"
                    >
                        <ChevronLeft size={14} />
                        <span className="text-[10px] font-medium uppercase tracking-widest">Monitor</span>
                    </Link>
                    <h1 className="text-[24px] font-bold tracking-tight text-white flex items-center gap-3">
                        <ShieldCheck size={22} className="text-neutral-400" />
                        Data Quality
                    </h1>
                    <p className="text-[12px] mt-2 text-neutral-400 max-w-2xl leading-relaxed">
                        Measuring data quality across{' '}
                        <span className="text-neutral-300 font-medium">6 ISO 25012 dimensions</span>
                        : Completeness, Timeliness, Accuracy, Consistency, Uniqueness, and Validity.
                    </p>

                    {/* ── audit metadata bar ──────────────────── */}
                    {run && (
                        <div className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-2 text-[10px] text-text-muted/70">
                            <span className="inline-flex items-center gap-1.5">
                                <Clock size={11} className="text-slate-500" />
                                {timeAgo(run.finished_at)}
                            </span>
                            <span className="text-slate-700">|</span>
                            <span>Run #{run.run_id}</span>
                            <span className="text-slate-700">|</span>
                            <span>{run.metric_count} metrics</span>
                            <span className="text-slate-700">|</span>
                            <span>{run.station_count} stations</span>
                            {run.fail_count > 0 && (
                                <>
                                    <span className="text-slate-700">|</span>
                                    <span className="text-red-400/80 font-semibold">{run.fail_count} fail</span>
                                </>
                            )}
                            {run.warn_count > 0 && (
                                <>
                                    <span className="text-slate-700">|</span>
                                    <span className="text-amber-400/80 font-semibold">{run.warn_count} warn</span>
                                </>
                            )}
                        </div>
                    )}
                </div>

                {error ? (
                    <div className="mx-10 md:mx-16 lg:mx-24 xl:mx-32 mt-4 px-5 py-4 rounded border border-red-500/20 bg-red-500/5">
                        <span className="text-[11px] font-mono text-red-300/80">{error}</span>
                    </div>
                ) : !run ? (
                    <div className="mx-10 md:mx-16 lg:mx-24 xl:mx-32 mt-4 px-5 py-4 rounded-lg border border-white/[0.06]
                         bg-white/[0.02]">
                        <p className="text-[12px] text-text-secondary mb-1">No audit data available</p>
                        <p className="text-[11px] text-text-muted">
                            The quality audit runs automatically via the daily Dagster pipeline, or trigger it
                            manually from the{' '}
                            <Link href="/monitor/pipelines" className="underline text-brand-400/60 hover:text-brand-400">
                                Pipelines
                            </Link>{' '}
                            page.
                        </p>
                    </div>
                ) : data ? (
                    <div className="px-10 md:px-16 lg:px-24 xl:px-32 pt-6 pb-12">
                        <QualityDashboard data={data} />
                    </div>
                ) : null}
            </div>
        );
    }, { routePath: "/monitor/quality" });
}
