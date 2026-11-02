import { NextResponse } from "next/server";

import { getLiveDetections } from "@/lib/home-live-detections-cache";
import { getFilteredLiveDetections } from "@/lib/home-live-detections-filtered-cache";
import {
  isFilteredFeed,
  type FeedFilters,
  type FeedFilterSort,
} from "@/lib/home-feed-filters";
import { withMeasuredApiRoute } from "@/lib/performance/server";

// Always reflect the current snapshot; the default path serves the last good
// cached result instantly and revalidates in the background. Filtered requests
// (station/grid spotlight, manual filters) run a bounded query behind their own
// stale-while-revalidate cache (see home-live-detections-filtered-cache).
export const dynamic = "force-dynamic";

function normalizeThreshold(raw: string | null): number | null {
  if (!raw) return null;
  const value = Number(raw);
  if (!Number.isFinite(value)) return null;
  const normalized = value > 1 ? value / 100 : value;
  return Math.min(Math.max(normalized, 0), 1);
}

function parseFilters(request: Request): FeedFilters {
  const params = new URL(request.url).searchParams;
  const sortRaw = params.get("sort");
  const sort: FeedFilterSort =
    sortRaw === "anomaly" ? "anomaly" : sortRaw === "confidence" ? "confidence" : "latest";
  const weekRaw = params.get("week");
  const week = weekRaw && /^\d{4}-\d{2}-\d{2}$/.test(weekRaw) ? weekRaw : null;

  return {
    week,
    speciesId: params.get("species") || null,
    stationId: params.get("station") || null,
    gridCellId: params.get("grid") || null,
    minConfidence: normalizeThreshold(params.get("minConf")),
    minScore: normalizeThreshold(params.get("minScore")),
    sort,
  };
}

const handleGet = async (request: Request) => {
  try {
    const filters = parseFilters(request);
    if (isFilteredFeed(filters)) {
      const detections = await getFilteredLiveDetections(filters);
      return NextResponse.json({ detections });
    }
    const detections = await getLiveDetections(
      filters.sort === "anomaly" ? "anomaly" : "latest",
    );
    return NextResponse.json({ detections });
  } catch {
    // DB/storage temporarily unavailable — return an empty feed rather than 500
    // so the dashboard panel degrades gracefully instead of erroring.
    return NextResponse.json({ detections: [] });
  }
};

export const GET = withMeasuredApiRoute(handleGet);
