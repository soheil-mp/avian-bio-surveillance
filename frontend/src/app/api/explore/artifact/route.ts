import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

import { NextRequest, NextResponse } from 'next/server';

import {
  artifactObjectPath,
  buildExploreMediaPlan,
  type ExploreDetectionRow,
  type ExploreVisualKind,
} from '@/app/explore/server';
import { query } from '@/lib/db';
import { objectExists, presignedGetObjectUrl } from '@/lib/minio';
import { withMeasuredApiRoute } from '@/lib/performance/server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const execFileAsync = promisify(execFile);
const ARTIFACT_GENERATOR_CONTAINER =
  process.env.EXPLORE_ARTIFACT_GENERATOR_CONTAINER?.trim() || 'ab-ingestion';
const ARTIFACT_GENERATOR_PYTHON =
  process.env.EXPLORE_ARTIFACT_GENERATOR_PYTHON?.trim() || 'python';
const ARTIFACT_GENERATOR_MODULE =
  'avian_biosurveillance.features.materialize_birdweather_visual_artifacts';
const RESPONSE_HEADERS = {
  'Cache-Control': 'private, no-store, max-age=0, must-revalidate',
};

type ArtifactLookupRow = Pick<
  ExploreDetectionRow,
  | 'detection_id'
  | 'observation_date'
  | 'station_id'
  | 'audio_filename'
  | 'audio_url'
  | 'feature_artifacts'
  | 'has_processed_clip'
>;

type ArtifactMaterializationResult = {
  detection_id: string;
  status: 'materialized' | 'existing' | 'missing_clip' | 'not_found' | 'failed';
  processed_clip: string | null;
  waveform: string | null;
  spectrogram: string | null;
  smooth_waveform: string | null;
  updated_db: boolean;
  error: string | null;
};

const pendingMaterializations = new Map<
  string,
  Promise<ArtifactMaterializationResult>
>();

// On-demand artifact generation spins up a fresh (expensive) Python process in
// the ingestion container, so we never run more than a couple at once — a page
// of detections must not launch a dozen generators in parallel. Per-detection
// de-duplication happens via pendingMaterializations above; this caps the
// global concurrency on top of that.
const MAX_CONCURRENT_MATERIALIZATIONS = 2;
let activeMaterializations = 0;
const materializationWaiters: Array<() => void> = [];

function acquireMaterializationSlot(): Promise<void> {
  if (activeMaterializations < MAX_CONCURRENT_MATERIALIZATIONS) {
    activeMaterializations += 1;
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => {
    materializationWaiters.push(resolve);
  });
}

function releaseMaterializationSlot(): void {
  const next = materializationWaiters.shift();
  if (next) {
    next();
    return;
  }
  activeMaterializations = Math.max(0, activeMaterializations - 1);
}

function readVisualKind(value: string | null): ExploreVisualKind | null {
  if (value === 'waveform' || value === 'spectrogram') {
    return value;
  }
  return null;
}

function readDetectionId(value: string | null): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function parseMaterializationOutput(stdout: string): ArtifactMaterializationResult {
  const lines = stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index];
    if (!line.startsWith('{')) {
      continue;
    }

    return JSON.parse(line) as ArtifactMaterializationResult;
  }

  throw new Error('Artifact materializer did not emit JSON output.');
}

const ARTIFACT_CHUNK_RE = /^_timescaledb_internal\.[a-z0-9_]+$/;

function readArtifactDate(value: string | null): string | null {
  return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

async function chunksForArtifactDate(date: string): Promise<string[]> {
  // Cover the day plus its neighbours so a detection whose observation_date sits
  // near a chunk boundary is still found. show_chunks is a cheap catalog lookup.
  const res = await query<{ chunk: string | null }>(
    `SELECT show_chunks(
        'birdweather_tbl',
        newer_than => $1::date - INTERVAL '1 day',
        older_than => $1::date + INTERVAL '1 day'
     )::text AS chunk`,
    [date],
  );
  return res.rows
    .map((r) => (typeof r.chunk === 'string' ? r.chunk.trim() : ''))
    .filter((name) => ARTIFACT_CHUNK_RE.test(name));
}

async function loadArtifactLookupRow(
  detectionId: string,
  date: string | null,
): Promise<ArtifactLookupRow | null> {
  // Fast path: when we know the observation date, resolve the day's chunk(s) and
  // query them directly. The plain `WHERE detection_id = $1` on the hypertable
  // (below) cannot prune chunks, so under the backfill it scans all ~700+ chunks
  // and times out — which is what was exhausting the DB connection pool.
  if (date) {
    try {
      const chunks = await chunksForArtifactDate(date);
      if (chunks.length) {
        const branches = chunks
          .map(
            (chunk) => `SELECT detection_id, observation_date, station_id,
             audio_filename, audio_url, feature_artifacts, has_processed_clip, detected_at
      FROM ${chunk} WHERE detection_id = $1`,
          )
          .join('\n      UNION ALL\n      ');
        const scoped = await query<ArtifactLookupRow & { detected_at: string }>(
          `${branches}\n      ORDER BY detected_at DESC\n      LIMIT 1`,
          [detectionId],
        );
        if (scoped.rows[0]) {
          return scoped.rows[0];
        }
      }
    } catch {
      // Fall through to the unbounded lookup below.
    }
  }

  const result = await query<ArtifactLookupRow>(
    `SELECT
       detection_id,
       observation_date,
       station_id,
       audio_filename,
       audio_url,
       feature_artifacts,
       has_processed_clip
     FROM birdweather_tbl
     WHERE detection_id = $1
     ORDER BY detected_at DESC
     LIMIT 1`,
    [detectionId],
  );

  return result.rows[0] ?? null;
}

function resolveRequestedArtifactPath(
  row: ArtifactLookupRow,
  kind: ExploreVisualKind,
): string | null {
  const mediaPlan = buildExploreMediaPlan(row);
  return kind === 'waveform' ? mediaPlan.waveformPath : mediaPlan.spectrogramPath;
}

async function redirectToFeatureObject(objectPath: string): Promise<NextResponse> {
  const url = await presignedGetObjectUrl('features', objectPath);
  const response = NextResponse.redirect(url, { status: 307 });
  for (const [name, value] of Object.entries(RESPONSE_HEADERS)) {
    response.headers.set(name, value);
  }
  return response;
}

async function materializeArtifacts(
  detectionId: string,
): Promise<ArtifactMaterializationResult> {
  const cached = pendingMaterializations.get(detectionId);
  if (cached) {
    return cached;
  }

  const pending = (async () => {
    await acquireMaterializationSlot();
    try {
      const { stdout } = await execFileAsync(
        'docker',
        [
          'exec',
          '-i',
          ARTIFACT_GENERATOR_CONTAINER,
          ARTIFACT_GENERATOR_PYTHON,
          '-m',
          ARTIFACT_GENERATOR_MODULE,
          '--detection-id',
          detectionId,
        ],
        {
          timeout: 120_000,
          maxBuffer: 1024 * 1024,
        },
      );
      return parseMaterializationOutput(stdout);
    } finally {
      releaseMaterializationSlot();
      pendingMaterializations.delete(detectionId);
    }
  })();

  pendingMaterializations.set(detectionId, pending);
  return pending;
}

function materializedArtifactPath(
  result: ArtifactMaterializationResult,
  kind: ExploreVisualKind,
): string | null {
  return artifactObjectPath(
    kind === 'waveform'
      ? (result.smooth_waveform ?? result.waveform)
      : result.spectrogram,
    'features',
  );
}

async function handleGet(request: NextRequest) {
  const detectionId = readDetectionId(
    request.nextUrl.searchParams.get('detectionId'),
  );
  const kind = readVisualKind(request.nextUrl.searchParams.get('kind'));
  const date = readArtifactDate(request.nextUrl.searchParams.get('date'));

  if (!detectionId || !kind) {
    return NextResponse.json(
      { error: 'Invalid detectionId or kind.' },
      {
        status: 400,
        headers: RESPONSE_HEADERS,
      },
    );
  }

  const row = await loadArtifactLookupRow(detectionId, date);
  if (!row || !row.has_processed_clip) {
    return NextResponse.json(
      { error: 'Processed clip not available for this detection.' },
      {
        status: 404,
        headers: RESPONSE_HEADERS,
      },
    );
  }

  const existingPath = resolveRequestedArtifactPath(row, kind);
  if (existingPath && (await objectExists('features', existingPath))) {
    return redirectToFeatureObject(existingPath);
  }

  try {
    const result = await materializeArtifacts(detectionId);
    const objectPath = materializedArtifactPath(result, kind);

    if (objectPath && (await objectExists('features', objectPath))) {
      return redirectToFeatureObject(objectPath);
    }

    const status =
      result.status === 'not_found' || result.status === 'missing_clip' ? 404 : 502;
    return NextResponse.json(
      {
        error: result.error || `Unable to materialize ${kind} for ${detectionId}.`,
        status: result.status,
      },
      {
        status,
        headers: RESPONSE_HEADERS,
      },
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return NextResponse.json(
      {
        error: `Artifact generator failed for ${detectionId}: ${message}`,
      },
      {
        status: 502,
        headers: RESPONSE_HEADERS,
      },
    );
  }
}

export const GET = withMeasuredApiRoute(handleGet);
