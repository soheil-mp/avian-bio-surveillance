import Link from 'next/link';
import { query } from '@/lib/db';
import { measurePageRender } from '@/lib/performance/server';
import { ChevronLeft } from 'lucide-react';
import PipelineCards, { type PipelineDef, type RunInfo } from './PipelineCards';
import { getFlowNames, getPipelines, type PipelineMeta } from './pipeline-config';
import {
    DAGSTER_LOCATION_NAMES,
    DAGSTER_REPOSITORY_NAME,
    dagsterGraphQL,
    deploymentToScheduleName,
    flowKeyToLocationName,
    flowKeyToJobName,
} from '@/lib/dagster';

export const dynamic = 'force-dynamic';

/* ── data layer ───────────────────────────────────────────── */

function strOrNull(value: unknown): string | null {
    return value ? String(value) : null;
}

function quoteIdentifier(value: string): string {
    return `"${value.replace(/"/g, '""')}"`;
}

async function getRowCount(table: string): Promise<number> {
    if (!table) return 0;
    try {
        const r = await query(`SELECT approximate_row_count($1::regclass)::bigint AS v`, [table]);
        return Number(r.rows[0]?.v ?? 0);
    } catch {
        try {
            const r = await query(
                `SELECT c.reltuples::bigint AS v FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = $1 AND n.nspname = 'public'`,
                [table],
            );
            return Number(r.rows[0]?.v ?? 0);
        } catch { return 0; }
    }
}

async function getLatestValue(table: string, column: string): Promise<string | null> {
    try {
        const r = await query<{ v: unknown }>(
            `SELECT ${quoteIdentifier(column)}::text AS v
             FROM ${quoteIdentifier(table)}
             ORDER BY ${quoteIdentifier(column)} DESC
             LIMIT 1`,
        );
        return strOrNull(r.rows[0]?.v);
    } catch {
        return null;
    }
}

async function getEarliestValue(table: string, column: string): Promise<string | null> {
    try {
        const firstChunk = await query<{ chunk_schema: string; chunk_name: string }>(
            `SELECT chunk_schema, chunk_name
             FROM timescaledb_information.chunks
             WHERE hypertable_schema = 'public'
               AND hypertable_name = $1
             ORDER BY range_start ASC
             LIMIT 1`,
            [table],
        );

        const chunk = firstChunk.rows[0];
        if (chunk) {
            const r = await query<{ v: unknown }>(
                `SELECT MIN(${quoteIdentifier(column)})::text AS v
                 FROM ${quoteIdentifier(chunk.chunk_schema)}.${quoteIdentifier(chunk.chunk_name)}`,
            );
            const value = strOrNull(r.rows[0]?.v);
            if (value) {
                return value;
            }
        }
    } catch {
        // Fall back to the base table query for non-hypertables or metadata gaps.
    }

    try {
        const r = await query<{ v: unknown }>(
            `SELECT ${quoteIdentifier(column)}::text AS v
             FROM ${quoteIdentifier(table)}
             ORDER BY ${quoteIdentifier(column)} ASC
             LIMIT 1`,
        );
        return strOrNull(r.rows[0]?.v);
    } catch {
        return null;
    }
}

async function getCoverage(
    table: string, coverageCol: string, span: 'daily' | 'monthly',
): Promise<{ date: string; count: number }[]> {
    if (!table || !coverageCol) return [];

    if (table === 'birdweather_tbl' && coverageCol === 'observation_date' && span === 'daily') {
        try {
            const r = await query<{ d: string; c: number }>(
                `SELECT bucket::date AS d, COALESCE(SUM(detection_count), 0)::int AS c
                 FROM birdweather_daily_cagg_tbl
                 WHERE bucket >= CURRENT_DATE - INTERVAL '30 days'
                   AND bucket < CURRENT_DATE + INTERVAL '1 day'
                 GROUP BY bucket::date
                 ORDER BY d`,
            );
            return r.rows.map((row) => ({ date: String(row.d), count: Number(row.c) }));
        } catch {
            // Fall through to the raw table query if the cagg is unavailable.
        }
    }

    try {
        const interval = span === 'monthly' ? '12 months' : '30 days';
        const bucket = span === 'monthly'
            ? `date_trunc('month', ${coverageCol})::date`
            : `${coverageCol}::date`;
        const r = await query<{ d: string; c: number }>(
            `SELECT ${bucket} AS d, COUNT(*)::int AS c
             FROM ${table}
             WHERE ${coverageCol} >= CURRENT_DATE - INTERVAL '${interval}'
               AND ${coverageCol} < CURRENT_DATE + INTERVAL '1 day'
             GROUP BY d ORDER BY d`,
        );
        return r.rows.map((row) => ({ date: String(row.d), count: Number(row.c) }));
    } catch { return []; }
}

/* ── Dagster schedule fetcher ─────────────────────────────── */

type ScheduleInfo = { frequency: string; time: string; enabled: boolean };

function parseCron(cron: string): { frequency: string; time: string } {
    const [minute, hour, dom, , dow] = cron.trim().split(/\s+/);
    const pad = (v: string) => v.padStart(2, '0');

    if (hour === '*') {
        return { frequency: 'Hourly', time: `00:${pad(minute)}` };
    }
    if (dom === '1') {
        return { frequency: 'Monthly', time: `${pad(hour)}:${pad(minute)}` };
    }
    if (dow !== '*') {
        return { frequency: 'Weekly', time: `${pad(hour)}:${pad(minute)}` };
    }
    return { frequency: 'Daily', time: `${pad(hour)}:${pad(minute)}` };
}

/**
 * Read every schedule registered with the Dagster code-location and
 * map it back to its pipeline id (the deployment name). Falls back to
 * an empty map when the webserver is unreachable.
 */
async function getDeploymentSchedules(pipelines: PipelineMeta[]): Promise<Record<string, ScheduleInfo>> {
    const result: Record<string, ScheduleInfo> = {};
    const query = /* GraphQL */ `
        query Schedules($s: RepositorySelector!) {
            schedulesOrError(repositorySelector: $s) {
                __typename
                ... on Schedules {
                    results {
                        name
                        cronSchedule
                        scheduleState { status }
                    }
                }
            }
        }
    `;
    type Resp = {
        schedulesOrError:
            | { __typename: 'Schedules'; results: { name: string; cronSchedule: string; scheduleState: { status: string } }[] }
            | { __typename: string };
    };
    try {
        for (const locationName of DAGSTER_LOCATION_NAMES) {
            const data = await dagsterGraphQL<Resp>(query, {
                s: {
                    repositoryLocationName: locationName,
                    repositoryName: DAGSTER_REPOSITORY_NAME,
                },
            });
            if (data.schedulesOrError.__typename !== 'Schedules') continue;
            const byName = new Map(
                (data.schedulesOrError as { results: { name: string; cronSchedule: string; scheduleState: { status: string } }[] })
                    .results.map((s) => [s.name, s]),
            );
            for (const p of pipelines) {
                if (flowKeyToLocationName(p.flowKey) !== locationName) continue;
                const sched = byName.get(deploymentToScheduleName(p.id));
                if (!sched?.cronSchedule) continue;
                result[p.id] = {
                    ...parseCron(sched.cronSchedule),
                    enabled: sched.scheduleState.status === 'RUNNING',
                };
            }
        }
    } catch { /* Dagster webserver down — all pipelines use fallback */ }
    return result;
}

/* ── Dagster run history ──────────────────────────────────── */

/**
 * Fetch the last ~10 runs per pipeline by querying the Dagster runs
 * registry filtered on job name. Each pipeline maps 1:1 to a job
 * (see ``flowKeyToJobName`` for the naming contract).
 */
async function getAllRunHistory(flowNames: Record<string, string>): Promise<Record<string, RunInfo[]>> {
    const empty: Record<string, RunInfo[]> = {};
    for (const id of Object.keys(flowNames)) empty[id] = [];

    const jobToPipeline = new Map(
        Object.entries(flowNames).map(([pid, flowKey]) => [flowKeyToJobName(flowKey), pid]),
    );
    const jobNames = Array.from(jobToPipeline.keys());
    if (jobNames.length === 0) return empty;

    const query = /* GraphQL */ `
        query Runs($filter: RunsFilter!, $limit: Int!) {
            runsOrError(filter: $filter, limit: $limit) {
                __typename
                ... on Runs {
                    results {
                        runId
                        status
                        pipelineName
                        startTime
                        endTime
                    }
                }
            }
        }
    `;
    type DagsterRun = {
        runId: string; status: string; pipelineName: string;
        startTime: number | null; endTime: number | null;
    };
    type Resp = {
        runsOrError:
            | { __typename: 'Runs'; results: DagsterRun[] }
            | { __typename: string };
    };

    try {
        const data = await dagsterGraphQL<Resp>(query, {
            filter: { pipelineName: undefined, statuses: null, jobName: null },
            limit: 50,
        });
        if (data.runsOrError.__typename !== 'Runs') return empty;
        const runs = (data.runsOrError as { results: DagsterRun[] }).results;
        const result = { ...empty };
        for (const r of runs) {
            const pid = jobToPipeline.get(r.pipelineName);
            if (!pid || result[pid].length >= 10) continue;
            const startIso = r.startTime ? new Date(r.startTime * 1000).toISOString() : null;
            const endIso = r.endTime ? new Date(r.endTime * 1000).toISOString() : null;
            const duration = r.startTime && r.endTime ? r.endTime - r.startTime : null;
            result[pid].push({
                id: r.runId,
                state: r.status,
                startTime: startIso,
                endTime: endIso,
                duration,
            });
        }
        return result;
    } catch {
        return empty;
    }
}

/* ── data fetcher ─────────────────────────────────────────── */

type PipelineMetrics = {
    rows: number;
    earliest: string | null;
    latest: string | null;
    lastIngestion: string | null;
    coverage: { date: string; count: number }[];
};

function pipelineMetricsKey(pipeline: PipelineMeta): string {
    return [
        pipeline.table,
        pipeline.orderCol,
        pipeline.coverageCol,
        pipeline.coverageSpan,
        pipeline.ingestionCol,
    ].join('|');
}

async function getPipelineSnapshot(pipeline: PipelineMeta): Promise<Omit<PipelineMetrics, 'coverage'>> {
    if (!pipeline.table) {
        return { rows: 0, earliest: null, latest: null, lastIngestion: null };
    }

    const hasMetricsConfig = Boolean(
        pipeline.orderCol && pipeline.ingestionCol && pipeline.coverageCol,
    );
    if (!hasMetricsConfig) {
        return { rows: await getRowCount(pipeline.table), earliest: null, latest: null, lastIngestion: null };
    }

    const latestPromise = getLatestValue(pipeline.table, pipeline.orderCol);
    const lastIngestionPromise = pipeline.ingestionCol === pipeline.orderCol
        ? latestPromise
        : getLatestValue(pipeline.table, pipeline.ingestionCol);

    const [rows, earliest, latest, lastIngestion] = await Promise.all([
        getRowCount(pipeline.table),
        getEarliestValue(pipeline.table, pipeline.orderCol),
        latestPromise,
        lastIngestionPromise,
    ]);

    return { rows, earliest, latest, lastIngestion };
}

async function getPipelineMetrics(pipeline: PipelineMeta): Promise<PipelineMetrics> {
    const [snapshot, coverage] = await Promise.all([
        getPipelineSnapshot(pipeline),
        getCoverage(pipeline.table, pipeline.coverageCol, pipeline.coverageSpan),
    ]);

    return { ...snapshot, coverage };
}

async function fetchPipelineData(): Promise<PipelineDef[]> {
    try {
        const pipelines = await getPipelines();
        const flowNames = await getFlowNames();
        const metricsByKey = new Map<string, Promise<PipelineMetrics>>();

        const getSharedMetrics = (pipeline: PipelineMeta): Promise<PipelineMetrics> => {
            const key = pipelineMetricsKey(pipeline);
            const existing = metricsByKey.get(key);
            if (existing) {
                return existing;
            }

            const metrics = getPipelineMetrics(pipeline);
            metricsByKey.set(key, metrics);
            return metrics;
        };

        const [pipelineRows, runHistoryMap, liveSchedules] = await Promise.all([
            Promise.all(
                pipelines.map(async (p) => {
                    const metrics = await getSharedMetrics(p);
                    return { ...p, ...metrics };
                }),
            ),
            getAllRunHistory(flowNames),
            getDeploymentSchedules(pipelines),
        ]);

        return pipelineRows.map((p) => {
            const sched = liveSchedules[p.id];
            return {
                ...p,
                frequency: sched?.frequency ?? p.fallbackFrequency,
                time: sched?.time ?? p.fallbackTime,
                enabled: sched?.enabled ?? p.fallbackEnabled,
                runHistory: runHistoryMap[p.id] ?? [],
            };
        });
    } catch {
        const pipelines = await getPipelines().catch(() => [] as PipelineMeta[]);
        return pipelines.map((p) => ({
            ...p,
            frequency: p.fallbackFrequency,
            time: p.fallbackTime,
            enabled: p.fallbackEnabled,
            rows: 0,
            earliest: null,
            latest: null,
            lastIngestion: null,
            coverage: [],
            runHistory: [],
        }));
    }
}

/* ── page ──────────────────────────────────────────────────── */

export default async function PipelinesPage() {
    return measurePageRender(async () => {
        const pipelines = await fetchPipelineData();

        return (
            <div className="h-full w-full overflow-y-auto">
                {/* ── header ─────────────────────────────── */}
                <div className="max-w-5xl mx-auto px-6 sm:px-10 pt-16 pb-6">
                    <Link
                        href="/monitor"
                        className="inline-flex items-center gap-1 mb-4 transition-colors"
                        style={{ color: 'rgba(255,255,255,0.3)' }}
                    >
                        <ChevronLeft size={14} />
                        <span className="text-[11px] font-mono uppercase tracking-wider hover:text-white/60 transition-colors">
                            Monitor
                        </span>
                    </Link>
                    <h1 className="text-[20px] font-semibold tracking-tight text-white">Pipelines &amp; Schedules</h1>
                    <p className="text-[12.5px] mt-1.5" style={{ color: 'rgba(255,255,255,0.3)' }}>
                        Ingestion schedules, data freshness, and pipeline configuration
                    </p>
                </div>

                {/* ── pipeline cards ──────────────────────── */}
                <div className="max-w-5xl mx-auto px-6 sm:px-10 pb-10">
                    <PipelineCards pipelines={pipelines} />
                </div>
            </div>
        );
    }, { routePath: "/monitor/pipelines" });
}
