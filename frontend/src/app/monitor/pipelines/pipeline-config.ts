import 'server-only';

import { existsSync } from 'node:fs';
import { join } from 'node:path';

import { readFileMeasured } from '@/lib/server-fs';

/** Pipeline kind: daily ingestion, catchup, repair, or post-processing. */
export type PipelineKind = 'daily' | 'catchup' | 'repair' | 'post-processing';

export type PipelineMeta = {
    /** Unique id — must match the `name` field in schedules.json. */
    id: string;
    /** Human-readable display name. */
    name: string;
    /** Data source description shown as subtitle. */
    source: string;
    /** Pipeline category for grouping/filtering. */
    kind: PipelineKind;
    /** DB table that holds the pipeline output (for coverage/freshness queries). */
    table: string;
    /** Column used for temporal ordering (e.g. detected_at). */
    orderCol: string;
    /** Column used for coverage heatmap bucketing. */
    coverageCol: string;
    /** Bucket width for coverage heatmap. */
    coverageSpan: 'daily' | 'monthly';
    /** Column used for "last ingestion" freshness. */
    ingestionCol: string;
    /** Dagster job name (snake_case derived from the flow's name). */
    deploymentName: string;
    /** Flow key in schedules.json — matches the Dagster op + job name. */
    flowKey: string;
    /** Shown when Dagster is unreachable. */
    fallbackFrequency: string;
    /** Shown when Dagster is unreachable. */
    fallbackTime: string;
    /** Shown when Dagster is unreachable. */
    fallbackEnabled: boolean;
};

type PipelineStaticMeta = {
    name: string;
    source: string;
    table: string;
    orderCol: string;
    coverageCol: string;
    coverageSpan: 'daily' | 'monthly';
    ingestionCol: string;
};

type ScheduleDeployment = {
    name: string;
    flow: string;
    cron: string;
    tags: string[];
    description: string;
    enabled: boolean;
};

type SchedulesFile = {
    deployments: ScheduleDeployment[];
};

const SCHEDULES_PATH = existsSync(join(process.cwd(), 'config', 'schedules.json'))
    ? join(process.cwd(), 'config', 'schedules.json')
    : join(process.cwd(), '..', 'config', 'schedules.json');

const PIPELINE_METADATA: Record<string, PipelineStaticMeta> = {
    'birdweather-daily': {
        name: 'BirdWeather Detections',
        source: 'BirdWeather GraphQL API',
        table: 'birdweather_tbl',
        orderCol: 'detected_at',
        coverageCol: 'observation_date',
        coverageSpan: 'daily',
        ingestionCol: 'detected_at',
    },
    'knmi-daily': {
        name: 'KNMI Weather Observations',
        source: 'KNMI EDR / Open Data REST API',
        table: 'knmi_tbl',
        orderCol: 'observed_at',
        coverageCol: 'observed_at',
        coverageSpan: 'daily',
        ingestionCol: 'observed_at',
    },
    'ebird-daily': {
        name: 'eBird Observations',
        source: 'eBird REST API (recent observations)',
        table: 'ebird_tbl',
        orderCol: 'obs_dt',
        coverageCol: 'obs_dt',
        coverageSpan: 'daily',
        ingestionCol: 'obs_dt',
    },
    'movebank-daily': {
        name: 'Movebank GPS Tracking',
        source: 'Movebank REST API (target species)',
        table: 'movebank_tbl',
        orderCol: 'timestamp',
        coverageCol: 'timestamp',
        coverageSpan: 'daily',
        ingestionCol: 'timestamp',
    },
    'sovon-daily': {
        name: 'Sovon Mortality Records',
        source: 'Sovon Portal + Vision Model',
        table: 'sovon_tbl',
        orderCol: 'observation_date',
        coverageCol: 'observation_date',
        coverageSpan: 'monthly',
        ingestionCol: 'observation_date',
    },
    'seismic-daily': {
        name: 'KNMI Seismic Events',
        source: 'KNMI FDSN Event API',
        table: 'seismic_tbl',
        orderCol: 'event_time',
        coverageCol: 'event_time',
        coverageSpan: 'daily',
        ingestionCol: 'event_time',
    },
    'earth-engine-grid-daily': {
        name: 'Earth Engine Base Grid Profiles',
        source: 'Google Earth Engine (5 km Netherlands base grid)',
        table: 'google_earth_engine_tbl',
        orderCol: 'observation_date',
        coverageCol: 'observation_date',
        coverageSpan: 'daily',
        ingestionCol: 'observation_date',
    },
    'birdweather-repair': {
        name: 'BirdWeather Repair',
        source: 'BirdWeather GraphQL API (gap repair)',
        table: 'birdweather_tbl',
        orderCol: 'detected_at',
        coverageCol: 'observation_date',
        coverageSpan: 'daily',
        ingestionCol: 'detected_at',
    },
    'knmi-repair': {
        name: 'KNMI Weather Repair',
        source: 'KNMI EDR / Open Data (date-range repair)',
        table: 'knmi_tbl',
        orderCol: 'observed_at',
        coverageCol: 'observed_at',
        coverageSpan: 'daily',
        ingestionCol: 'observed_at',
    },
    'ebird-repair': {
        name: 'eBird Repair',
        source: 'eBird REST API (historic repair)',
        table: 'ebird_tbl',
        orderCol: 'obs_dt',
        coverageCol: 'obs_dt',
        coverageSpan: 'daily',
        ingestionCol: 'obs_dt',
    },
    'movebank-repair': {
        name: 'Movebank GPS Repair',
        source: 'Movebank REST API (date-range repair)',
        table: 'movebank_tbl',
        orderCol: 'timestamp',
        coverageCol: 'timestamp',
        coverageSpan: 'daily',
        ingestionCol: 'timestamp',
    },
    'earth-engine-grid-repair': {
        name: 'Earth Engine Base Grid Repair',
        source: 'Google Earth Engine 5 km base grid (date-range repair)',
        table: 'google_earth_engine_tbl',
        orderCol: 'observation_date',
        coverageCol: 'observation_date',
        coverageSpan: 'daily',
        ingestionCol: 'observation_date',
    },
    'cds-drought-daily': {
        name: 'CDS Drought Indicators',
        source: 'Copernicus CDS ERA5-derived monthly drought grid',
        table: 'cds_drought_tbl',
        orderCol: 'observation_month',
        coverageCol: 'observation_month',
        coverageSpan: 'monthly',
        ingestionCol: 'observation_month',
    },
    'cds-drought-catchup': {
        name: 'CDS Drought Catchup',
        source: 'Copernicus CDS ERA5-derived monthly drought grid (delayed monthly release)',
        table: 'cds_drought_tbl',
        orderCol: 'observation_month',
        coverageCol: 'observation_month',
        coverageSpan: 'monthly',
        ingestionCol: 'observation_month',
    },
    'historical-catchup': {
        name: 'Historical Catchup',
        source: 'One-day replay across BirdWeather, KNMI, eBird, and Earth Engine with post-processing follow-up',
        table: '',
        orderCol: '',
        coverageCol: '',
        coverageSpan: 'daily',
        ingestionCol: '',
    },
    'station-summary-refresh': {
        name: 'Station Summary Refresh',
        source: 'PL/pgSQL aggregate + inline quality scores',
        table: 'birdweather_stations_tbl',
        orderCol: 'last_detection',
        coverageCol: 'last_detection',
        coverageSpan: 'daily',
        ingestionCol: 'last_detection',
    },
    'quality-audit': {
        name: 'Data Quality Audit',
        source: 'Quality module (ISO 25012, 6 dimensions)',
        table: 'data_quality_tbl',
        orderCol: 'created_at',
        coverageCol: 'created_at',
        coverageSpan: 'daily',
        ingestionCol: 'created_at',
    },
    'derived-aggregates-refresh': {
        name: 'Derived Aggregates Refresh',
        source: 'Refreshes species_tbl, movebank_tracks_tbl, and species_tracker_tbl for dashboard reads',
        table: 'species_tracker_tbl',
        orderCol: 'period_start',
        coverageCol: 'period_start',
        coverageSpan: 'daily',
        ingestionCol: 'computed_at',
    },
};

function padCronPart(value: string): string {
    return value.padStart(2, '0');
}

export function parseCron(cron: string): { frequency: string; time: string } {
    const [minute = '0', hour = '0', dayOfMonth = '*', , dayOfWeek = '*'] = cron.trim().split(/\s+/);

    if (hour === '*') {
        return { frequency: 'Hourly', time: `00:${padCronPart(minute)}` };
    }
    if (dayOfMonth !== '*' && dayOfWeek === '*') {
        return {
            frequency: 'Monthly',
            time: `${padCronPart(hour)}:${padCronPart(minute)}`,
        };
    }
    if (dayOfWeek !== '*') {
        return {
            frequency: 'Weekly',
            time: `${padCronPart(hour)}:${padCronPart(minute)}`,
        };
    }
    return {
        frequency: 'Daily',
        time: `${padCronPart(hour)}:${padCronPart(minute)}`,
    };
}

function inferKind(tags: string[]): PipelineKind {
    if (tags.includes('catchup')) return 'catchup';
    if (tags.includes('repair')) return 'repair';
    if (tags.includes('post-processing') || tags.includes('audit')) return 'post-processing';
    return 'daily';
}

function inferDisplayName(id: string): string {
    return id
        .split('-')
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(' ');
}

function toDagsterJobName(flowKey: string): string {
    // Mirrors `_safe_job_name` in dagster_defs/jobs.py: hyphens → underscores.
    return flowKey.replace(/-/g, '_');
}

async function readSchedules(): Promise<SchedulesFile> {
    const raw = await readFileMeasured(SCHEDULES_PATH, 'utf-8');
    return JSON.parse(raw) as SchedulesFile;
}

export async function getPipelines(): Promise<PipelineMeta[]> {
    const { deployments } = await readSchedules();

    return deployments.map((deployment) => {
        const metadata = PIPELINE_METADATA[deployment.name];
        const parsed = parseCron(deployment.cron);

        return {
            id: deployment.name,
            name: metadata?.name ?? inferDisplayName(deployment.name),
            source: metadata?.source ?? deployment.description,
            kind: inferKind(deployment.tags),
            table: metadata?.table ?? '',
            orderCol: metadata?.orderCol ?? '',
            coverageCol: metadata?.coverageCol ?? '',
            coverageSpan: metadata?.coverageSpan ?? 'daily',
            ingestionCol: metadata?.ingestionCol ?? '',
            deploymentName: toDagsterJobName(deployment.flow),
            flowKey: deployment.flow,
            fallbackFrequency: parsed.frequency,
            fallbackTime: parsed.time,
            fallbackEnabled: deployment.enabled,
        };
    });
}

export async function getDeploymentMap(): Promise<Record<string, string>> {
    const pipelines = await getPipelines();
    return Object.fromEntries(pipelines.map((pipeline) => [pipeline.id, pipeline.deploymentName]));
}

export async function getFlowNames(): Promise<Record<string, string>> {
    const pipelines = await getPipelines();
    return Object.fromEntries(
        pipelines.map((pipeline) => [pipeline.id, pipeline.flowKey]),
    );
}
