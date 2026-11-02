// Isomorphic feed-filter contract shared by the client panel and the API route.
// Intentionally free of "server-only" so the client `LiveDetectionsTable` can
// import the types and pure helpers without pulling in the DB layer.

export type FeedFilterSort = "latest" | "anomaly" | "confidence";

// A draft/applied filter set for the Live Feed. Numeric thresholds are stored
// normalised to 0..1 (null = unset). `week` is a period_start (YYYY-MM-DD);
// selecting one switches the feed from the latest matching detection window to
// that bounded week.
export type FeedFilters = {
  week: string | null;
  speciesId: string | null;
  stationId: string | null;
  gridCellId: string | null;
  minConfidence: number | null;
  minScore: number | null;
  sort: FeedFilterSort;
};

export type FeedFilterOption = { id: string; label: string };
export type FeedFilterWeek = { id: string; start: string; end: string; label: string };

export type FeedFilterOptions = {
  weeks: FeedFilterWeek[];
  species: FeedFilterOption[];
  stations: FeedFilterOption[];
  grids: FeedFilterOption[];
};

export const DEFAULT_FEED_FILTERS: FeedFilters = {
  week: null,
  speciesId: null,
  stationId: null,
  gridCellId: null,
  minConfidence: null,
  minScore: null,
  sort: "latest",
};

// Count of *narrowing* filters in effect (sort is an ordering, not a filter).
export function countActiveFilters(f: FeedFilters): number {
  let n = 0;
  if (f.week) n += 1;
  if (f.speciesId) n += 1;
  if (f.stationId) n += 1;
  if (f.gridCellId) n += 1;
  if (f.minConfidence != null && f.minConfidence > 0) n += 1;
  if (f.minScore != null && f.minScore > 0) n += 1;
  return n;
}

// Whether the request must take the bounded filtered query path rather than the
// cached default feed. A non-default sort of "confidence" also requires it,
// since the stale-while-revalidate cache only holds "latest"/"anomaly".
export function isFilteredFeed(f: FeedFilters): boolean {
  return countActiveFilters(f) > 0 || f.sort === "confidence";
}

// Serialise a filter set into the query string consumed by
// /api/home/live-detections. An empty string means "default feed".
export function buildFeedQuery(f: FeedFilters): string {
  const p = new URLSearchParams();
  if (f.week) p.set("week", f.week);
  if (f.speciesId) p.set("species", f.speciesId);
  if (f.stationId) p.set("station", f.stationId);
  if (f.gridCellId) p.set("grid", f.gridCellId);
  if (f.minConfidence != null && f.minConfidence > 0) p.set("minConf", String(f.minConfidence));
  if (f.minScore != null && f.minScore > 0) p.set("minScore", String(f.minScore));
  if (f.sort && f.sort !== "latest") p.set("sort", f.sort);
  return p.toString();
}
