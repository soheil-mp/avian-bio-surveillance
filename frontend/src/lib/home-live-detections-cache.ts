import "server-only";

import { tmpdir } from "node:os";
import { join } from "node:path";

import type { LiveDetection } from "@/components/home/LiveDetectionsTable";
import { fetchLiveDetections, type FeedSort } from "@/lib/home-live-detections";
import { readFileMeasured, writeFileMeasured } from "@/lib/server-fs";

// Durable last-good snapshot of the live feed, kept per sort mode. The
// underlying query is cheap to execute but expensive to *plan* against the
// many-chunk hypertable when the catalog cache is cold; this layer guarantees
// the request path serves the last good result instantly (stale-while-
// revalidate) and never stampedes the DB. Each mode ("latest" / "anomaly")
// gets its own snapshot, so toggling order in the UI is instant once both warm.
function snapshotPath(sort: FeedSort): string {
  return join(tmpdir(), `avian-home-live-detections-v3-${sort}.json`);
}

// Serve without refreshing if the snapshot is newer than this. Kept generous
// because, while the 2024–25 backfill runs, planning a query against the
// many-chunk hypertable can take minutes; refreshing more often would keep a
// heavy query almost permanently in flight and slow every other route. Lower
// this (e.g. to 30_000) once the backfill is done and cold-plan time drops.
const FRESH_MS = 5 * 60_000;
// Hard floor on refresh frequency, on top of single-flight, so a failed/slow
// refresh can never be retried in a tight loop.
const REFRESH_MIN_GAP_MS = 2 * 60_000;
// Allow an empty result to overwrite a non-empty snapshot only once it is this
// old, so a transient cold-timeout empty can't wipe a good snapshot, but a feed
// that genuinely goes quiet still self-corrects eventually.
const STALE_OVERWRITE_MS = 10 * 60_000;

type Snapshot = { detections: LiveDetection[]; fetchedAt: number };

type SortState = {
  memory: Snapshot | null;
  loadedFromDisk: boolean;
  refreshing: Promise<void> | null;
  lastRefreshStartedAt: number;
};

// One independent stale-while-revalidate state per sort mode.
const states = new Map<FeedSort, SortState>();

function stateFor(sort: FeedSort): SortState {
  let state = states.get(sort);
  if (!state) {
    state = { memory: null, loadedFromDisk: false, refreshing: null, lastRefreshStartedAt: 0 };
    states.set(sort, state);
  }
  return state;
}

async function loadFromDisk(sort: FeedSort): Promise<void> {
  const state = stateFor(sort);
  if (state.loadedFromDisk) return;
  state.loadedFromDisk = true;
  try {
    const raw = await readFileMeasured(snapshotPath(sort), "utf8");
    const parsed = JSON.parse(raw) as Snapshot;
    if (parsed && Array.isArray(parsed.detections) && typeof parsed.fetchedAt === "number") {
      state.memory = parsed;
    }
  } catch {
    // No snapshot on disk yet — first run.
  }
}

async function persist(sort: FeedSort, snapshot: Snapshot): Promise<void> {
  try {
    await writeFileMeasured(snapshotPath(sort), JSON.stringify(snapshot), "utf8");
  } catch {
    // Best-effort: the in-memory copy still serves this process.
  }
}

function startRefresh(sort: FeedSort): Promise<void> {
  const state = stateFor(sort);
  // Single-flight: collapse concurrent refreshes into one in-flight query so a
  // burst of requests can never spawn a pile of slow plans.
  if (state.refreshing) return state.refreshing;
  state.lastRefreshStartedAt = Date.now();
  state.refreshing = (async () => {
    try {
      const detections = await fetchLiveDetections(sort);
      const allowEmptyOverwrite =
        !state.memory || Date.now() - state.memory.fetchedAt > STALE_OVERWRITE_MS;
      if (detections.length > 0 || allowEmptyOverwrite) {
        const snapshot: Snapshot = { detections, fetchedAt: Date.now() };
        state.memory = snapshot;
        await persist(sort, snapshot);
      }
    } catch {
      // Preserve the last good snapshot on failure/timeout.
    } finally {
      state.refreshing = null;
    }
  })();
  return state.refreshing;
}

function canStartRefresh(sort: FeedSort, now: number): boolean {
  const state = stateFor(sort);
  return !state.refreshing && now - state.lastRefreshStartedAt > REFRESH_MIN_GAP_MS;
}

/**
 * Returns the live-detection feed for the given sort mode, serving the last good
 * snapshot instantly and revalidating in the background. The request path never
 * blocks on a live query — even on a cold start it returns immediately and lets
 * a single-flight, rate-limited background refresh seed the snapshot when the DB
 * can serve it.
 */
export async function getLiveDetections(
  sort: FeedSort = "latest",
): Promise<LiveDetection[]> {
  await loadFromDisk(sort);
  const state = stateFor(sort);
  const now = Date.now();

  if (state.memory) {
    if (now - state.memory.fetchedAt > FRESH_MS && canStartRefresh(sort, now)) {
      // Fire-and-forget; serve the (possibly stale) snapshot immediately.
      void startRefresh(sort);
    }
    return state.memory.detections;
  }

  // No snapshot yet for this cache version. Block once so deploys/schema
  // changes do not show an empty feed until a background refresh finishes.
  if (canStartRefresh(sort, now)) await startRefresh(sort);
  return stateFor(sort).memory?.detections ?? [];
}
