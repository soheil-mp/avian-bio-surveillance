import "server-only";

import type { LiveDetection } from "@/components/home/LiveDetectionsTable";
import { buildFeedQuery, type FeedFilters } from "@/lib/home-feed-filters";
import { fetchFilteredLiveDetections } from "@/lib/home-live-detections";

// Stale-while-revalidate cache for the *filtered* live feed: the live-map
// station/grid spotlight and the manual filter panel. The default (unfiltered)
// feed has its own durable per-sort snapshot (see home-live-detections-cache);
// this is its sibling for the bounded filtered queries, which previously re-ran
// 2–3 hypertable queries on every click. Keyed by the canonical filter query
// string, so re-selecting a station/grid — or toggling the spotlight popup shut
// then open — is served from memory while a single-flight background refresh
// keeps it current.

type Entry = {
  detections: LiveDetection[];
  // 0 until the first successful (or empty) result lands; used to distinguish a
  // cold key (must block) from a warm one (serve immediately, revalidate later).
  fetchedAt: number;
  refreshing: Promise<void> | null;
  lastRefreshStartedAt: number;
};

// Serve without refreshing while the entry is newer than this. Short, because a
// spotlight means "recent detections from here" and should feel live; long
// enough that the rapid click-around / popup-toggle pattern is always a hit.
const FRESH_MS = 60_000;
// Floor on refresh frequency per key (on top of single-flight) so a slow/cold
// refresh can never be retried in a tight loop.
const REFRESH_MIN_GAP_MS = 20_000;
// LRU bound on distinct filter keys held in memory. Each entry is at most
// LIVE_FEED_LIMIT rich rows; this caps worst-case memory while covering far
// more stations/grids than a user explores in one session.
const MAX_ENTRIES = 64;

const cache = new Map<string, Entry>();

// Re-insert to move a key to the most-recent position (Map keeps insertion
// order, so the first key is the LRU eviction candidate).
function touch(key: string, entry: Entry): void {
  cache.delete(key);
  cache.set(key, entry);
}

function evictIfNeeded(): void {
  while (cache.size > MAX_ENTRIES) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

function startRefresh(
  key: string,
  filters: FeedFilters,
  entry: Entry,
): Promise<void> {
  // Single-flight: collapse concurrent refreshes for the same key into one
  // in-flight query so a burst of clicks can't spawn a pile of slow plans.
  if (entry.refreshing) return entry.refreshing;
  entry.lastRefreshStartedAt = Date.now();
  entry.refreshing = (async () => {
    try {
      const detections = await fetchFilteredLiveDetections(filters);
      entry.detections = detections;
      entry.fetchedAt = Date.now();
    } catch {
      // Preserve the last good result on failure/timeout; a cold key stays cold
      // (fetchedAt 0) so the next request retries rather than caching an error.
    } finally {
      entry.refreshing = null;
    }
  })();
  return entry.refreshing;
}

/**
 * Returns the filtered live feed for a filter set, serving a cached result
 * instantly and revalidating in the background. A cold key blocks once on the
 * (single-flight) query so it never returns an empty placeholder; thereafter the
 * request path is non-blocking until the entry goes stale. A genuinely empty
 * result is cached as warm, so an empty station/grid does not re-query on every
 * click.
 */
export async function getFilteredLiveDetections(
  filters: FeedFilters,
): Promise<LiveDetection[]> {
  const key = buildFeedQuery(filters);
  const now = Date.now();

  let entry = cache.get(key);
  if (!entry) {
    entry = { detections: [], fetchedAt: 0, refreshing: null, lastRefreshStartedAt: 0 };
    cache.set(key, entry);
    evictIfNeeded();
  }
  touch(key, entry);

  // Never served yet: block on the query so we return real data. Concurrent cold
  // callers await the same single-flight promise.
  if (entry.fetchedAt === 0) {
    await startRefresh(key, filters, entry);
    return entry.detections;
  }

  // Warm: serve immediately and revalidate in the background when stale.
  const isStale = now - entry.fetchedAt > FRESH_MS;
  const canRefresh =
    !entry.refreshing && now - entry.lastRefreshStartedAt > REFRESH_MIN_GAP_MS;
  if (isStale && canRefresh) void startRefresh(key, filters, entry);
  return entry.detections;
}
