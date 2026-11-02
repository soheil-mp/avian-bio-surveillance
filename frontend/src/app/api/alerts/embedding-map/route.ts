import { NextRequest, NextResponse } from "next/server";

import { loadAlertsData } from "@/app/alerts/data";
import { packAlertsEmbeddingMap } from "@/app/alerts/embedding-map";
import { withMeasuredApiRoute } from "@/lib/performance/server";
import type { SearchParamValue } from "@/app/alerts/server";

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
    const data = await loadAlertsData(searchParamsToRecord(request));
    return NextResponse.json(packAlertsEmbeddingMap(data));
  } catch {
    return NextResponse.json(
      { error: "Failed to load alerts embedding map" },
      { status: 500 },
    );
  }
};

export const GET = withMeasuredApiRoute(handleGet);
