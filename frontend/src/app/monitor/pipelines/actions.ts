'use server';

import { getPipelines } from './pipeline-config';
import type { ScheduleUpdate } from '@/app/api/schedules/route';
import { measureServerAction } from '@/lib/performance/server';
import {
    buildDagsterRunConfig,
    DAGSTER_REPOSITORY_NAME,
    dagsterGraphQL,
    deploymentToScheduleName,
    flowKeyToJobName,
    flowKeyToLocationName,
} from '@/lib/dagster';

const INTERNAL_URL = process.env.INTERNAL_URL || 'http://localhost:3000';

/* ── helpers: cron round-trip ─────────────────────────────── */

function toCron(frequency: string, time: string): string {
    const [hStr, mStr] = time.split(':');
    const hour = parseInt(hStr, 10) || 0;
    const minute = parseInt(mStr, 10) || 0;
    switch (frequency) {
        case 'Hourly': return `${minute} * * * *`;
        case 'Weekly': return `${minute} ${hour} * * 0`;
        case 'Monthly': return `${minute} ${hour} 1 * *`;
        default: return `${minute} ${hour} * * *`;
    }
}

/* ── helpers: persist to schedules.json via internal API ──── */

async function persistToSchedulesJson(
    pipelineId: string,
    cron: string,
    enabled: boolean,
): Promise<void> {
    const body: ScheduleUpdate = {
        deploymentName: pipelineId,
        patch: { cron, enabled },
    };
    const res = await fetch(`${INTERNAL_URL}/api/schedules`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || `Failed to persist schedule (HTTP ${res.status})`);
    }
}

/* ── helpers: Dagster GraphQL operations ─────────────────── */

async function reloadCodeLocation(locationName: string): Promise<void> {
    const mutation = /* GraphQL */ `
        mutation Reload($name: String!) {
            reloadRepositoryLocation(repositoryLocationName: $name) {
                __typename
                ... on PythonError { message }
                ... on RepositoryLocationNotFound { message }
            }
        }
    `;
    type Resp = { reloadRepositoryLocation: { __typename: string; message?: string } };
    const data = await dagsterGraphQL<Resp>(
        mutation,
        { name: locationName },
        30_000,
    );
    const t = data.reloadRepositoryLocation.__typename;
    if (t === 'PythonError' || t === 'RepositoryLocationNotFound') {
        throw new Error(data.reloadRepositoryLocation.message || `Reload failed: ${t}`);
    }
}

async function setScheduleStatus(
    locationName: string,
    scheduleName: string,
    enabled: boolean,
): Promise<void> {
    const selector = {
        repositoryLocationName: locationName,
        repositoryName: DAGSTER_REPOSITORY_NAME,
        scheduleName,
    };
    if (enabled) {
        const mutation = /* GraphQL */ `
            mutation Start($s: ScheduleSelector!) {
                startSchedule(scheduleSelector: $s) {
                    __typename
                    ... on PythonError { message }
                }
            }
        `;
        type Resp = { startSchedule: { __typename: string; message?: string } };
        const data = await dagsterGraphQL<Resp>(mutation, { s: selector });
        if (data.startSchedule.__typename === 'PythonError') {
            throw new Error(data.startSchedule.message || 'Failed to start schedule.');
        }
        return;
    }

    const mutation = /* GraphQL */ `
        mutation Stop($s: ScheduleSelector!) {
            stopRunningScheduleByName(
                repositoryLocationName: $s.repositoryLocationName,
                repositoryName: $s.repositoryName,
                scheduleName: $s.scheduleName,
            ) {
                __typename
                ... on PythonError { message }
            }
        }
    `;
    type Resp = {
        stopRunningScheduleByName: { __typename: string; message?: string };
    };
    const data = await dagsterGraphQL<Resp>(mutation, { s: selector });
    if (data.stopRunningScheduleByName.__typename === 'PythonError') {
        throw new Error(
            data.stopRunningScheduleByName.message || 'Failed to stop schedule.',
        );
    }
}

/* ── public Server Actions ──────────────────────────────── */

export async function saveSchedule(
    pipelineId: string,
    config: { frequency: string; time: string; enabled: boolean },
): Promise<{ ok: boolean; message: string }> {
    return measureServerAction('monitor.pipelines.saveSchedule', async () => {
        const pipelines = await getPipelines();
        const meta = pipelines.find((p) => p.id === pipelineId);
        if (!meta) {
            return { ok: false, message: `Unknown pipeline "${pipelineId}".` };
        }
        const cron = toCron(config.frequency, config.time);
        const locationName = flowKeyToLocationName(meta.flowKey);

        try {
            await persistToSchedulesJson(pipelineId, cron, config.enabled);
            await reloadCodeLocation(locationName);
            await setScheduleStatus(
                locationName,
                deploymentToScheduleName(pipelineId),
                config.enabled,
            );
            return {
                ok: true,
                message: `Schedule updated — ${config.frequency} at ${config.time} UTC.`,
            };
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            if (msg.includes('abort') || msg.includes('timeout')) {
                return {
                    ok: false,
                    message: 'Dagster GraphQL timed out. Ensure the dagster-webserver service is running.',
                };
            }
            return { ok: false, message: `Save failed: ${msg}` };
        }
    });
}

export async function triggerPipeline(
    pipelineId: string,
): Promise<{ ok: boolean; message: string; url?: string }> {
    return measureServerAction('monitor.pipelines.triggerPipeline', async () => {
        const pipelines = await getPipelines();
        const meta = pipelines.find((p) => p.id === pipelineId);
        if (!meta) {
            return { ok: false, message: `Unknown pipeline "${pipelineId}".` };
        }

        const jobName = flowKeyToJobName(meta.flowKey);
        const locationName = flowKeyToLocationName(meta.flowKey);

        let parameters: Record<string, unknown> = {};
        try {
            const res = await fetch(`${INTERNAL_URL}/api/schedules`, {
                signal: AbortSignal.timeout(5_000),
                cache: 'no-store',
            });
            if (res.ok) {
                const body = await res.json();
                const dep = (body.deployments || []).find(
                    (d: { name: string; parameters?: Record<string, unknown> }) =>
                        d.name === pipelineId,
                );
                parameters = dep?.parameters || {};
            }
        } catch { /* fall back to empty parameters */ }

        const runConfigData = buildDagsterRunConfig(meta.flowKey, parameters);

        const mutation = /* GraphQL */ `
            mutation LaunchManual($p: ExecutionParams!) {
                launchRun(executionParams: $p) {
                    __typename
                    ... on LaunchRunSuccess { run { runId } }
                    ... on RunConfigValidationInvalid { errors { message } }
                    ... on PythonError { message }
                }
            }
        `;
        type Resp = {
            launchRun:
                | { __typename: 'LaunchRunSuccess'; run: { runId: string } }
                | { __typename: 'RunConfigValidationInvalid'; errors: { message: string }[] }
                | { __typename: 'PythonError'; message: string };
        };

        try {
            const data = await dagsterGraphQL<Resp>(
                mutation,
                {
                    p: {
                        selector: {
                            repositoryLocationName: locationName,
                            repositoryName: DAGSTER_REPOSITORY_NAME,
                            pipelineName: jobName,
                        },
                        runConfigData,
                        tags: [
                            { key: 'avian/deployment', value: pipelineId },
                            { key: 'avian/trigger', value: 'manual' },
                        ],
                        mode: 'default',
                    },
                },
                10_000,
            );

            const r = data.launchRun;
            if (r.__typename === 'LaunchRunSuccess') {
                const ui = process.env.NEXT_PUBLIC_DAGSTER_UI_URL || 'http://localhost:13030';
                return {
                    ok: true,
                    message: `Run launched (${r.run.runId.slice(0, 8)}…)`,
                    url: `${ui}/runs/${r.run.runId}`,
                };
            }
            if (r.__typename === 'RunConfigValidationInvalid') {
                return {
                    ok: false,
                    message: `Run-config invalid: ${r.errors.map((e) => e.message).join('; ')}`,
                };
            }
            return { ok: false, message: r.message };
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            if (msg.includes('abort') || msg.includes('timeout')) {
                return {
                    ok: false,
                    message: 'Dagster GraphQL timed out. Ensure the dagster-webserver service is running.',
                };
            }
            return { ok: false, message: `Launch failed: ${msg}` };
        }
    });
}
