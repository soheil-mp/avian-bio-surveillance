/**
 * /api/schedules — read and update config/schedules.json.
 *
 * GET  → returns the full schedules.json content.
 * PUT  → receives a partial update for a single deployment entry,
 *        merges it into the file, and writes back atomically.
 *
 * The file lives at `config/schedules.json` relative to the project
 * root (one level above `frontend/`).
 */

import { NextRequest, NextResponse } from 'next/server';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

import { withMeasuredApiRoute } from '@/lib/performance/server';
import { readFileMeasured, writeFileMeasured } from '@/lib/server-fs';

/**
 * Resolve config/schedules.json for both environments:
 *   - Docker standalone: cwd = /app, config mounted at /app/config
 *   - Local dev (`next dev` from frontend/): cwd = frontend/, config at ../config
 */
const SCHEDULES_PATH = existsSync(join(process.cwd(), 'config', 'schedules.json'))
    ? join(process.cwd(), 'config', 'schedules.json')
    : join(process.cwd(), '..', 'config', 'schedules.json');

type Deployment = {
    name: string;
    flow: string;
    cron: string;
    tags: string[];
    description: string;
    concurrency_limit: number;
    enabled: boolean;
    run_on_startup: boolean;
    parameters: Record<string, unknown>;
    _comment?: string;
};

type SchedulesFile = {
    _comment: string;
    deployments: Deployment[];
};

async function readSchedules(): Promise<SchedulesFile> {
    const raw = await readFileMeasured(SCHEDULES_PATH, 'utf-8');
    return JSON.parse(raw) as SchedulesFile;
}

async function writeSchedules(data: SchedulesFile): Promise<void> {
    await writeFileMeasured(SCHEDULES_PATH, JSON.stringify(data, null, 4) + '\n', 'utf-8');
}

/* ── GET /api/schedules ─────────────────────────────────── */

const handleGet = async (_request: Request) => {
    try {
        const data = await readSchedules();
        return NextResponse.json(data);
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return NextResponse.json({ error: msg }, { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);

/* ── PUT /api/schedules ─────────────────────────────────── */

export type ScheduleUpdate = {
    /** The deployment `name` field in schedules.json (e.g. "birdweather-daily"). */
    deploymentName: string;
    /** Fields to merge into the deployment entry. */
    patch: {
        cron?: string;
        enabled?: boolean;
        parameters?: Record<string, unknown>;
    };
};

const handlePut = async (req: NextRequest) => {
    try {
        const body = (await req.json()) as ScheduleUpdate;

        if (!body.deploymentName || !body.patch) {
            return NextResponse.json(
                { error: 'Missing deploymentName or patch.' },
                { status: 400 },
            );
        }

        const data = await readSchedules();
        const idx = data.deployments.findIndex((d) => d.name === body.deploymentName);

        if (idx === -1) {
            return NextResponse.json(
                { error: `Deployment "${body.deploymentName}" not found in schedules.json.` },
                { status: 404 },
            );
        }

        /* Merge only allowed fields to prevent arbitrary writes. */
        if (body.patch.cron !== undefined) {
            data.deployments[idx].cron = body.patch.cron;
        }
        if (body.patch.enabled !== undefined) {
            data.deployments[idx].enabled = body.patch.enabled;
        }
        if (body.patch.parameters !== undefined) {
            data.deployments[idx].parameters = {
                ...data.deployments[idx].parameters,
                ...body.patch.parameters,
            };
        }

        await writeSchedules(data);

        return NextResponse.json({
            ok: true,
            deployment: data.deployments[idx],
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return NextResponse.json({ error: msg }, { status: 500 });
    }
};

export const PUT = withMeasuredApiRoute(handlePut);
