import { NextResponse } from "next/server";

import { fetchFeedFilterOptions } from "@/lib/home-live-detections";
import { withMeasuredApiRoute } from "@/lib/performance/server";

// Filter option lists for the Live Feed filter panel. All underlying queries hit
// small reference/modeling tables, so this is cheap; the client fetches it once
// on first opening the panel and caches it for the session.
export const dynamic = "force-dynamic";

const handleGet = async () => {
  try {
    const options = await fetchFeedFilterOptions();
    return NextResponse.json(options);
  } catch {
    return NextResponse.json({
      weeks: [],
      species: [],
      stations: [],
      grids: [],
    });
  }
};

export const GET = withMeasuredApiRoute(handleGet);
