import { NextRequest, NextResponse } from "next/server";

import {
  loadAlertsEvaluationData,
  loadAlertsModelPerformance,
} from "@/app/alerts/data";
import type { SearchParamValue } from "@/app/alerts/server";
import { withMeasuredApiRoute } from "@/lib/performance/server";

export const dynamic = "force-dynamic";

function searchParamsToRecord(request: NextRequest): Record<string, SearchParamValue> {
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

const handleGet = async (request: NextRequest) => {
  try {
    const searchParams = searchParamsToRecord(request);
    const [evaluation, modelPerformance] = await Promise.all([
      loadAlertsEvaluationData(searchParams),
      loadAlertsModelPerformance(searchParams),
    ]);

    return NextResponse.json({ evaluation, modelPerformance });
  } catch {
    return NextResponse.json(
      { error: "Failed to load alerts evaluation data" },
      { status: 500 },
    );
  }
};

export const GET = withMeasuredApiRoute(handleGet);
