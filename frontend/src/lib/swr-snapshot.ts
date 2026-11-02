import "server-only";

import { tmpdir } from "node:os";
import { join } from "node:path";

import { readFileMeasured, writeFileMeasured } from "@/lib/server-fs";

// Generic disk-backed stale-while-revalidate cache, keyed by an arbitrary
// string. Built for endpoints whose cost is a cold-plan-heavy query against the
// many-chunk hypertable: the request path serves the last good snapshot from
// disk instantly and revalidates in the background, so a load is only ever slow
// once per key (the first time it is seen, with no snapshot yet). The on-disk
// snapshot survives dev recompiles and prod restarts — which the in-memory Next
// data cache (unstable_cache) does not, and that gap is what makes a route feel
// "sometimes fast, sometimes slow".

type Entry<T> = { value: T; fetchedAt: number };

type KeyState<T> = {
  memory: Entry<T> | null;
  loadedFromDisk: boolean;
  refreshing: Promise<void> | null;
  lastRefreshStartedAt: number;
};

export type SnapshotCacheOptions<T> = {
  // Stable identifier; becomes part of the snapshot filename.
  name: string;
  // Serve without revalidating while the snapshot is newer than this.
  freshMs?: number;
  // Hard floor between background refreshes for a key (atop single-flight).
  refreshMinGapMs?: number;
  // A non-empty snapshot is only overwritten by an "empty" result once it is
  // this old, so a transient empty (e.g. a cold-timeout) can't wipe a good one.
  staleOverwriteMs?: number;
  // Classifies a value as empty for the guard above. Default: never empty.
  isEmpty?: (value: T) => boolean;
};

const DEFAULT_FRESH_MS = 5 * 60_000;
const DEFAULT_REFRESH_MIN_GAP_MS = 2 * 60_000;
const DEFAULT_STALE_OVERWRITE_MS = 10 * 60_000;

function safeSegment(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 120) || "_";
}

export function createSnapshotCache<T>(options: SnapshotCacheOptions<T>) {
  const freshMs = options.freshMs ?? DEFAULT_FRESH_MS;
  const refreshMinGapMs = options.refreshMinGapMs ?? DEFAULT_REFRESH_MIN_GAP_MS;
  const staleOverwriteMs = options.staleOverwriteMs ?? DEFAULT_STALE_OVERWRITE_MS;
  const isEmpty = options.isEmpty ?? (() => false);

  const states = new Map<string, KeyState<T>>();

  function stateFor(key: string): KeyState<T> {
    let state = states.get(key);
    if (!state) {
      state = { memory: null, loadedFromDisk: false, refreshing: null, lastRefreshStartedAt: 0 };
      states.set(key, state);
    }
    return state;
  }

  function snapshotPath(key: string): string {
    return join(tmpdir(), `avian-${safeSegment(options.name)}-${safeSegment(key)}.json`);
  }

  async function loadFromDisk(key: string): Promise<void> {
    const state = stateFor(key);
    if (state.loadedFromDisk) return;
    state.loadedFromDisk = true;
    try {
      const raw = await readFileMeasured(snapshotPath(key), "utf8");
      const parsed = JSON.parse(raw) as Entry<T>;
      if (parsed && typeof parsed.fetchedAt === "number" && "value" in parsed) {
        state.memory = parsed;
      }
    } catch {
      // No snapshot yet for this key — first run.
    }
  }

  async function persist(key: string, entry: Entry<T>): Promise<void> {
    try {
      await writeFileMeasured(snapshotPath(key), JSON.stringify(entry), "utf8");
    } catch {
      // Best-effort; the in-memory copy still serves this process.
    }
  }

  // Single-flight refresh for a key. A background refresh (propagateErrors =
  // false) swallows failures so the last good snapshot keeps serving; a blocking
  // cold load (propagateErrors = true) lets the caller see the error.
  function refresh(key: string, loader: () => Promise<T>, propagateErrors: boolean): Promise<void> {
    const state = stateFor(key);
    if (state.refreshing) return state.refreshing;
    state.lastRefreshStartedAt = Date.now();
    const run = (async () => {
      const value = await loader();
      const allowEmptyOverwrite =
        !state.memory || Date.now() - state.memory.fetchedAt > staleOverwriteMs;
      if (!isEmpty(value) || allowEmptyOverwrite) {
        const entry: Entry<T> = { value, fetchedAt: Date.now() };
        state.memory = entry;
        await persist(key, entry);
      }
    })();
    const guarded = propagateErrors ? run : run.catch(() => {});
    state.refreshing = guarded.finally(() => {
      state.refreshing = null;
    });
    return state.refreshing;
  }

  function canStartRefresh(key: string, now: number): boolean {
    const state = stateFor(key);
    return !state.refreshing && now - state.lastRefreshStartedAt > refreshMinGapMs;
  }

  return {
    /**
     * Serve the snapshot for `key` instantly when present (revalidating in the
     * background once stale). On a true cold miss (no snapshot yet) await the
     * loader once so the first render is correct, then persist it for every
     * later request — including after dev recompiles and prod restarts.
     */
    async get(key: string, loader: () => Promise<T>): Promise<T> {
      await loadFromDisk(key);
      const state = stateFor(key);
      const now = Date.now();

      if (state.memory) {
        if (now - state.memory.fetchedAt > freshMs && canStartRefresh(key, now)) {
          // Fire-and-forget; serve the (possibly stale) snapshot immediately.
          void refresh(key, loader, false);
        }
        return state.memory.value;
      }

      // Cold miss: block once (single-flight) on a correct first snapshot.
      await refresh(key, loader, true);
      const populated = stateFor(key).memory;
      if (populated) return populated.value;
      // Defensive: refresh resolved without storing — run the loader directly so
      // the request still gets a result.
      return loader();
    },
  };
}
