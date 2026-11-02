import { NextRequest, NextResponse } from "next/server";

import { loadAlertsModelPerformance } from "@/app/alerts/data";
import type { SearchParamValue } from "@/app/alerts/server";
import { withMeasuredApiRoute } from "@/lib/performance/server";

export const dynamic = "force-dynamic";

function searchParamsToRecord(
  request: NextRequest,
): Record<string, SearchParamValue> {
  const params: Record<string, SearchParamValue> = {};
  request.nextUrl.searchParams.forEach((value, key) => {
    const current = params[key];
    if (Array.isArray(current)) {
      current.push(value);
    } else if (typeof current === "string") {
      params[key] = [current, value];
    } else {
      params[key] = value;
    }
  });
  return params;
}

// Lightweight endpoint that returns ONLY the parsed validation report
// (spatial early-warning evaluation + scoring mode). It reads a single JSON
// artifact, so it is fast and independent of the heavier trend-evaluation
// dataset — the spatial panel should not wait behind that load.
const handleGet = async (request: NextRequest) => {
  try {
    const searchParams = searchParamsToRecord(request);
    const modelPerformance = await loadAlertsModelPerformance(searchParams);
    return NextResponse.json({ modelPerformance });
  } catch {
    return NextResponse.json(
      { error: "Failed to load model performance report" },
      { status: 500 },
    );
  }
};

export const GET = withMeasuredApiRoute(handleGet);
